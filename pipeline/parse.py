"""PDF -> 구조화된 블록 목록.

논문 PDF를 읽어 '읽는 순서(reading order)'대로 정렬된 블록 스트림으로 변환한다.
각 블록은 다음 중 하나:

    {"type": "heading", "text": ...}          # 제목/절 제목
    {"type": "text",    "text": ...}          # 본문 문단
    {"type": "caption", "text": ...}          # 그림/표 캡션 (번역 대상)
    {"type": "figure",  "image": "assets/..", "w":.., "h":..}
    {"type": "table",   "image": "assets/..", "w":.., "h":..}

그림/표는 벡터 그래픽까지 포함해 페이지 영역을 그대로 이미지로 렌더링하므로,
번역본(오른쪽)에도 영문(왼쪽)과 '같은 위치·같은 모양'으로 들어간다.

핵심: 좌/우 뷰어는 이 블록 목록을 '행 단위로 짝지어' 렌더링한다.
따라서 대응 문단이 항상 같은 행에 놓여 스크롤이 자동으로 정렬된다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import fitz  # PyMuPDF


CAPTION_RE = re.compile(r"^\s*(figure|fig\.?|table|알고리즘|algorithm)\s*\.?\s*\d+", re.I)
# 렌더 배율(이미지 선명도). 2 = 144dpi 상당.
ZOOM = 2.0
# 그래픽(그림/표) 영역으로 인정할 최소 면적 비율(페이지 대비).
MIN_GRAPHIC_AREA = 0.015


@dataclass
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    def to_fitz(self) -> fitz.Rect:
        return fitz.Rect(self.x0, self.y0, self.x1, self.y1)

    def intersects(self, o: "Rect", pad: float = 0.0) -> bool:
        return not (
            self.x1 + pad < o.x0
            or o.x1 + pad < self.x0
            or self.y1 + pad < o.y0
            or o.y1 + pad < self.y0
        )

    def union(self, o: "Rect") -> "Rect":
        return Rect(min(self.x0, o.x0), min(self.y0, o.y0), max(self.x1, o.x1), max(self.y1, o.y1))


def _merge_rects(rects: list[Rect], pad: float) -> list[Rect]:
    """가까이 있는 사각형들을 하나로 병합(클러스터링)."""
    rects = [r for r in rects if r.area > 0]
    merged = True
    while merged:
        merged = False
        out: list[Rect] = []
        for r in rects:
            hit = None
            for i, m in enumerate(out):
                if m.intersects(r, pad):
                    hit = i
                    break
            if hit is None:
                out.append(r)
            else:
                out[hit] = out[hit].union(r)
                merged = True
        rects = out
    return rects


def _detect_columns(text_rects: list[Rect], page_w: float, page_h: float) -> float | None:
    """2단 조판이면 좌/우를 가르는 x 좌표를, 1단이면 None 을 반환."""
    if not text_rects:
        return None
    mid = page_w / 2
    band = page_w * 0.06  # 중앙 근처 여유
    # 페이지 폭의 절반을 가로지르는 '전폭' 블록은 제외하고 판단
    narrow = [r for r in text_rects if r.w < page_w * 0.6]
    if len(narrow) < 4:
        return None
    left = [r for r in narrow if r.cx < mid]
    right = [r for r in narrow if r.cx >= mid]
    if len(left) < 2 or len(right) < 2:
        return None
    # 중앙 band 를 가로지르는(=단을 넘나드는) 블록이 거의 없어야 2단
    straddle = [r for r in narrow if r.x0 < mid - band and r.x1 > mid + band]
    if len(straddle) > max(1, len(narrow) * 0.12):
        return None
    # 좌단 오른쪽 끝과 우단 왼쪽 끝 사이(거터)의 중앙을 분할선으로
    left_edge = max(r.x1 for r in left)
    right_edge = min(r.x0 for r in right)
    if right_edge <= left_edge:
        return mid
    return (left_edge + right_edge) / 2


def _reading_order(blocks: list[dict], split_x: float | None, page_w: float) -> list[dict]:
    """전폭 요소를 경계(band)로, 각 band 안에서 좌단->우단 순으로 정렬."""
    def rect_of(b) -> Rect:
        return b["_rect"]

    if split_x is None:
        return sorted(blocks, key=lambda b: (rect_of(b).y0, rect_of(b).x0))

    band = page_w * 0.06
    full = [b for b in blocks if rect_of(b).x0 < split_x - band and rect_of(b).x1 > split_x + band]
    full.sort(key=lambda b: rect_of(b).y0)

    # 전폭 요소들의 y 구간으로 페이지를 밴드로 나눔
    boundaries = [(-1e9, 1e9)]  # placeholder
    edges = [-1e9] + [rect_of(b).cy for b in full] + [1e9]

    ordered: list[dict] = []
    col_blocks = [b for b in blocks if b not in full]
    # full 요소와 밴드별 컬럼 요소를 y 순서로 병합
    events = []
    for b in full:
        events.append((rect_of(b).y0, 0, b))
    # 밴드 경계 사이의 컬럼 블록 그룹화
    for i in range(len(edges) - 1):
        top, bot = edges[i], edges[i + 1]
        band_blocks = [b for b in col_blocks if top <= rect_of(b).cy < bot]
        left = sorted([b for b in band_blocks if rect_of(b).cx < split_x], key=lambda b: rect_of(b).y0)
        right = sorted([b for b in band_blocks if rect_of(b).cx >= split_x], key=lambda b: rect_of(b).y0)
        seq = left + right
        if seq:
            anchor = top if top > -1e9 else (rect_of(seq[0]).y0 - 1)
            events.append((anchor, 1, seq))
    events.sort(key=lambda e: (e[0], e[1]))
    for _, kind, payload in events:
        if kind == 0:
            ordered.append(payload)
        else:
            ordered.extend(payload)
    return ordered


def _clean_text(text: str) -> str:
    # 줄 끝 하이픈 이음 (word-\nword -> wordword)
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def parse_pdf(pdf_path: str | Path, assets_dir: str | Path) -> list[dict]:
    """PDF 를 읽어 정렬된 블록 목록을 반환. 그림/표 이미지는 assets_dir 에 저장."""
    pdf_path = Path(pdf_path)
    assets_dir = Path(assets_dir)
    assets_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(pdf_path)
    result: list[dict] = []

    # 본문 글자 크기 추정을 위해 전체 span 크기 수집
    all_sizes: list[float] = []
    for page in doc:
        d = page.get_text("dict")
        for blk in d.get("blocks", []):
            for line in blk.get("lines", []):
                for span in line.get("spans", []):
                    all_sizes.append(round(span["size"], 1))
    body_size = _median(all_sizes) if all_sizes else 10.0

    for pno, page in enumerate(doc):
        pw, ph = page.rect.width, page.rect.height

        # 1) 그래픽 영역 후보: 래스터 이미지 + 벡터 드로잉 + 표
        graphic_rects: list[tuple[str, Rect]] = []  # (kind, rect)

        for img in page.get_images(full=True):
            try:
                for r in page.get_image_rects(img[0]):
                    gr = Rect(r.x0, r.y0, r.x1, r.y1)
                    if gr.area > pw * ph * 0.004:
                        graphic_rects.append(("figure", gr))
            except Exception:
                pass

        draw_rects = []
        for dr in page.get_drawings():
            r = dr["rect"]
            draw_rects.append(Rect(r.x0, r.y0, r.x1, r.y1))
        for gr in _merge_rects(draw_rects, pad=pw * 0.02):
            if gr.area > pw * ph * MIN_GRAPHIC_AREA and gr.w > pw * 0.08 and gr.h > ph * 0.03:
                graphic_rects.append(("figure", gr))

        try:
            for tbl in page.find_tables().tables:
                r = tbl.bbox
                graphic_rects.append(("table", Rect(r[0], r[1], r[2], r[3])))
        except Exception:
            pass

        # 인접·중첩 그래픽 영역을 하나로 묶음 (멀티패널 그림이 조각나지 않도록)
        graphic_regions = _group_graphics(graphic_rects, pw, ph)

        # 2) 텍스트 블록 (그래픽 영역 내부 span 은 제외)
        d = page.get_text("dict")
        text_blocks: list[dict] = []
        text_rects_for_cols: list[Rect] = []
        for blk in d.get("blocks", []):
            if blk.get("type", 0) != 0:
                continue
            br = Rect(*blk["bbox"])
            text_rects_for_cols.append(br)
            # 그래픽 영역 안에 대부분 들어가면 캡션이 아닌 한 건너뜀
            inside = any(g["rect"].intersects(br) and _overlap_ratio(br, g["rect"]) > 0.6 for g in graphic_regions)
            lines_text = []
            max_size = 0.0
            for line in blk.get("lines", []):
                for span in line.get("spans", []):
                    lines_text.append(span["text"])
                    max_size = max(max_size, span["size"])
            raw = " ".join(lines_text)
            text = _clean_text(raw)
            if not text or len(text) < 2:
                continue
            is_caption = bool(CAPTION_RE.match(text))
            if inside and not is_caption:
                continue
            kind = "text"
            if is_caption:
                kind = "caption"
            elif max_size >= body_size * 1.18 and len(text) < 200:
                kind = "heading"
            text_blocks.append({"type": kind, "text": text, "_rect": br, "_size": max_size})

        # 3) 그래픽 영역 -> 이미지로 렌더링
        graphic_blocks: list[dict] = []
        for gi, g in enumerate(graphic_regions):
            r = g["rect"]
            # 페이지 경계로 클램프
            clip = fitz.Rect(max(0, r.x0 - 2), max(0, r.y0 - 2), min(pw, r.x1 + 2), min(ph, r.y1 + 2))
            if clip.width < 8 or clip.height < 8:
                continue
            pix = page.get_pixmap(matrix=fitz.Matrix(ZOOM, ZOOM), clip=clip, alpha=False)
            fname = f"p{pno:03d}_{g['kind']}{gi}.png"
            pix.save(str(assets_dir / fname))
            graphic_blocks.append({
                "type": g["kind"],
                "image": f"assets/{fname}",
                "w": round(clip.width),
                "h": round(clip.height),
                "_rect": Rect(clip.x0, clip.y0, clip.x1, clip.y1),
            })

        # 4) 읽는 순서로 병합
        split_x = _detect_columns(text_rects_for_cols, pw, ph)
        page_blocks = _reading_order(text_blocks + graphic_blocks, split_x, pw)
        for b in page_blocks:
            b.pop("_rect", None)
            b.pop("_size", None)
            b["_page"] = pno
            result.append(b)

    doc.close()
    return _postprocess(result)


def _rect_gap(a: Rect, b: Rect) -> tuple[float, float]:
    """두 사각형 사이의 x/y 방향 간격(겹치면 0)."""
    dx = max(0.0, max(a.x0, b.x0) - min(a.x1, b.x1))
    dy = max(0.0, max(a.y0, b.y0) - min(a.y1, b.y1))
    return dx, dy


def _group_graphics(items: list[tuple[str, Rect]], page_w: float, page_h: float) -> list[dict]:
    """인접(간격이 작은)하거나 겹치는 그래픽을 하나의 영역으로 병합.

    멀티패널 그림(여러 장의 사진이 격자로 배열된 정성 결과 등)이
    여러 조각으로 분리되는 것을 막는다. x/y 양방향 모두 가까울 때만 병합하므로,
    세로로 본문 텍스트를 사이에 둔 별개 그림은 합쳐지지 않는다.
    """
    gx = page_w * 0.045   # 가로 허용 간격
    gy = page_h * 0.035   # 세로 허용 간격
    regions = [{"kind": k, "rect": r} for k, r in items if r.area > 0]

    merged = True
    while merged:
        merged = False
        out: list[dict] = []
        for reg in regions:
            hit = None
            for o in out:
                dx, dy = _rect_gap(reg["rect"], o["rect"])
                same_kind = reg["kind"] == o["kind"]
                if same_kind:
                    # 같은 종류: 간격이 작으면 병합(멀티패널 묶기)
                    close = dx <= gx and dy <= gy
                else:
                    # 다른 종류(그림↔표): 실제로 겹칠 때만 병합
                    close = _overlap_ratio(reg["rect"], o["rect"]) > 0.2
                if close:
                    hit = o
                    break
            if hit is None:
                out.append(reg)
            else:
                hit["rect"] = hit["rect"].union(reg["rect"])
                if reg["kind"] == "table":
                    hit["kind"] = "table"
                merged = True
        regions = out

    # 너무 작은 잔여 영역(구분선·아이콘 등) 제거
    keep = []
    for reg in regions:
        r = reg["rect"]
        if r.area < page_w * page_h * 0.006:
            continue
        if r.h < page_h * 0.02 and r.w < page_w * 0.35:
            continue
        keep.append(reg)
    return keep


def _overlap_ratio(a: Rect, b: Rect) -> float:
    ix0, iy0 = max(a.x0, b.x0), max(a.y0, b.y0)
    ix1, iy1 = min(a.x1, b.x1), min(a.y1, b.y1)
    inter = max(0, ix1 - ix0) * max(0, iy1 - iy0)
    small = min(a.area, b.area) or 1
    return inter / small


def _norm(s: str) -> str:
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", s)).strip().lower()


def _drop_running_headers(blocks: list[dict]) -> list[dict]:
    """여러 페이지에 반복되는 짧은 블록(러닝헤더/푸터)과 페이지 번호만 있는 블록 제거."""
    pages_of: dict[str, set] = {}
    for b in blocks:
        if b["type"] in ("text", "heading") and len(b["text"]) < 120:
            pages_of.setdefault(_norm(b["text"]), set()).add(b.get("_page", -1))
    repeated = {k for k, ps in pages_of.items() if len(ps) >= 2}
    out = []
    for b in blocks:
        txt = b["text"] if b["type"] != "figure" and b["type"] != "table" else ""
        n = _norm(txt)
        if b["type"] in ("text", "heading"):
            if n in repeated:
                continue
            if re.fullmatch(r"[#\s\.\-]+", n):  # 페이지 번호/구분선만
                continue
        out.append(b)
    return out


def _postprocess(blocks: list[dict]) -> list[dict]:
    """러닝헤더 제거 + 연속된 짧은 text 조각 합치기 등 후처리."""
    blocks = _drop_running_headers(blocks)
    for b in blocks:
        b.pop("_page", None)
    out: list[dict] = []
    for b in blocks:
        if (
            b["type"] == "text"
            and out
            and out[-1]["type"] == "text"
            and not out[-1]["text"].rstrip().endswith((".", "?", "!", ":", "”", '"'))
            and len(out[-1]["text"]) < 80
        ):
            out[-1]["text"] = (out[-1]["text"] + " " + b["text"]).strip()
        else:
            out.append(b)
    return out


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


if __name__ == "__main__":
    import json
    import sys

    pdf = sys.argv[1]
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("cache/_test")
    blks = parse_pdf(pdf, out_dir / "assets")
    (out_dir / "blocks.json").write_text(json.dumps(blks, ensure_ascii=False, indent=2), encoding="utf-8")
    kinds = {}
    for b in blks:
        kinds[b["type"]] = kinds.get(b["type"], 0) + 1
    print(f"{len(blks)} blocks:", kinds)
