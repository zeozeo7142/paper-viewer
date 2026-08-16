"""paper-viewer 로컬 서버.

실행:  python server.py    ->  http://localhost:8000

동작:
- papers/ 폴더의 PDF 목록을 보여주고, 하나를 열면
- (캐시가 없으면) PDF 파싱 + Ollama 번역을 수행하며 진행률을 SSE 로 스트리밍,
- 완료되면 좌(영문)/우(한글) 뷰어에 블록을 행 단위로 정렬해 표시한다.
- 결과는 cache/<hash>/ 에 저장되어 다음에 열 때 즉시 로드된다.
"""

from __future__ import annotations

import hashlib
import json
import queue
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from pipeline import Translator, parse_pdf
from pipeline.export import export_filename, export_paper
from pipeline.model_select import select_best_model

ROOT = Path(__file__).parent
PAPERS_DIR = ROOT / "papers"
CACHE_DIR = ROOT / "cache"
WEB_DIR = ROOT / "web"
EXPORT_DIR = ROOT / "exports"
TM_PATH = CACHE_DIR / "tm.json"  # 번역 메모리(문단 캐시, 논문 간 공유)
GLOSSARY_PATH = ROOT / "glossary.json"

PAPERS_DIR.mkdir(exist_ok=True)
CACHE_DIR.mkdir(exist_ok=True)

app = FastAPI(title="paper-viewer")

_MODEL_INFO: dict | None = None
_READY_MODEL: str | None = None  # 헬스체크까지 통과한 실제 사용 모델(메모이즈)
_HEALTH_PATH = CACHE_DIR / "model_health.json"


def _load_failed_models() -> set:
    """이 머신에서 '불안정'으로 판정된 모델 목록(디스크 영속). 재시도/대기 방지."""
    try:
        return set(json.loads(_HEALTH_PATH.read_text(encoding="utf-8")).get("failed", []))
    except Exception:
        return set()


def _save_failed_models() -> None:
    try:
        _HEALTH_PATH.write_text(json.dumps({"failed": sorted(_FAILED_MODELS)}, ensure_ascii=False),
                                encoding="utf-8")
    except Exception:
        pass


_FAILED_MODELS: set = _load_failed_models()  # 불안정 판정 모델(디스크에서 로드)


def get_model() -> dict:
    """이 머신 GPU에 맞는 모델 후보(primary/fallback)를 한 번 선택해 기억한다."""
    global _MODEL_INFO
    if _MODEL_INFO is None:
        _MODEL_INFO = select_best_model()
    return _MODEL_INFO


def _glossary_sig() -> str:
    """용어집 '내용' 해시. (기기 간 이동에도 동일하도록 내용 기반)"""
    if GLOSSARY_PATH.exists():
        return hashlib.sha1(GLOSSARY_PATH.read_bytes()).hexdigest()[:10]
    return "none"


def _paper_id(pdf: Path) -> str:
    # 모델명은 넣지 않는다 → 번역 결과를 어떤 기기에서 열어도 같은 캐시를 가리킴(뷰 전용은 GPU 불필요).
    st = pdf.stat()
    raw = f"{pdf.name}|{st.st_size}|{int(st.st_mtime)}|{_glossary_sig()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _doc_dir(pid: str) -> Path:
    return CACHE_DIR / pid


def _resolve_pdf(rel_or_abs: str) -> Path:
    p = Path(rel_or_abs)
    if not p.is_absolute():
        p = (PAPERS_DIR / rel_or_abs).resolve()
    if not p.exists() or p.suffix.lower() != ".pdf":
        raise HTTPException(404, f"PDF 를 찾을 수 없습니다: {rel_or_abs}")
    return p


# ---------------------------------------------------------------- API ----
@app.get("/api/papers")
def list_papers():
    items = []
    for pdf in sorted(PAPERS_DIR.glob("**/*.pdf")):
        pid = _paper_id(pdf)
        items.append({
            "name": str(pdf.relative_to(PAPERS_DIR)).replace("\\", "/"),
            "id": pid,
            "cached": (_doc_dir(pid) / "doc.json").exists(),
        })
    info = get_model()
    # 실제로 사용할(=이미 검증됐거나, 불안정 판정 안 된) 모델을 표시
    eff = _READY_MODEL
    if not eff:
        primary = info["primary"]
        eff = primary if primary not in _FAILED_MODELS else (info.get("fallback") or primary)
    return {"papers": items, "model": eff,
            "vram": info["vram"], "model_reason": info["reason"]}


@app.get("/api/doc/{pid}")
def get_doc(pid: str):
    f = _doc_dir(pid) / "doc.json"
    if not f.exists():
        raise HTTPException(404, "아직 처리되지 않은 논문입니다.")
    return JSONResponse(json.loads(f.read_text(encoding="utf-8")))


