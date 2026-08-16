// paper-viewer 프런트엔드
// - 좌(영문)/우(한글)를 '행 단위로 짝지어' 렌더링 → 단일 스크롤로 항상 정렬.
// - 문장 단위 span(data-sid)으로 좌우 상호 하이라이트(형광펜).
// - 그림/표 클릭 시 쉬운 설명 말풍선 표시.

const $ = (id) => document.getElementById(id);
const paperSelect = $("paperSelect");
const openBtn = $("openBtn");
const modelTag = $("modelTag");
const progress = $("progress");
const barFill = $("barFill");
const progressMsg = $("progressMsg");
const doc = $("doc");
const colhead = $("colhead");

let papers = [];
let currentEvtSource = null;
let currentPid = null;
let gpuVram = null;

// ---------- 논문 목록 ----------
async function loadPapers() {
  try {
    const r = await fetch("/api/papers");
    const data = await r.json();
    papers = data.papers;
    gpuVram = data.vram || null;
    modelTag.textContent = data.model ? (data.model + (gpuVram ? ` · GPU ${gpuVram}GB` : "")) : "";
    if (data.model_reason) modelTag.title = data.model_reason;
    paperSelect.innerHTML = "";
    if (papers.length === 0) {
      const opt = document.createElement("option");
      opt.textContent = "papers/ 폴더에 PDF 를 넣어주세요";
      opt.disabled = true;
      paperSelect.appendChild(opt);
      openBtn.disabled = true;
      return;
    }
    for (const p of papers) {
      const opt = document.createElement("option");
      opt.value = p.name;
      opt.textContent = (p.cached ? "✓ " : "") + p.name;
      paperSelect.appendChild(opt);
    }
    openBtn.disabled = false;
  } catch (e) {
    progressMsg.textContent = "서버에 연결할 수 없습니다: " + e;
    progress.classList.remove("hidden");
  }
}

// ---------- 처리(파싱+번역) 스트리밍 ----------
function openPaper(name) {
  if (currentEvtSource) currentEvtSource.close();
  progress.classList.remove("hidden");
  barFill.style.width = "0%";
  barFill.style.background = "";
  progressMsg.textContent = "시작 중…";
  openBtn.disabled = true;

  const es = new EventSource("/api/process?path=" + encodeURIComponent(name));
  currentEvtSource = es;

  es.addEventListener("status", (e) => {
    const d = JSON.parse(e.data);
    progressMsg.textContent = d.message;
    if (d.phase === "parse") barFill.style.width = "6%";
    if (d.phase === "parsed") barFill.style.width = "12%";
  });
  es.addEventListener("progress", (e) => {
    const d = JSON.parse(e.data);
    const pct = 12 + Math.round((d.done / d.total) * 88);
    barFill.style.width = pct + "%";
    progressMsg.textContent = `번역 중… ${d.done} / ${d.total} (${pct}%)`;
  });
  es.addEventListener("done", async (e) => {
    const d = JSON.parse(e.data);
    es.close();
    currentEvtSource = null;
    barFill.style.width = "100%";
    progressMsg.textContent = d.cached ? "캐시에서 불러오는 중…" : "완료. 렌더링 중…";
    await renderDoc(d.id);
    progress.classList.add("hidden");
    openBtn.disabled = false;
    loadPapers();
  });
  es.addEventListener("error", (e) => {
    let msg = "오류가 발생했습니다.";
    try { msg = JSON.parse(e.data).message; } catch (_) {}
    progressMsg.textContent = "⚠ " + msg;
    barFill.style.background = "#e0554e";
    es.close();
    currentEvtSource = null;
    openBtn.disabled = false;
  });
}

// ---------- 문서 렌더 ----------
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

