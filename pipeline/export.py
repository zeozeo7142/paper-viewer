"""번역된 논문을 '단일 HTML 파일'로 내보내기.

결과물은 서버·GPU·인터넷 없이 어떤 기기(노트북, 아이패드, 안드로이드 등)의
브라우저에서든 열 수 있는 자체 완결형 파일이다. 그림/표 이미지는 base64 로 내장되고,
번역 데이터와 뷰어(렌더링·문장 하이라이트·설명 말풍선)가 모두 파일 안에 들어간다.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

# 자체 완결형 뷰어 로직(서버 호출 없음). window.__DOC__ 를 렌더링.
_VIEWER_JS = r"""
const DOC = window.__DOC__;
const docEl = document.getElementById("doc");

function buildCell(cls, sentences, bi, side) {
  const cell = document.createElement("div");
  cell.className = "cell " + cls;
  sentences.forEach((s, si) => {
    const span = document.createElement("span");
    span.className = "sent";
    span.dataset.sid = bi + "-" + si;
    span.textContent = (side === "en" ? s.en : s.ko) + " ";
    cell.appendChild(span);
  });
  return cell;
}

function render() {
  const inner = document.createElement("div");
  inner.className = "doc-inner";
  DOC.blocks.forEach((b, bi) => {
    if (b.type === "figure" || b.type === "table") {
      const row = document.createElement("div");
      row.className = "row " + b.type;
      const wrap = document.createElement("div");
      wrap.className = "figwrap";
      const img = document.createElement("img");
      img.src = b.image;  // data URI (내장)
      wrap.appendChild(img);
      const tag = document.createElement("div");
      tag.className = "figtag";
      tag.textContent = b.type === "table" ? "TABLE" : "FIGURE";
      wrap.appendChild(tag);
      if (b.explain) {
        wrap.classList.add("has-explain");
        const hint = document.createElement("div");
        hint.className = "explain-hint";
        hint.textContent = "💡 클릭하면 쉬운 설명 보기";
        wrap.appendChild(hint);
        const pop = document.createElement("div");
        pop.className = "explain-pop";
        const close = document.createElement("button");
        close.className = "explain-close";
        close.textContent = "✕";
        const body = document.createElement("div");
        body.className = "explain-body";
        body.textContent = b.explain;
        pop.appendChild(close);
        pop.appendChild(body);
        wrap.appendChild(pop);
      }
      row.appendChild(wrap);
      inner.appendChild(row);
      return;
    }
    if (b.nohl || b.type === "reference") {
      const row = document.createElement("div");
      row.className = "row " + b.type + " nohl";
      const en = document.createElement("div");
      en.className = "cell en";
      en.textContent = b.text || "";
      const ko = document.createElement("div");
      ko.className = "cell ko";
      ko.textContent = b.ko || b.text || "";
      row.appendChild(en);
      row.appendChild(ko);
      inner.appendChild(row);
      return;
    }
    const sentences = (b.sentences && b.sentences.length)
      ? b.sentences : [{ en: b.text || "", ko: b.ko || b.text || "" }];
    const row = document.createElement("div");
    row.className = "row " + b.type;
    row.appendChild(buildCell("en", sentences, bi, "en"));
    row.appendChild(buildCell("ko", sentences, bi, "ko"));
    inner.appendChild(row);
  });
  docEl.innerHTML = "";
  docEl.appendChild(inner);
}

function clearHighlight() { document.querySelectorAll(".sent.hl").forEach((s) => s.classList.remove("hl")); }
function closeAllPops() { document.querySelectorAll(".figwrap.open").forEach((w) => w.classList.remove("open")); }

docEl.addEventListener("click", (e) => {
  if (e.target.closest(".explain-close")) { closeAllPops(); e.stopPropagation(); return; }
  const wrap = e.target.closest(".figwrap.has-explain");
  if (wrap) { const was = wrap.classList.contains("open"); closeAllPops(); clearHighlight(); if (!was) wrap.classList.add("open"); return; }
  const sent = e.target.closest(".sent");
  if (sent) {
    const sid = sent.dataset.sid; const on = sent.classList.contains("hl");
    clearHighlight(); closeAllPops();
    if (!on) document.querySelectorAll('.sent[data-sid="' + CSS.escape(sid) + '"]').forEach((s) => s.classList.add("hl"));
    return;
  }
  clearHighlight(); closeAllPops();
});

document.getElementById("toggleEn").addEventListener("change", (e) => {
  document.body.classList.toggle("hide-en", !e.target.checked);
});
let fontSize = 16;
function applyFont() { document.documentElement.style.setProperty("--fs", fontSize + "px"); }
document.getElementById("fontPlus").addEventListener("click", () => { fontSize = Math.min(28, fontSize + 1); applyFont(); });
document.getElementById("fontMinus").addEventListener("click", () => { fontSize = Math.max(11, fontSize - 1); applyFont(); });

render();
"""


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_standalone_html(doc_dir: Path, css_path: Path, title: str | None = None) -> str:
    doc_dir = Path(doc_dir)
    doc = json.loads((doc_dir / "doc.json").read_text(encoding="utf-8"))

    # 그림/표 이미지를 base64 data URI 로 내장
    for b in doc["blocks"]:
        img = b.get("image")
        if img:
            p = doc_dir / img
            if p.exists():
                data = base64.b64encode(p.read_bytes()).decode("ascii")
                b["image"] = "data:image/png;base64," + data

    css = css_path.read_text(encoding="utf-8")
    title = title or doc.get("title", "Paper")
    model = _html_escape(doc.get("model", "") or "")
    model_badge = f'<span class="model-tag" title="이 논문 번역에 사용된 모델">{model}</span>' if model else ""
    # </script> 가 데이터 안에 들어가 스크립트를 조기 종료시키는 것 방지
    doc_json = json.dumps(doc, ensure_ascii=False).replace("</", "<\\/")

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>{_html_escape(title)}</title>
<style>
{css}
/* 단일 파일용: 하단 여백 축소 */
.doc-inner {{ padding: 8px 0 20vh; }}
</style>
</head>
<body>
<header class="toolbar">
  <div class="tb-left"><strong class="brand">📄 {_html_escape(title[:80])}</strong>{model_badge}</div>
  <div class="tb-right">
    <label class="chk"><input type="checkbox" id="toggleEn" checked /> 영문 보기</label>
    <div class="fontctl"><button id="fontMinus" title="글자 작게">A−</button><button id="fontPlus" title="글자 크게">A+</button></div>
  </div>
</header>
<div class="colhead show"><div class="colhead-inner"><span class="ch ch-en">English</span><span class="ch ch-ko">한국어</span></div></div>
<main id="doc" class="doc"></main>
<script>window.__DOC__ = {doc_json};</script>
<script>{_VIEWER_JS}</script>
</body>
</html>
"""


def export_paper(doc_dir: Path, out_path: Path, css_path: Path) -> Path:
    html = build_standalone_html(doc_dir, css_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path


if __name__ == "__main__":
    import sys

    root = Path(__file__).resolve().parent.parent
    pid = sys.argv[1]
    dd = root / "cache" / pid
    doc = json.loads((dd / "doc.json").read_text(encoding="utf-8"))
    safe = re.sub(r"[^\w가-힣 .-]", "_", (doc.get("title") or pid))[:60].strip() or pid
    out = root / "exports" / f"{safe}.html"
    export_paper(dd, out, root / "web" / "style.css")
    print("wrote", out, f"({out.stat().st_size // 1024} KB)")