@app.get("/api/process")
def process(path: str):
    """PDF 를 파싱+번역하고 진행률을 SSE(text/event-stream)로 스트리밍."""
    pdf = _resolve_pdf(path)
    pid = _paper_id(pdf)
    out_dir = _doc_dir(pid)
    doc_json = out_dir / "doc.json"

    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    def gen():
        # 캐시 히트
        if doc_json.exists():
            yield sse("done", {"id": pid, "cached": True})
            return

        yield sse("status", {"phase": "parse", "message": "PDF 구조 분석 중…"})
        try:
            blocks = parse_pdf(pdf, out_dir / "assets")
        except Exception as e:
            yield sse("error", {"message": f"PDF 파싱 실패: {e}"})
            return
        yield sse("status", {"phase": "parsed", "message": f"{len(blocks)}개 블록 추출 완료. 번역 준비…"})

        global _READY_MODEL
        info = get_model()
        # 후보: Qwen3(primary) → Qwen2.5(fallback). 헬스체크 통과한 모델을 사용.
        candidates = [info["primary"], info.get("fallback")]
        candidates = [c for i, c in enumerate(candidates) if c and c not in candidates[:i]]
        tr = None
        model = None
        last_msg = ""
        for candidate in candidates:
            if candidate in _FAILED_MODELS:  # 이미 불안정 판정 → 건너뜀(재-wedge 방지)
                continue
            t = Translator(model=candidate, cache_path=TM_PATH, glossary_path=GLOSSARY_PATH)
            ok, msg = t.ensure_ready()
            if not ok and "없습니다" in msg:  # 미설치 → 자동 다운로드(최초 1회)
                yield sse("status", {"phase": "pull", "message": f"모델 다운로드 중(최초 1회): {candidate}"})
                try:
                    last_pct = -1
                    for p in t.pull_stream():
                        total, done = p.get("total"), p.get("completed")
                        if total and done is not None:
                            pct = int(done * 100 / total)
                            if pct != last_pct:  # 1% 단위로만 전송(과도한 이벤트 방지)
                                last_pct = pct
                                yield sse("status", {
                                    "phase": "pull", "pct": pct,
                                    "message": f"모델 다운로드 중: {candidate} — {pct}% "
                                               f"({done/1e9:.1f}/{total/1e9:.1f}GB)",
                                })
                    ok, msg = t.ensure_ready()
                except Exception as e:
                    ok, msg = False, f"다운로드 실패: {e}"
            if not ok:
                last_msg = msg
                yield sse("status", {"phase": "fallback", "message": f"{candidate} 사용 불가 — 대체 모델 시도…"})
                continue
            # 실제로 생성이 되는지 헬스체크(멈추는 모델 감지 → 폴백)
            if _READY_MODEL != candidate:  # 세션 중 이미 검증된 모델이면 생략
                yield sse("status", {"phase": "check", "message": f"모델 점검 중: {candidate}"})
                if not t.health_ok(timeout=90):
                    _FAILED_MODELS.add(candidate)
                    _save_failed_models()  # 디스크에 기억 → 이 머신에서 다시 시도/대기 안 함
                    last_msg = f"{candidate} 응답 없음(이 GPU/Ollama에서 불안정)"
                    yield sse("status", {"phase": "fallback", "message": f"{candidate} 불안정 — 대체 모델로 전환…"})
                    continue
            tr, model = t, candidate
            _READY_MODEL = candidate
            break
        if tr is None:
            yield sse("error", {"message": last_msg or "사용 가능한 모델이 없습니다."})
            return
        yield sse("status", {"phase": "model", "message": f"번역 모델: {model}"})

        # 진행률을 백그라운드 스레드에서 큐로 전달
        q: "queue.Queue" = queue.Queue()

        def worker():
            def prog(done: int, total: int):
                q.put(("progress", {"done": done, "total": total}))
            try:
                tr.translate_blocks(blocks, progress=prog)
                q.put(("finish", None))
            except Exception as e:  # noqa
                q.put(("fail", str(e)))

        threading.Thread(target=worker, daemon=True).start()

        while True:
            kind, data = q.get()
            if kind == "progress":
                yield sse("progress", data)
            elif kind == "fail":
                yield sse("error", {"message": f"번역 실패: {data}"})
                return
            elif kind == "finish":
                break

        doc = {"id": pid, "title": _guess_title(blocks), "model": model,
               "source": pdf.name, "blocks": blocks}
        out_dir.mkdir(parents=True, exist_ok=True)
        doc_json.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        yield sse("done", {"id": pid, "cached": False})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/export/{pid}")
def export_doc(pid: str):
    """번역된 논문을 단일 HTML 파일로 내보낸다(GPU·서버 없이 어떤 기기에서도 열림)."""
    dd = _doc_dir(pid)
    if not (dd / "doc.json").exists():
        raise HTTPException(404, "먼저 논문을 번역한 뒤 내보낼 수 있습니다.")
    doc = json.loads((dd / "doc.json").read_text(encoding="utf-8"))
    name = export_filename(doc)
    out = EXPORT_DIR / f"{name}.html"
    export_paper(dd, out, WEB_DIR / "style.css")
    return FileResponse(str(out), media_type="text/html; charset=utf-8",
                        filename=f"{name}.html")


def _guess_title(blocks: list[dict]) -> str:
    for b in blocks[:5]:
        if b["type"] in ("heading", "text") and len(b["text"]) > 8:
            return b["text"][:120]
    return "논문"


# 캐시(그림/표 이미지) 정적 서빙:  /cache/<pid>/assets/xxx.png
app.mount("/cache", StaticFiles(directory=str(CACHE_DIR)), name="cache")


@app.get("/")
def index():
    return FileResponse(str(WEB_DIR / "index.html"))


# web 정적 파일
app.mount("/web", StaticFiles(directory=str(WEB_DIR)), name="web")


if __name__ == "__main__":
    import uvicorn

    print("paper-viewer  ->  http://localhost:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