async function renderDoc(pid) {
  const r = await fetch("/api/doc/" + pid);
  const data = await r.json();
  document.title = (data.title || "Paper") + " — Paper Viewer";
  // 이 논문이 실제로 번역된 모델을 표시(예: 폴백으로 qwen2.5 사용된 경우)
  if (data.model) {
    modelTag.textContent = data.model + (gpuVram ? ` · GPU ${gpuVram}GB` : "");
    modelTag.title = "이 논문 번역에 사용된 모델: " + data.model;
  }

  const inner = document.createElement("div");
  inner.className = "doc-inner";

  data.blocks.forEach((b, bi) => {
    if (b.type === "figure" || b.type === "table") {
      const row = document.createElement("div");
      row.className = "row " + b.type;
      const wrap = document.createElement("div");
      wrap.className = "figwrap";

      const img = document.createElement("img");
      img.src = "/cache/" + pid + "/" + b.image;
      img.loading = "eager";
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
        close.title = "닫기";
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

    // 참고문헌/하이라이트 제외 블록: 문장 span 없이 플레인 셀 (클릭·형광펜 없음)
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

    // text / heading / caption: 문장 단위 span (좌/우 상호 하이라이트)
    const sentences = b.sentences && b.sentences.length
      ? b.sentences
      : [{ en: b.text || "", ko: b.ko || b.text || "" }];
    const row = document.createElement("div");
    row.className = "row " + b.type;
    row.appendChild(buildCell("en", sentences, bi, "en"));
    row.appendChild(buildCell("ko", sentences, bi, "ko"));
    inner.appendChild(row);
  });

  doc.innerHTML = "";
  doc.appendChild(inner);
  doc.scrollTop = 0;
  colhead.classList.add("show");
  currentPid = pid;
  $("exportBtn").disabled = false;
}

// ---------- 상호작용: 문장 하이라이트 & 그림 설명 ----------
function clearHighlight() {
  document.querySelectorAll(".sent.hl").forEach((s) => s.classList.remove("hl"));
}
function closeAllPops() {
  document.querySelectorAll(".figwrap.open").forEach((w) => w.classList.remove("open"));
}

doc.addEventListener("click", (e) => {
  // 그림/표 설명 닫기 버튼
  if (e.target.closest(".explain-close")) {
    closeAllPops();
    e.stopPropagation();
    return;
  }
  // 그림/표 클릭 → 설명 말풍선 토글
  const wrap = e.target.closest(".figwrap.has-explain");
  if (wrap) {
    const wasOpen = wrap.classList.contains("open");
    closeAllPops();
    clearHighlight();
    if (!wasOpen) wrap.classList.add("open");
    return;
  }
  // 문장 클릭 → 좌/우 동일 문장 하이라이트
  const sent = e.target.closest(".sent");
  if (sent) {
    const sid = sent.dataset.sid;
    const already = sent.classList.contains("hl");
    clearHighlight();
    closeAllPops();
    if (!already) {
      document.querySelectorAll('.sent[data-sid="' + CSS.escape(sid) + '"]').forEach((s) => s.classList.add("hl"));
    }
    return;
  }
  // 빈 곳 클릭 → 모두 해제
  clearHighlight();
  closeAllPops();
});

// ---------- 컨트롤 ----------
openBtn.addEventListener("click", () => {
  const name = paperSelect.value;
  if (name) openPaper(name);
});
$("toggleEn").addEventListener("change", (e) => {
  document.body.classList.toggle("hide-en", !e.target.checked);
});
$("exportBtn").addEventListener("click", () => {
  if (currentPid) window.location.href = "/api/export/" + currentPid;
});
let fontSize = 16;
function applyFont() { document.documentElement.style.setProperty("--fs", fontSize + "px"); }
$("fontPlus").addEventListener("click", () => { fontSize = Math.min(28, fontSize + 1); applyFont(); });
$("fontMinus").addEventListener("click", () => { fontSize = Math.max(11, fontSize - 1); applyFont(); });

// URL 파라미터로 캐시된 논문 바로 열기: /?doc=<id>&block=<n>
const params = new URLSearchParams(location.search);
const directDoc = params.get("doc");
loadPapers().then(async () => {
  if (!directDoc) return;
  await renderDoc(directDoc);
  const imgs = [...doc.querySelectorAll("img")];
  await Promise.all(imgs.map((im) => im.complete ? 1 : new Promise((r) => { im.onload = im.onerror = r; })));
  const rows = doc.querySelectorAll(".row");

  const bi = params.get("block");
  if (bi !== null && rows[parseInt(bi)]) rows[parseInt(bi)].scrollIntoView({ block: "start" });

  // 딥링크: 특정 문장 하이라이트 (?hl=blockIdx-sentIdx)
  const hl = params.get("hl");
  if (hl) {
    document.querySelectorAll('.sent[data-sid="' + CSS.escape(hl) + '"]').forEach((s) => s.classList.add("hl"));
    const first = document.querySelector('.sent[data-sid="' + CSS.escape(hl) + '"]');
    if (first) first.scrollIntoView({ block: "center" });
  }
  // 딥링크: 특정 그림/표 설명 열기 (?open=blockIdx)
  const op = params.get("open");
  if (op !== null && rows[parseInt(op)]) {
    const w = rows[parseInt(op)].querySelector(".figwrap.has-explain");
    if (w) { w.classList.add("open"); w.scrollIntoView({ block: "center" }); }
  }
});
