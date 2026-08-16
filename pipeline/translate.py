"""블록의 영어 텍스트를 자연스러운 학술 한국어로 번역.

주요 기능:
- 로컬 Ollama(Qwen2.5 계열)로 번역. 문단 단위 캐시(cache/tm.json)로 재실행 시 즉시 로드.
- 용어집(glossary.json) 주입 → 핵심 용어를 일관된 한국어로 번역.
- 문장 단위 1:1 정렬 번역 → 좌/우 문장 상호 하이라이트가 가능하도록 sentences[] 생성.
- 그림/표에 대한 쉬운 한국어 설명(explain) 생성.
- 언어 가드: 한자/가나/키릴 등 비한국어 유출을 감지해 재시도·교정·구간치환으로 제거.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Callable

import requests

OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5:7b-instruct"

SYSTEM_PROMPT = (
    "당신은 영어 학술 논문을 한국어로 옮기는 전문 번역가입니다. 규칙:\n"
    "1) 출력은 반드시 한국어(한글)로만 작성합니다. 한자·중국어·일본어 문자를 절대 사용하지 않습니다.\n"
    "2) 의미를 정확히 보존하되 번역투가 아닌 자연스럽고 매끄러운 학술 한국어로 옮깁니다.\n"
    "3) 전문 용어는 통용되는 한국어 표기를 쓰되, 널리 영어로 쓰는 용어(Transformer, GPU, LiDAR 등)는 그대로 둡니다.\n"
    "4) 숫자, 수식, 변수, 인용 표기([12], (3) 등), 고유명사, 단위는 절대 바꾸지 않습니다.\n"
    "5) 원문에 없는 내용을 덧붙이거나 요약하지 않고 빠짐없이 옮깁니다.\n"
    "6) 설명·머리말·따옴표 없이 오직 번역 결과만 출력합니다."
)

_EXAMPLE_EN = "We evaluate our method on three benchmarks and achieve state-of-the-art performance [15]."
_EXAMPLE_KO = "우리는 세 가지 벤치마크에서 제안 방법을 평가하였으며, 최고 수준의 성능을 달성하였다 [15]."

_SKIP_RE = re.compile(r"^[\s\d\W]+$")
_HANGUL = re.compile(r"[가-힣]")
# 한국어 번역에 나와선 안 되는 문자: 한자·가나·키릴·아랍·히브리·태국·데바나가리.
# (그리스문자 α·β·σ·θ 등은 수식에 정상적으로 쓰이므로 제외한다.)
_FOREIGN = re.compile(r"[一-鿿぀-ヿЀ-ӿ؀-ۿ֐-׿฀-๿ऀ-ॿ]")
_FOREIGN_RUN = re.compile(
    r"[一-鿿぀-ヿｦ-ﾟЀ-ӿ؀-ۿ֐-׿฀-๿ऀ-ॿ，。：；！？、（）「」『』]+"
)


def _looks_non_korean(out: str, src: str) -> bool:
    """번역 결과가 한국어가 아닌 것으로 의심되면 True (중국어/일본어/키릴 등 유출)."""
    if _FOREIGN.search(out):
        return True
    hangul = len(_HANGUL.findall(out))
    letters = sum(1 for c in src if c.isascii() and c.isalpha())
    if letters > 20 and hangul < 3:
        return True
    return False


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z(\"'])", text.strip())
    return [p.strip() for p in parts if p.strip()]


# --- 번역하지 않고 영문을 유지할 블록 판별 -------------------------------------
_EMAIL_RE = re.compile(r"\S+@\S+\.\S+")
_AFFIL_RE = re.compile(r"\bare with\b|\bUniversit|\bLaborator|\bInstitute\b|\bDepartment\b", re.I)
_REF_HEAD_RE = re.compile(r"^\s*R\s*EFERENCES\b|^\s*(참고\s*문헌|bibliography)\b", re.I)


def _is_references_start(text: str, btype: str) -> bool:
    if _REF_HEAD_RE.search(text):
        return True
    if re.match(r"^\s*\[1\]\s+[A-Z]", text):
        return True
    return False


def _keep_english(text: str) -> bool:
    if _EMAIL_RE.search(text):
        return True
    if _AFFIL_RE.search(text):
        return True
    if " and " in text and text.count(",") >= 2 and not re.search(r"\.\s*$", text) and len(text) < 200:
        words = re.findall(r"[A-Za-z][A-Za-z.\-]+", text)
        if words and sum(1 for w in words if w[:1].isupper()) / len(words) > 0.7:
            return True
    return False


def _strip_wrappers(s: str) -> str:
    s = s.strip()
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.S).strip()  # Qwen3 추론 블록 제거(안전장치)
    s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
    s = re.sub(r"\n?```$", "", s).strip()
    lines = s.split("\n")
    while lines:
        first = lines[0].strip()
        if first == "":
            lines.pop(0)
            continue
        if len(first) < 40 and re.search(
            r"(다음과\s*같|번역\s*결과|아래(와)?\s*같|번역하면|翻译如下|翻譯如下|译文如下)", first
        ):
            lines.pop(0)
            continue
        break
    s = "\n".join(lines).strip()
    s = re.sub(r"^(번역|한국어\s*번역|Translation)\s*[:：]\s*", "", s)
    if len(s) >= 2 and s[0] in "\"“'" and s[-1] in "\"”'":
        s = s[1:-1].strip()
    return s.strip()


def _parse_numbered(out: str, n: int) -> list[str] | None:
    """`[1] ...` 형식(또는 `1. ...`)의 번호 매겨진 번역을 파싱해 리스트로 반환."""
    d: dict[int, str] = {}
    for m in re.finditer(r"\[(\d+)\]\s*(.*?)(?=\s*\[\d+\]|\Z)", out, re.S):
        d[int(m.group(1))] = m.group(2).strip()
    if sum(1 for i in range(1, n + 1) if d.get(i)) < n:
        d2: dict[int, str] = {}
        for m in re.finditer(r"(?m)^\s*(\d+)[.)]\s*(.*?)(?=\n\s*\d+[.)]|\Z)", out, re.S):
            d2[int(m.group(1))] = m.group(2).strip()
        if sum(1 for i in range(1, n + 1) if d2.get(i)) > sum(1 for i in range(1, n + 1) if d.get(i)):
            d = d2
    if all(d.get(i) for i in range(1, n + 1)):
        return [d[i].replace("\n", " ").strip() for i in range(1, n + 1)]
    return None


class Translator:
    def __init__(self, model: str = DEFAULT_MODEL, cache_path: str | Path | None = None,
                 url: str = OLLAMA_URL, glossary_path: str | Path | None = None):
        self.model = model
        self.url = url.rstrip("/")
        self.cache_path = Path(cache_path) if cache_path else None
        self._cache: dict = {}
        if self.cache_path and self.cache_path.exists():
            try:
                self._cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except Exception:
                self._cache = {}
        self.glossary: dict[str, str] = {}
        if glossary_path and Path(glossary_path).exists():
            try:
                raw = json.loads(Path(glossary_path).read_text(encoding="utf-8"))
                self.glossary = {k: v for k, v in raw.items() if not k.startswith("_")}
            except Exception:
                self.glossary = {}

    # --- 공개 API ---------------------------------------------------------
    def ensure_ready(self) -> tuple[bool, str]:
        try:
            r = requests.get(f"{self.url}/api/tags", timeout=5)
            r.raise_for_status()
        except Exception as e:
            return False, f"Ollama 서버에 연결할 수 없습니다 ({self.url}). 'ollama serve' 실행 여부를 확인하세요. [{e}]"
        names = [m.get("name", "") for m in r.json().get("models", [])]
        base = self.model.split(":")[0]
        if not any(n == self.model or n.startswith(base) for n in names):
            return False, f"모델 '{self.model}' 이(가) 없습니다. 'ollama pull {self.model}' 로 받아주세요. (보유: {names})"
        return True, "ok"

    def pull(self) -> None:
        """모델을 Ollama 로 다운로드(최초 1회). 대형 모델은 오래 걸릴 수 있음.

        스트리밍으로 받아 중간 오류(네트워크/레지스트리 등)를 실제 메시지로 드러낸다.
        (비스트리밍 pull 은 대용량에서 opaque 한 500 을 반환하는 경우가 있음)
        """
        with requests.post(f"{self.url}/api/pull",
                           json={"name": self.model, "stream": True},
                           stream=True, timeout=7200) as r:
            r.raise_for_status()
            last = ""
            for line in r.iter_lines():
                if not line:
                    continue
                try:
                    obj = json.loads(line.decode("utf-8"))
                except Exception:
                    continue
                if obj.get("error"):
                    raise RuntimeError(obj["error"])  # 실제 원인 메시지 노출
                last = obj.get("status", last)
            if last != "success":
                # 스트림이 success 로 끝나지 않으면 준비 여부로 최종 확인
                ok, _ = self.ensure_ready()
                if not ok:
                    raise RuntimeError(f"모델 다운로드가 완료되지 않았습니다(status={last!r}).")

    def health_ok(self, timeout: int = 60) -> bool:
        """모델이 이 머신에서 실제로 (제한시간 안에) 생성하는지 확인.
        Qwen3 가 구형 Ollama 등에서 멈추는 경우를 감지해 폴백 판단에 사용한다.
        (모델 최초 로드 시간을 감안해 timeout 은 넉넉히.)"""
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": "Translate to Korean: ready"}],
            "stream": False,
            "keep_alive": "30m",
            "options": {"temperature": 0, "num_predict": 8, "num_ctx": 1024},
        }
        if "qwen3" in self.model:
            payload["think"] = False
        try:
            r = requests.post(f"{self.url}/api/chat", json=payload, timeout=timeout)
            r.raise_for_status()
            return bool(r.json().get("message", {}).get("content", "").strip())
        except Exception:
            return False

    def translate_blocks(
        self,
        blocks: list[dict],
        progress: Callable[[int, int], None] | None = None,
    ) -> list[dict]:
        """각 블록에 번역 결과를 채운다.
        - text/heading/caption: sentences=[{en, ko}] 및 ko(전체 합본).
        - figure/table: explain(쉬운 한국어 설명), caption_en(연결된 캡션).
        """
        _attach_captions(blocks)

        # 참고문헌 시작 위치 탐색 → 본문/참고문헌 분리
        ref_start = None
        for i, b in enumerate(blocks):
            if b["type"] in ("text", "heading", "caption") and _is_references_start(b["text"], b["type"]):
                ref_start = i
                break
        body = blocks if ref_start is None else blocks[:ref_start]
        tail = [] if ref_start is None else blocks[ref_start:]

        body_work = [b for b in body if b["type"] in ("text", "heading", "caption", "figure", "table")]
        ref_entries = _consolidate_references(tail)  # 개별 참고문헌(영문) 목록
        total = len(body_work) + len(ref_entries)
        done = 0

        # 본문 번역 (문장 정렬 / 저자·소속은 영문 유지)
        for b in body_work:
            try:
                if b["type"] in ("text", "heading", "caption"):
                    t = b["text"]
                    if _keep_english(t):
                        b["sentences"] = [{"en": s, "ko": s} for s in (_split_sentences(t) or [t])]
                        b["ko"] = t
                    else:
                        b["sentences"] = self._aligned_cached(t)
                        b["ko"] = " ".join(s["ko"] for s in b["sentences"])
                elif b["type"] in ("figure", "table"):
                    b["explain"] = self._explain_cached(b)
            except Exception:  # 한 블록 실패가 전체를 중단시키지 않도록 영문 폴백
                if b["type"] in ("text", "heading", "caption"):
                    b["sentences"] = [{"en": s, "ko": s} for s in (_split_sentences(b["text"]) or [b["text"]])]
                    b["ko"] = b["text"]
                else:
                    b["explain"] = ""
            done += 1
            if progress:
                progress(done, total)
            if self.cache_path and done % 8 == 0:
                self.save()

        # 참고문헌: 영문 유지 + 각 논문 제목만 한글 번역 (하이라이트 없음)
        new_refs: list[dict] = []
        if ref_entries:
            new_refs.append({"type": "heading", "text": "REFERENCES", "ko": "참고문헌", "nohl": True})
            for e in ref_entries:
                try:
                    ko = self._translate_ref_entry(e)
                except Exception:
                    ko = e  # 실패 시 영문 유지
                new_refs.append({"type": "reference", "text": e, "ko": ko, "nohl": True})
                done += 1
                if progress:
                    progress(done, total)
                if self.cache_path and done % 8 == 0:
                    self.save()

        blocks[:] = body + new_refs  # 조각난 참고문헌 블록을 정리된 블록으로 대체
        self.save()
        return blocks

    def save(self) -> None:
        if self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self._cache, ensure_ascii=False), encoding="utf-8")

    # --- 캐시 래퍼 --------------------------------------------------------
    def _aligned_cached(self, text: str) -> list[dict]:
        # 캐시 키에 '이 문단에 적용되는 용어집 항목'을 포함 →
        # 용어집을 수정하면 해당 용어가 든 문단만 자동으로 다시 번역된다.
        key = self._key("ALIGN", self._glossary_hint(text) + "" + text)
        if key in self._cache:
            return self._cache[key]
        val = self._translate_aligned(text)
        self._cache[key] = val
        return val

    def _explain_cached(self, block: dict) -> str:
        cap = block.get("caption_en", "") or ""
        if len(cap.strip()) < 8:
            return ""  # 캡션이 없으면 지어내지 않는다
        key = self._key("EXPLAIN", block["type"] + "|" + cap)
        if key in self._cache:
            return self._cache[key]
        val = self._explain_graphic(block["type"], cap)
        self._cache[key] = val
        return val

    def _cached_text(self, kind: str, text: str) -> str:
        """단일 문자열 번역을 캐시(용어집 반영)."""
        key = self._key(kind, self._glossary_hint(text) + "" + text)
        if key in self._cache:
            return self._cache[key]
        val = self._translate_text(text)
        self._cache[key] = val
        return val

    def _translate_ref_entry(self, entry: str) -> str:
        """참고문헌 항목에서 따옴표로 둘러싸인 논문 제목만 한국어로 치환. 나머지는 영문 유지."""
        m = re.search(r"[\"“]([^\"”]{6,}?)[\"”]", entry)
        if not m:
            return entry  # 제목(따옴표)이 없으면 원문 그대로
        title = m.group(1).rstrip(", ").strip()
        ko = _strip_wrappers(self._cached_text("REFTITLE", title))
        if not ko or _looks_non_korean(ko, title):
            return entry
        return entry[:m.start()] + "“" + ko + "”" + entry[m.end():]

    def _key(self, kind: str, text: str) -> str:
        return hashlib.sha1(f"{self.model} v4 {kind} {text}".encode("utf-8")).hexdigest()

    # --- 문장 정렬 번역 ---------------------------------------------------
    def _translate_aligned(self, text: str) -> list[dict]:
        text = text.strip()
        if not text:
            return [{"en": text, "ko": text}]
        sents = _split_sentences(text)
        if len(sents) <= 1:
            return [{"en": text, "ko": self._translate_text(text)}]

        numbered = "\n".join(f"[{i + 1}] {s}" for i, s in enumerate(sents))
        system = (
            SYSTEM_PROMPT
            + "\n각 번호 문장을 같은 번호를 붙여 한국어로 번역하세요. 문장을 합치거나 나누지 말고 번호와 개수를 그대로 유지하세요."
            + self._glossary_hint(text)
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content":
                "다음 번호가 매겨진 영어 문장들을 각각 한국어로 번역하세요. 반드시 [번호] 형식을 유지하세요.\n\n" + numbered},
        ]
        out = self._post(messages, 0.2)
        ko_list = _parse_numbered(out, len(sents))
        if ko_list is None:
            ko_list = [self._translate_text(s) for s in sents]

        result = []
        for en, ko in zip(sents, ko_list):
            ko = _strip_wrappers(ko)
            if _looks_non_korean(ko, en):
                ko = self._repair_or_retry(en, ko)
            result.append({"en": en, "ko": ko})
        return result

    def _repair_or_retry(self, en: str, ko: str) -> str:
        r = self._repair_korean(ko)
        if not _looks_non_korean(r, en):
            return r
        r2 = self._translate_text(en)
        return r2

    # --- 단일 문자열 강건 번역(5단계 방어) --------------------------------
    def _translate_text(self, text: str) -> str:
        text = text.strip()
        if not text or _SKIP_RE.match(text):
            return text
        out = _strip_wrappers(self._translate_once(text, 0.2, reinforce=False))
        if not _looks_non_korean(out, text):
            return out
        out2 = _strip_wrappers(self._translate_once(text, 0.0, reinforce=True))
        if not _looks_non_korean(out2, text):
            return out2
        best = out2 or out
        sents = _split_sentences(text)
        if len(sents) > 1:
            parts = []
            for s in sents:
                o = _strip_wrappers(self._translate_once(s, 0.0, reinforce=True))
                if _looks_non_korean(o, s):
                    o = self._repair_korean(o)
                parts.append(o)
            joined = " ".join(p for p in parts if p)
            if not _looks_non_korean(joined, text):
                return joined
            best = joined
        for _ in range(2):
            if not _looks_non_korean(best, text):
                break
            best = self._repair_korean(best)
        if _FOREIGN.search(best):
            best = self._deforeign_substrings(best)
        return best

    def _translate_once(self, text: str, temperature: float, reinforce: bool) -> str:
        system = SYSTEM_PROMPT + self._glossary_hint(text)
        if reinforce:
            system += (
                "\n\n[매우 중요] 출력은 100% 한국어(한글)로만. 한자·중국어·일본어를 단 한 글자도 쓰지 마세요. "
                "생각이나 설명을 쓰지 말고 번역문만 출력하세요."
            )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"다음 영어를 한국어로 번역하세요.\n\n{_EXAMPLE_EN}"},
            {"role": "assistant", "content": _EXAMPLE_KO},
            {"role": "user", "content": f"다음 영어를 한국어로 번역하세요.\n\n{text}"},
        ]
        return self._post(messages, temperature)

    def _repair_korean(self, text: str) -> str:
        messages = [
            {"role": "system", "content":
                "당신은 한국어 교정 전문가입니다. 입력에 한자·중국어·일본어·기타 외국문자가 섞여 있으면 "
                "의미를 그대로 유지한 채 모든 표현을 자연스러운 한국어(한글)로만 다시 씁니다. "
                "숫자·수식·인용표기([12] 등)·영어 약어(LiDAR, MLP 등)는 그대로 둡니다. 설명 없이 결과만 출력합니다."},
            {"role": "user", "content":
                "다음 텍스트를 순수 한국어로만 다시 쓰세요. 한자·중국어·일본어를 한 글자도 남기지 마세요.\n\n" + text},
        ]
        return _strip_wrappers(self._post(messages, 0.0))

    def _deforeign_substrings(self, text: str) -> str:
        def repl(m: re.Match) -> str:
            frag = m.group(0)
            try:
                ko = _strip_wrappers(self._post([
                    {"role": "system", "content": "외국어(중국어·일본어·러시아어 등)를 자연스러운 한국어로 번역합니다. 결과만 출력하세요."},
                    {"role": "user", "content": "다음을 한국어로 번역하세요.\n\n" + frag},
                ], 0.0))
            except Exception:
                ko = ""
            return _FOREIGN_RUN.sub("", ko)
        return _FOREIGN_RUN.sub(repl, text)

    # --- 그림/표 설명 ----------------------------------------------------
    def _explain_graphic(self, gtype: str, caption_en: str) -> str:
        kind = "표" if gtype == "table" else "그림"
        system = (
            "당신은 학술 논문을 쉽게 풀어 설명하는 한국어 도우미입니다. "
            "출력은 한국어(한글)로만 작성하고, 한자·외국문자를 쓰지 않습니다. "
            "주어진 캡션에 근거하여 설명하고, 캡션에 없는 사실을 지어내지 마세요."
        )
        user = (
            f"다음은 논문 속 {kind}의 캡션입니다:\n\"{caption_en}\"\n\n"
            f"이 {kind}가 무엇을 보여주는지, 논문을 처음 읽는 사람도 이해할 수 있도록 "
            f"쉽고 친절하게 한국어로 3~4문장으로 설명하세요. 전문용어는 풀어서 설명하세요. "
            f"'이 {kind}는' 으로 시작하세요."
        )
        out = _strip_wrappers(self._post(
            [{"role": "system", "content": system}, {"role": "user", "content": user}], 0.3))
        if _looks_non_korean(out, caption_en):
            out = self._repair_korean(out)
        if _FOREIGN.search(out):
            out = self._deforeign_substrings(out)
        return out

    # --- 용어집 & 저수준 호출 --------------------------------------------
    def _glossary_hint(self, text: str) -> str:
        if not self.glossary:
            return ""
        low = text.lower()
        hits = []
        for en, ko in self.glossary.items():
            if re.search(r"\b" + re.escape(en.lower()) + r"\b", low):
                hits.append(f"{en} → {ko}")
        if not hits:
            return ""
        return "\n[용어집] 다음 용어는 반드시 지정된 한국어로 번역하세요: " + "; ".join(hits[:25])

    def _post(self, messages: list[dict], temperature: float) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "keep_alive": "30m",  # 실행 중 모델이 언로드/재로드되며 멈추는 것 방지
            "options": {"temperature": temperature, "top_p": 0.9, "num_ctx": 4096, "repeat_penalty": 1.05},
        }
        if "qwen3" in self.model:
            payload["think"] = False  # Qwen3 추론(thinking) 모드 끄기 → 번역만 빠르게 출력
        last = None
        for _ in range(3):  # 간헐적 생성 스톨(타임아웃) 시 재시도
            try:
                r = requests.post(f"{self.url}/api/chat", json=payload, timeout=90)
                r.raise_for_status()
                return r.json().get("message", {}).get("content", "").strip()
            except (requests.Timeout, requests.ConnectionError) as e:
                last = e
        raise last


def _consolidate_references(tail: list[dict]) -> list[str]:
    """조각난 참고문헌 블록들을 하나로 합쳐 `[n]` 단위 개별 항목으로 분리."""
    parts = [b["text"] for b in tail if b["type"] in ("text", "heading", "caption")]
    joined = " ".join(parts)
    joined = re.sub(r"^\s*R\s*EFERENCES\b\s*", "", joined, flags=re.I)
    joined = re.sub(r"(\w)-\s+(\w)", r"\1\2", joined)  # 줄바꿈 하이픈 이음(preci- sion → precision)
    joined = re.sub(r"\s+", " ", joined).strip()
    if not joined:
        return []
    entries = re.split(r"(?=\[\d+\]\s)", joined)
    return [e.strip() for e in entries if re.match(r"^\[\d+\]", e.strip())]


def _attach_captions(blocks: list[dict]) -> None:
    """각 그림/표 블록에 인접한 캡션 텍스트를 caption_en 으로 연결."""
    n = len(blocks)
    for i, b in enumerate(blocks):
        if b["type"] not in ("figure", "table"):
            continue
        cap = ""
        for j in (i + 1, i - 1, i + 2, i - 2):
            if 0 <= j < n and blocks[j]["type"] == "caption":
                cap = blocks[j]["text"]
                break
        b["caption_en"] = cap


if __name__ == "__main__":
    t = Translator(glossary_path=Path(__file__).resolve().parent.parent / "glossary.json")
    ok, msg = t.ensure_ready()
    print("ready:", ok, msg)
    if ok:
        sample = ("Despite the rapid advancement of navigation algorithms, mobile robots often produce "
                  "anomalous behaviors. We propose a proactive anomaly detection network. It fuses multi-sensor data.")
        aligned = t._translate_aligned(sample)
        Path("_align_out.txt").write_text(
            "\n".join(f"[{i+1}] EN: {p['en']}\n    KO: {p['ko']}" for i, p in enumerate(aligned)),
            encoding="utf-8")
        print("wrote _align_out.txt with", len(aligned), "sentences")
