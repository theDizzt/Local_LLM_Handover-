// [읽기 안내] 기존 REST API를 사용하는 최소 검토 화면이다. 문서 내용은 서버에 저장하고
// URL에는 선택 문서 ID만 둔다. 새로고침 시 서버의 최신 작업으로 상태를 복원한다.
// 화면 요청의 세대 번호(ticket)를 비교하여 이전 문서의 늦은 응답이 새 선택을 덮지 않게 한다.
"use strict";
const $ = (id) => document.getElementById(id);
const state = { document: null, job: null, page: null, pageIndex: 0, offset: 0,
  ticket: 0, pageTicket: 0, listTicket: 0, timer: null, starting: false, loading: false,
  structure: null, building: false, chunkTicket: 0, chunkOffset: 0 };
const names = { pending: "OCR 대기", ready: "결과 있음", failed: "처리 실패" };
const regionNames = { ocr_text: "원문", heading: "제목 후보", paragraph: "본문", table_candidate: "표 후보", unknown: "판별 보류" };
const reviewNames = { unreviewed: "미검토", confirmed: "검토 완료", needs_correction: "수정 필요" };
const draftCategories = {overview: "업무 개요", prerequisites: "사전 조건", procedure: "절차", cautions: "주의사항", troubleshooting: "문제 해결"};
const draftErrors = {model_not_found: "설정한 Ollama 모델이 없습니다.", model_unavailable: "로컬 Ollama 연결을 확인해 주세요.",
  model_timeout: "모델 응답 시간이 초과됐습니다.", invalid_model_output: "모델 응답의 형식 또는 근거 ID를 확인할 수 없습니다.",
  no_eligible_evidence: "조건에 맞는 근거가 없습니다. 검색어와 페이지 검토 상태를 확인해 주세요.",
  no_relevant_evidence: "모델이 관련된 원문을 선택하지 못했습니다. 검색어와 근거를 확인해 주세요.",
  source_changed: "생성 중 OCR 또는 근거 버전이 변경됐습니다.", review_changed: "생성 중 검토 기록이 변경됐습니다.",
  interrupted: "서버 재시작으로 생성이 중단됐습니다.", search_unavailable: "검색 색인을 사용할 수 없습니다."};
let draftTicket = 0, draftTimer = null, draftBusy = false;
function resetDrafts() {
  ++draftTicket; clearTimeout(draftTimer); draftBusy = false;
  $("draft-history").replaceChildren(); $("draft-result").replaceChildren();
  $("draft-status").textContent = "생성 기록을 확인합니다.";
}
function renderDraft(job) {
  const panel = $("draft-result"); panel.replaceChildren();
  if (!job.result) return;
  const title = document.createElement("h3"); title.textContent = `${job.result.title} · 검토 필요`;
  const note = document.createElement("p"); note.textContent = job.result.notice + " 생성 당시 근거를 보존한 기록이며 현재 OCR·검토 결과와 다를 수 있습니다.";
  const missing = document.createElement("p"); missing.textContent = "미포함 항목: " + (job.result.missing_categories.map(key => draftCategories[key]).join(", ") || "없음 (내용의 완전성을 보장하지 않습니다.)");
  panel.append(title, note, missing);
  for (const item of job.result.items) {
    const article = document.createElement("article"), heading = document.createElement("strong"), text = document.createElement("p"), link = document.createElement("a"), review = document.createElement("p");
    heading.textContent = draftCategories[item.category]; text.textContent = item.block.text;
    link.textContent = `${item.evidence.pdf_page_number}페이지 원문 이미지 열기 ↗`;
    link.href = `/api/v1/documents/${encodeURIComponent(job.document_id)}/pages/${encodeURIComponent(item.page_id)}/image`;
    link.target = "_blank"; link.rel = "noopener";
    review.className = "muted small";
    review.textContent = `생성 당시 ${item.human_review ? reviewNames[item.human_review.status] : "사람 검토 기록 없음"} · OCR 신뢰도 ${(item.block.confidence * 100).toFixed(1)}%`;
    if (item.review_reasons.length) review.textContent += " · " + item.review_reasons.map(key => layoutWarnings[key] || key).join(" ");
    article.append(heading, text, link, review); panel.append(article);
  }
}
async function loadDrafts() {
  if (!state.document) return;
  const ticket = ++draftTicket; clearTimeout(draftTimer);
  try {
    const rows = await api(`${docPath()}/drafts?limit=10`);
    if (ticket !== draftTicket) return;
    draftBusy = rows.some(row => ["queued", "running"].includes(row.status));
    $("draft-history").replaceChildren();
    const names = {queued: "대기", running: "생성 중", ready: "완료·검토 필요", failed: "실패"};
    for (const job of rows) {
      const button = document.createElement("button"); button.type = "button"; button.className = "quiet";
      button.textContent = `${job.request.title} · ${names[job.status]} · ${job.created_at} UTC`;
      button.onclick = () => {
        $("draft-status").textContent = job.status === "failed" ? (draftErrors[job.error_code] || "생성에 실패했습니다. 다시 요청해 주세요.") : names[job.status];
        renderDraft(job);
      };
      $("draft-history").append(button);
    }
    const latest = rows[0];
    $("draft-status").textContent = !latest ? "최근 생성 기록이 없습니다." : latest.status === "failed"
      ? (draftErrors[latest.error_code] || "생성에 실패했습니다. 다시 요청해 주세요.")
      : `최근 초안: ${names[latest.status]}`;
    if (latest) renderDraft(latest);
    controls();
    if (draftBusy) draftTimer = setTimeout(loadDrafts, 1200);
  } catch (error) {
    if (ticket === draftTicket) $("draft-status").textContent = message(error);
  }
}
$("draft-refresh").onclick = loadDrafts;
$("draft-form").onsubmit = async event => {
  event.preventDefault();
  if (!state.structure || draftBusy) return;
  const ticket = draftTicket;
  draftBusy = true; controls(); $("draft-status").textContent = "생성을 요청합니다…";
  try {
    await api(`${docPath()}/drafts`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
      structure_id: state.structure.structure_id, title: $("draft-name").value, query: $("draft-query").value,
      reviewed_only: $("draft-reviewed").checked, top_k: 10,
    })});
    if (ticket === draftTicket) await loadDrafts();
  } catch (error) {
    if (ticket === draftTicket) { draftBusy = false; controls(); $("draft-status").textContent = message(error); }
  }
};
let reviewTicket = 0, pageReview = null;
function resetReview() {
  ++reviewTicket;
  pageReview = null;
  $("review-fields").disabled = true;
  $("review-summary").textContent = "";
  $("review-history").replaceChildren();
  $("review-note").value = "";
  for (const key of ["text", "order", "regions"]) $("review-" + key).checked = false;
}
function reviewPath() {
  return `${docPath()}/layout/${encodeURIComponent(state.structure.structure_id)}`;
}
async function loadReview() {
  if (!layoutEnabled() || !state.structure || !state.page) return;
  const ticket = ++reviewTicket, path = reviewPath(), pageId = state.page.page_id;
  $("review-fields").disabled = true;
  $("human-review-status").textContent = "검토 기록을 불러오는 중입니다…";
  try {
    const [review, summary, history] = await Promise.all([
      api(`${path}/pages/${pageId}/review`), api(`${path}/reviews`),
      api(`${path}/pages/${pageId}/review/history?limit=10`),
    ]);
    if (ticket !== reviewTicket) return;
    pageReview = review;
    $("human-review-status").textContent = `${reviewNames[review.status]} · 기록 ${review.revision}${review.updated_at ? ` · ${review.updated_at} UTC` : ""}`;
    $("review-summary").textContent = `전체 ${summary.total}페이지 · 검토 완료 ${summary.confirmed} · 수정 필요 ${summary.needs_correction} · 미검토 ${summary.unreviewed}`;
    $("review-note").value = review.note;
    for (const key of ["text", "order", "regions"]) $("review-" + key).checked = review[key + "_checked"];
    $("review-fields").disabled = false;
    $("review-history").textContent = history.length ? history.map(item =>
      `기록 ${item.revision} · ${reviewNames[item.status]} · ${item.updated_at} UTC\n${item.note || "메모 없음"}`
    ).join("\n\n") : "아직 저장한 검토 기록이 없습니다.";
  } catch (error) {
    if (ticket === reviewTicket) $("human-review-status").textContent = message(error);
  }
}
async function saveReview(status) {
  if (!pageReview || $("review-fields").disabled) return;
  const ticket = reviewTicket, body = { expected_revision: pageReview.revision, status, note: $("review-note").value };
  for (const key of ["text", "order", "regions"]) body[key + "_checked"] = $("review-" + key).checked;
  if (status === "confirmed" && !(body.text_checked && body.order_checked && body.regions_checked)) {
    $("human-review-status").textContent = "세 가지 원문 대조 항목을 모두 확인해 주세요."; return;
  }
  if (status === "needs_correction" && !body.note.trim()) {
    $("human-review-status").textContent = "수정이 필요한 내용을 메모에 남겨 주세요."; return;
  }
  $("review-fields").disabled = true;
  try {
    await api(`${reviewPath()}/pages/${state.page.page_id}/review`, {
      method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
    });
    if (ticket !== reviewTicket) return;
    // 저장된 검토 상태가 바뀌었으므로 화면의 이전 검색 결과도 폐기한다.
    // 재색인은 필요 없다. 다음 검색은 SQLite의 최신 검토 기록을 조회한다.
    resetSearch(); refreshIndex();
    await loadReview();
  } catch (error) {
    if (ticket !== reviewTicket) return;
    $("human-review-status").textContent = `${message(error)} 입력은 보존했습니다. ‘저장 상태 다시 불러오기’로 확인하세요.`;
    $("review-fields").disabled = false;
  }
}
$("review-confirm").onclick = () => saveReview("confirmed");
$("review-correct").onclick = () => saveReview("needs_correction");
$("review-reopen").onclick = () => saveReview("unreviewed");
$("review-reload").onclick = loadReview;
const layoutWarnings = {
  heuristic_layout: "좌표 규칙에 따른 제안입니다. 실제 제목·읽기 순서를 원문과 비교해 주세요.",
  low_ocr_confidence: "낮은 OCR 신뢰도의 문장이 포함되어 있습니다.",
  empty_page: "인식된 글자가 없습니다. 빈 페이지인지 확인해 주세요.",
  overlapping_boxes: "겹치는 원문 영역이 있어 OCR 순서를 유지했습니다.",
  complex_page: "영역이 너무 많아 OCR 순서를 유지했습니다.",
  heading_candidate: "글자 높이로 추정한 제목을 확인해 주세요.",
  table_cells_unverified: "표 후보입니다. 병합 셀·행/열 경계는 검증하지 않았습니다.",
  table_or_columns: "짧은 2열은 표와 다단을 구분할 수 없어 OCR 순서를 유지했습니다.",
  ambiguous_reading_order: "배치가 불명확하여 OCR 순서를 유지했습니다.",
  column_order_candidate: "왼쪽 열 다음 오른쪽 열로 읽도록 제안했습니다.",
};
function layoutEnabled() { return $("layout-mode").value === "geometry"; }
function structureEndpoint() { return layoutEnabled() ? "layout" : "structure"; }
const errors = { interrupted: "서버 재시작으로 작업이 중단되었습니다. 다시 실행해 주세요.",
  ocr_unavailable: "OCR 엔진을 사용할 수 없습니다. 설치와 모델 설정을 확인한 뒤 다시 실행해 주세요.",
  processing_failed: "문서 처리에 실패했습니다. 원본 PDF를 확인한 뒤 다시 실행해 주세요." };
// 검색 응답도 별도 세대 번호로 보호한다. 문서·페이지·정리 방식이 바뀌면 이전
// 검색/진행률 응답을 버린다. 결과의 근거 버전을 확인한 후에만 원문을 강조한다.
let searchTicket = 0, indexTimer = null;
function resetSearch() {
  resetCleanup();
  ++searchTicket;
  clearTimeout(indexTimer);
  $("search-results").replaceChildren();
  $("build-index").disabled = $("search-submit").disabled = true;
  $("index-status").textContent = "근거 준비 후 사용할 수 있습니다.";
}
let cleanupTicket = 0;
function resetCleanup() {
  ++cleanupTicket;
  $("cleanup-candidates").replaceChildren();
  $("cleanup-status").textContent = "";
  $("cleanup-execute").disabled = true;
  $("cleanup-preview").disabled = false;
}
async function previewCleanup() {
  if (!state.document) { $("cleanup-status").textContent = "문서를 먼저 선택해 주세요."; return; }
  const ticket = ++cleanupTicket;
  $("cleanup-preview").disabled = true;
  $("cleanup-execute").disabled = true;
  $("cleanup-candidates").replaceChildren();
  try {
    const rows = await api(`${docPath()}/search-index/cleanup?limit=21`);
    if (ticket !== cleanupTicket) return;
    $("cleanup-status").textContent = rows.length ? `정리 가능한 이전 색인 ${Math.min(rows.length, 20)}개${rows.length > 20 ? " · 추가 대상은 정리 후 다시 조회하세요." : ""}` : "정리할 이전 색인이 없습니다.";
    const reasons = {old_ocr: "이전 OCR 버전", failed_run: "실패한 색인", replaced_run: "재구축으로 교체됨"};
    for (const row of rows.slice(0, 20)) {
      const label = document.createElement("label"), checkbox = document.createElement("input");
      checkbox.type = "checkbox"; checkbox.value = row.run_id;
      checkbox.onchange = () => { $("cleanup-execute").disabled = !$("cleanup-candidates").querySelector("input:checked"); };
      label.append(checkbox, document.createTextNode(` ${row.created_at} UTC · ${reasons[row.reason]} · 근거 ${row.completed}개${row.cleanup_status === "failed" ? " · 정리 재시도" : ""}`));
      $("cleanup-candidates").append(label);
    }
  } catch (error) {
    if (ticket === cleanupTicket) $("cleanup-status").textContent = message(error);
  } finally {
    if (ticket === cleanupTicket) $("cleanup-preview").disabled = false;
  }
}
$("cleanup-preview").onclick = previewCleanup;
$("cleanup-execute").onclick = async () => {
  const runIds = [...$("cleanup-candidates").querySelectorAll("input:checked")].map(input => input.value);
  if (!state.document || !runIds.length) return;
  const ticket = ++cleanupTicket;
  $("cleanup-execute").disabled = $("cleanup-preview").disabled = true;
  $("cleanup-candidates").querySelectorAll("input").forEach(input => { input.disabled = true; });
  $("cleanup-status").textContent = "선택한 이전 색인을 정리하는 중입니다…";
  try {
    const results = await api(`${docPath()}/search-index/cleanup`, {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({run_ids: runIds}),
    });
    if (ticket !== cleanupTicket) return;
    const count = status => results.filter(item => item.status === status).length;
    $("cleanup-status").textContent = `삭제 완료 ${count("deleted")}개 · 보호됨 ${count("protected")}개 · 처리 중 ${count("busy")}개 · 실패 ${count("failed")}개. 대상을 다시 확인할 수 있습니다.`;
    $("cleanup-candidates").replaceChildren();
  } catch (error) {
    if (ticket === cleanupTicket) $("cleanup-status").textContent = `${message(error)} 정리 대상을 다시 조회해 주세요.`;
  } finally {
    if (ticket === cleanupTicket) $("cleanup-preview").disabled = false;
  }
};
async function refreshIndex() {
  if (!state.structure) return;
  const ticket = searchTicket, path = docPath(), structureId = state.structure.structure_id;
  clearTimeout(indexTimer);
  try {
    const result = await api(`${path}/search-index?structure_id=${encodeURIComponent(structureId)}`);
    if (ticket !== searchTicket || structureId !== state.structure?.structure_id) return;
    const running = ["queued", "running"].includes(result.latest?.status);
    $("build-index").disabled = running;
    $("search-submit").disabled = !result.active;
    $("index-status").textContent = running
      ? `검색 색인 준비 중 · ${result.latest.completed} / ${result.latest.total}`
      : result.latest?.status === "failed"
        ? `색인 준비 실패 (${result.latest.error_code}). 다시 준비해 주세요.${result.active ? " 기존 색인은 검색 가능합니다." : ""}`
        : result.active ? "검색 준비 완료" : "검색 색인을 준비해 주세요.";
    if (running) indexTimer = setTimeout(refreshIndex, 1000);
  } catch (error) {
    if (ticket === searchTicket) $("index-status").textContent = error.message;
  }
}
$("build-index").onclick = async () => {
  if (!state.structure) return;
  const ticket = searchTicket;
  $("build-index").disabled = true;
  try {
    await api(`${docPath()}/search-index`, {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({structure_id: state.structure.structure_id})});
    if (ticket === searchTicket) refreshIndex();
  } catch (error) {
    if (ticket === searchTicket) { $("index-status").textContent = error.message; $("build-index").disabled = false; }
  }
};
$("search-form").onsubmit = async (event) => {
  event.preventDefault();
  if (!state.structure) return;
  const ticket = searchTicket, structureId = state.structure.structure_id;
  $("search-submit").disabled = true;
  $("search-results").textContent = "검색 중…";
  try {
    const result = await api(`${docPath()}/search`, {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({structure_id: structureId, query: $("search-query").value})});
    if (ticket !== searchTicket) return;
    $("search-results").replaceChildren();
    if (!result.hits.length) $("search-results").textContent = "검색 결과가 없습니다.";
    for (const hit of result.hits) {
      const button = document.createElement("button"), chunk = hit.chunk;
      button.type = "button"; button.className = "chunk";
      button.textContent = `${chunk.evidence.pdf_page_number}페이지${hit.review_required ? " · 배치 분석 검토 필요" : ""}\n${chunk.text}`;
      if (hit.review_reasons?.length) button.textContent += "\n" + hit.review_reasons.map(reason => layoutWarnings[reason] || reason).join(" ");
      if (hit.human_review) button.textContent += `\n사람 검토: ${reviewNames[hit.human_review.status]}${hit.human_review.note ? ` · ${hit.human_review.note}` : ""}`;
      button.onclick = async () => {
        if (ticket !== searchTicket || state.document.ingestion_id !== chunk.evidence.ingestion_id) return;
        state.pageIndex = chunk.evidence.pdf_page_number - 1;
        await loadPage();
        if (state.page?.page_id === chunk.page_id) highlightSources(chunk.sources);
      };
      $("search-results").append(button);
    }
  } catch (error) {
    if (ticket === searchTicket) $("search-results").textContent = error.message;
  } finally {
    if (ticket === searchTicket) $("search-submit").disabled = false;
  }
};
function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").hidden = !message;
  $("notice").className = error ? "error" : "";
}
async function api(path, options = {}) {
  // 요청이 영원히 대기하지 않게 제한한다. POST 실패 시 서버에서 등록/실행됐을 수도
  // 있으므로 자동 재전송하지 않는다. 최신 상태 조회 후 사용자가 재시도한다.
  const response = await fetch(`/api/v1${path}`, { ...options, signal: AbortSignal.timeout(60000) });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(typeof body?.detail === "string" ? body.detail : `요청 실패 (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return body;
}
function message(error) {
  return error.name === "TimeoutError" || error instanceof TypeError
    ? "서버에 연결할 수 없거나 응답이 지연되고 있습니다. 새로고침으로 상태를 확인해 주세요." : error.message;
}
function docPath(id = state.document.document_id) { return `/documents/${encodeURIComponent(id)}`; }
function active() { return ["queued", "running"].includes(state.job?.status); }
function controls() {
  $("draft-create").disabled = !state.structure || draftBusy || state.loading || state.starting || active();
  $("start-ocr").disabled = !state.document || state.loading || state.starting || active();
  $("start-ocr").textContent = state.starting ? "요청 중…" : active() ? "OCR 처리 중" : state.job ? "OCR 다시 실행" : "OCR 시작";
  $("page-prev").disabled = !state.page || state.pageIndex === 0;
  $("page-next").disabled = !state.page || state.pageIndex + 1 >= state.document.page_count;
  $("build-structure").disabled = state.document?.status !== "ready" || state.loading || state.building || state.starting || active();
  $("build-structure").textContent = state.building ? "준비 중…" : state.structure ? "근거 확인" : "근거 준비";
  $("layout-mode").disabled = state.building;
}
async function listDocuments() {
  const ticket = ++state.listTicket;
  $("docs-prev").disabled = $("docs-next").disabled = true;
  try {
    // 1개를 더 가져와 다음 페이지 유무를 판단한다. 전체 목록을 메모리에 쌓지 않는다.
    const rows = await api(`/documents?limit=11&offset=${state.offset}`);
    if (ticket !== state.listTicket) return;
    $("documents").replaceChildren();
    for (const document of rows.slice(0, 10)) {
      const button = documentElement(document);
      $("documents").append(button);
    }
    if (!rows.length) $("documents").textContent = "등록된 문서가 없습니다. PDF를 등록해 주세요.";
    $("docs-range").textContent = rows.length ? `${state.offset + 1}–${state.offset + Math.min(10, rows.length)}` : "0개";
    $("docs-prev").disabled = state.offset === 0;
    $("docs-next").disabled = rows.length <= 10;
    return true;
  } catch (error) {
    if (ticket !== state.listTicket) return;
    notice(message(error), true);
    $("docs-prev").disabled = state.offset === 0;
    return false;
  }
}
function documentElement(doc) {
  const button = document.createElement("button");
  button.className = "doc";
  button.dataset.id = doc.document_id;
  button.setAttribute("aria-pressed", String(doc.document_id === state.document?.document_id));
  const title = document.createElement("strong");
  const meta = document.createElement("small");
  // 파일명과 OCR 결과는 신뢰할 수 없는 입력이다. innerHTML로 삽입하지 않는다.
  title.textContent = doc.file_name;
  meta.textContent = `${doc.page_count}페이지 · ${names[doc.status]}`;
  button.append(title, meta);
  button.onclick = () => selectDocument(doc.document_id);
  return button;
}
function clearPage(text) {
  resetReview();
  resetSearch();
  state.page = null;
  ++state.chunkTicket;
  state.chunkOffset = 0;
  $("chunks").textContent = "페이지를 선택하고 근거를 준비해 주세요.";
  $("chunks-more").hidden = true;
  $("layout-panel").hidden = true;
  $("layout-regions").replaceChildren();
  ++state.pageTicket;
  $("image-stage").hidden = true;
  $("page-image").removeAttribute("src");
  $("original-image").hidden = true;
  $("original-image").removeAttribute("href");
  $("image-empty").hidden = false;
  $("image-empty").textContent = text;
  $("highlights").replaceChildren();
  $("blocks").textContent = text;
  $("block-count").textContent = "";
  $("page-label").textContent = "페이지 없음";
  controls();
}
async function selectDocument(id) {
  resetDrafts();
  clearTimeout(state.timer);
  const ticket = ++state.ticket;
  state.document = state.job = null;
  state.structure = null;
  state.building = false;
  $("structure-status").textContent = "OCR 완료 후 페이지별 근거를 준비할 수 있습니다.";
  state.starting = false;
  state.loading = true;
  state.pageIndex = 0;
  clearPage("문서를 불러오는 중입니다…");
  $("review-title").textContent = "문서 불러오는 중…";
  $("document-meta").textContent = "";
  $("job-status").textContent = "처리 상태 확인 중…";
  $("job-progress").hidden = true;
  notice("");
  try {
    const [doc, job] = await Promise.all([api(docPath(id)), api(`${docPath(id)}/ocr/latest`)]);
    if (ticket !== state.ticket) return;
    state.document = doc;
    loadDrafts();
    state.job = job;
    state.loading = false;
    const url = new URL(location.href);
    url.searchParams.set("document", id);
    history.replaceState(null, "", url);
    $("review-title").textContent = doc.file_name;
    $("document-meta").textContent = `${doc.page_count}페이지 · ${names[doc.status]} · 원문과 숫자·누락·읽기 순서를 확인하세요.`;
    document.querySelectorAll(".doc").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.id === id)));
    showJob();
    await loadPage();
    if (ticket === state.ticket && active()) schedulePoll(ticket);
  } catch (error) {
    if (ticket !== state.ticket) return;
    state.loading = false;
    $("review-title").textContent = "문서를 불러오지 못했습니다";
    $("job-status").textContent = "새로고침으로 다시 확인해 주세요.";
    clearPage("문서 조회에 실패했습니다.");
    notice(message(error), true);
  }
}
function showJob() {
  const job = state.job;
  let text = "아직 OCR을 실행하지 않았습니다. ‘OCR 시작’을 눌러 주세요.";
  if (active()) text = `${job.status === "queued" ? "처리 대기 중" : "텍스트 인식 중"} · ${job.completed_pages} / ${job.total_pages}페이지\n첫 실행은 모델 준비에 시간이 걸릴 수 있습니다.`;
  else if (job?.status === "completed") text = `OCR 완료 · ${job.completed_pages}페이지. 인식된 내용을 원문과 비교해 주세요.`;
  else if (job?.status === "failed") text = errors[job.error_code] || "OCR 작업에 실패했습니다. 다시 실행해 주세요.";
  if (state.document.status === "ready" && job && job.ingestion_id !== state.document.ingestion_id)
    text += "\n아래에는 이전에 성공한 결과를 표시합니다.";
  $("job-status").textContent = text;
  $("job-progress").hidden = !active();
  $("job-progress").value = job ? 100 * job.completed_pages / job.total_pages : 0;
  controls();
}
function schedulePoll(ticket) {
  // setInterval과 달리 응답이 끝난 뒤 다음 조회를 예약하여 느린 서버에도 요청이 겹치지 않는다.
  state.timer = setTimeout(() => poll(ticket), 1500);
}
async function poll(ticket) {
  if (ticket !== state.ticket) return;
  try {
    const job = await api(`${docPath()}/ocr/latest`);
    if (ticket !== state.ticket) return;
    state.job = job;
    showJob();
    if (active()) schedulePoll(ticket);
    else { await selectDocument(state.document.document_id); await listDocuments(); }
  } catch (error) {
    if (ticket !== state.ticket) return;
    $("job-status").textContent = `${message(error)} 자동으로 다시 확인합니다.`;
    state.timer = setTimeout(() => poll(ticket), 5000);
  }
}
async function loadPage() {
  const ticket = state.ticket;
  if (state.document.status !== "ready") { clearPage("OCR 완료 후 결과를 확인할 수 있습니다."); return; }
  clearPage("페이지를 불러오는 중입니다…");
  const pageTicket = state.pageTicket;
  try {
    // ingestion_id를 고정해야 검토 도중 재처리가 완료되어도 페이지 버전이 섞이지 않는다.
    const pages = await api(`${docPath()}/pages?limit=1&offset=${state.pageIndex}&ingestion_id=${encodeURIComponent(state.document.ingestion_id)}`);
    if (ticket !== state.ticket || pageTicket !== state.pageTicket) return;
    if (!pages.length) { clearPage("이 버전에 조회 가능한 페이지가 없습니다."); return; }
    state.page = pages[0];
    $("page-label").textContent = `${state.page.pdf_page_number} / ${state.document.page_count}`;
    const img = $("page-image");
    img.alt = `${state.document.file_name} ${state.page.pdf_page_number}페이지 원문`;
    img.onload = () => {
      if (pageTicket !== state.pageTicket || ticket !== state.ticket) return;
      $("image-empty").hidden = true;
      $("image-stage").hidden = false;
    };
    img.onerror = () => {
      if (pageTicket !== state.pageTicket || ticket !== state.ticket) return;
      $("image-stage").hidden = true;
      $("image-empty").hidden = false;
      $("image-empty").textContent = "이미지를 불러오지 못했습니다. 새로고침해 주세요.";
    };
    img.src = `/api/v1${docPath()}/pages/${encodeURIComponent(state.page.page_id)}/image`;
    // 작은 화면이나 A4 문서의 세부 숫자는 브라우저 기본 이미지 확대 기능으로 검토한다.
    $("original-image").href = img.src;
    $("original-image").hidden = false;
    renderBlocks();
    controls();
    await loadChunks();
  } catch (error) {
    if (ticket === state.ticket && pageTicket === state.pageTicket) { clearPage("페이지 조회 실패 · 새로고침해 주세요."); notice(message(error), true); }
  }
}
function renderBlocks() {
  $("blocks").replaceChildren();
  $("highlights").replaceChildren();
  if (!state.page) return;
  const blocks = state.page.blocks.filter((block) => !$("low-only").checked || block.confidence < 0.8);
  $("block-count").textContent = `${blocks.length} / ${state.page.blocks.length}개`;
  if (!blocks.length) $("blocks").textContent = state.page.blocks.length ? "신뢰도 80% 미만인 항목이 없습니다." : "인식된 텍스트가 없습니다. 빈 페이지 또는 인식 누락인지 원문을 확인하세요.";
  for (const block of blocks) {
    const button = document.createElement("button");
    button.className = `block${block.confidence < 0.8 ? " low" : ""}`;
    button.setAttribute("aria-pressed", "false");
    button.textContent = block.text;
    const confidence = document.createElement("span");
    confidence.className = "confidence";
    confidence.textContent = `신뢰도 ${(block.confidence * 100).toFixed(1)}%${block.confidence < 0.8 ? " · 검토 권장" : ""}`;
    button.append(confidence);
    button.onclick = () => {
      highlightSources([block]);
      button.setAttribute("aria-pressed", "true");
    };
    $("blocks").append(button);
  }
}

function highlightSources(sources) {
  // 근거 하나가 여러 OCR 행을 포함하므로 각 행의 원래 bbox를 모두 표시한다.
  // 행 사이 빈 공간까지 큰 사각형 하나로 덮으면 출처 범위를 과장하므로 합치지 않는다.
  document.querySelectorAll(".block, .chunk").forEach((item) => item.setAttribute("aria-pressed", "false"));
  const boxes = sources.map((source) => {
    const box = document.createElement("div");
    box.className = "highlight";
    const [x0, y0, x1, y1] = source.bbox;
    Object.assign(box.style, { left: `${x0 * 100}%`, top: `${y0 * 100}%`, width: `${(x1 - x0) * 100}%`, height: `${(y1 - y0) * 100}%` });
    return box;
  });
  $("highlights").replaceChildren(...boxes);
}

async function loadChunks(append = false) {
  if (!state.page) return;
  const ticket = state.ticket, pageTicket = state.pageTicket, chunkTicket = ++state.chunkTicket;
  $("chunks-more").hidden = true;
  if (!append) { state.chunkOffset = 0; $("chunks").textContent = "근거를 확인하는 중입니다…"; }
  const version = encodeURIComponent(state.document.ingestion_id);
  try {
    // 페이지 뷰어와 같은 버전을 지정한다. 새 OCR이 완료돼도 예전 이미지에 새 근거를
    // 강조하지 않으며 문서 새로고침 시 두 정보가 함께 새 버전으로 바뀐다.
    const [summary, rows, layouts] = await Promise.all([
      api(`${docPath()}/${structureEndpoint()}?ingestion_id=${version}`),
      api(`${docPath()}/${layoutEnabled() ? "layout/chunks" : "chunks"}?ingestion_id=${version}&page_number=${state.page.pdf_page_number}&limit=11&offset=${state.chunkOffset}`),
      layoutEnabled() ? api(`${docPath()}/layout/pages?ingestion_id=${version}&limit=1&offset=${state.pageIndex}`) : Promise.resolve([]),
    ]);
    if (ticket !== state.ticket || pageTicket !== state.pageTicket || chunkTicket !== state.chunkTicket) return;
    state.structure = summary;
    refreshIndex();
    renderLayout(layouts[0]);
    if (layouts[0] && !append) loadReview();
    $("structure-status").textContent = summary
      ? `${summary.section_count}페이지 · 근거 ${summary.chunk_count}개 준비됨`
      : "이 OCR 버전의 근거가 아직 없습니다. ‘근거 준비’를 눌러 주세요.";
    if (summary?.review_page_count) $("structure-status").textContent += ` · 검토 필요 ${summary.review_page_count}페이지`;
    if (!append) $("chunks").replaceChildren();
    if (!rows.length && !append) $("chunks").textContent = summary
      ? "이 페이지에는 묶을 텍스트가 없습니다. 원문에 인식 누락이 없는지 확인하세요."
      : "근거를 준비한 뒤 확인하세요.";
    for (const chunk of rows.slice(0, 10)) {
      const button = document.createElement("button");
      button.className = "chunk";
      button.setAttribute("aria-pressed", "false");
      button.textContent = chunk.text;
      const meta = document.createElement("span");
      meta.className = "confidence";
      meta.textContent = `${regionNames[chunk.content_type] || "원문"} · ${chunk.section_title} · 원문 ${chunk.sources.length}개 · 최저 신뢰도 ${(chunk.ocr_confidence * 100).toFixed(1)}%`;
      button.append(meta);
      button.onclick = () => {
        highlightSources(chunk.sources);
        button.setAttribute("aria-pressed", "true");
        $("image-stage").scrollIntoView({ behavior: "smooth", block: "center" });
      };
      $("chunks").append(button);
    }
    state.chunkOffset += Math.min(10, rows.length);
    $("chunks-more").hidden = rows.length <= 10;
    controls();
  } catch (error) {
    if (ticket !== state.ticket || pageTicket !== state.pageTicket || chunkTicket !== state.chunkTicket) return;
    $("structure-status").textContent = `근거 조회 실패: ${message(error)}`;
    if (!append) $("chunks").textContent = "‘근거 확인’ 또는 ‘근거 준비’로 다시 조회할 수 있습니다.";
    else $("chunks-more").hidden = false;
  }
}

$("build-structure").onclick = async () => {
  if (!state.document || state.building || active()) return;
  const ticket = state.ticket;
  state.building = true;
  controls();
  $("structure-status").textContent = "페이지 순서와 원문 연결을 저장하는 중입니다…";
  try {
    await api(`${docPath()}/${structureEndpoint()}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ingestion_id: state.document.ingestion_id }),
    });
    if (ticket === state.ticket) await loadChunks();
  } catch (error) {
    if (ticket === state.ticket) $("structure-status").textContent = message(error);
  } finally {
    if (ticket === state.ticket) { state.building = false; controls(); }
  }
};
$("chunks-more").onclick = () => loadChunks(true);
function renderLayout(layout) {
  $("layout-panel").hidden = !layout;
  $("layout-regions").replaceChildren();
  if (!layout) return;
  // 동일 경고를 합쳐 표시하되 판별 보류/표/다단의 사유는 숨기지 않는다.
  const reasons = new Set([...layout.warnings, ...layout.regions.flatMap((region) => region.review_reasons)]);
  $("layout-warning").textContent = [...reasons].map((reason) => layoutWarnings[reason] || reason).join("\n");
  const blocks = new Map(state.page.blocks.map((block) => [block.block_id, block]));
  layout.regions.forEach((region, index) => {
    const button = document.createElement("button");
    button.className = "quiet layout-region";
    button.textContent = `${index + 1}. ${regionNames[region.kind]}${region.column ? ` · ${region.column}열` : ""} · 원문 ${region.block_ids.length}개`;
    button.onclick = () => {
      highlightSources(region.block_ids.map((id) => blocks.get(id)).filter(Boolean));
      $("image-stage").scrollIntoView({ behavior: "smooth", block: "center" });
    };
    $("layout-regions").append(button);
  });
}
$("layout-mode").onchange = () => {
  resetReview();
  resetSearch();
  // 모드 전환은 분석 결과를 삭제하지 않는다. 조회할 버전만 바꾸고 이전 요청을 무효화한다.
  ++state.chunkTicket;
  state.structure = null;
  $("highlights").replaceChildren();
  renderLayout(null);
  $("structure-status").textContent = "선택한 방식의 근거를 확인합니다.";
  const url = new URL(location.href);
  url.searchParams.set("layout", $("layout-mode").value);
  history.replaceState(null, "", url);
  controls();
  loadChunks();
};
$("upload-form").onsubmit = async (event) => {
  event.preventDefault();
  const file = $("pdf-file").files[0];
  if (!file) return;
  $("upload-button").disabled = true;
  $("upload-button").textContent = "등록 중…";
  notice("PDF를 업로드하고 확인하는 중입니다.");
  try {
    const data = new FormData();
    data.append("file", file);
    const result = await api("/documents", { method: "POST", body: data });
    state.offset = 0;
    await listDocuments();
    await selectDocument(result.document.document_id);
    notice(result.duplicate ? "동일한 파일이 이미 등록되어 기존 문서를 열었습니다." : "문서를 등록했습니다. OCR을 시작해 주세요.");
    $("upload-form").reset();
  } catch (error) { notice(message(error), true); }
  finally { $("upload-button").disabled = false; $("upload-button").textContent = "PDF 등록"; }
};
$("start-ocr").onclick = async () => {
  if (!state.document || state.starting || active()) return;
  const ticket = state.ticket;
  state.starting = true;
  controls();
  notice("");
  try {
    const job = await api(`${docPath()}/ocr`, { method: "POST" });
    if (ticket !== state.ticket) return;
    state.job = job;
    showJob();
    schedulePoll(ticket);
  } catch (error) {
    if (ticket !== state.ticket) return;
    // 다른 탭에서 먼저 시작한 경우 해당 작업을 조회하여 현재 화면도 진행 상태로 전환한다.
    if (error.status === 409) await selectDocument(state.document.document_id);
    else notice(message(error), true);
  } finally { if (ticket === state.ticket) { state.starting = false; controls(); } }
};
$("refresh").onclick = async () => {
  // 목록 오류를 상세 화면의 초기화가 지우지 않도록 실패하면 여기서 멈춘다.
  if (!await listDocuments()) return;
  const id = state.document?.document_id || new URL(location.href).searchParams.get("document");
  if (id) await selectDocument(id);
};
$("docs-prev").onclick = () => { state.offset = Math.max(0, state.offset - 10); listDocuments(); };
$("docs-next").onclick = () => { state.offset += 10; listDocuments(); };
$("page-prev").onclick = () => { state.pageIndex--; loadPage(); };
$("page-next").onclick = () => { state.pageIndex++; loadPage(); };
$("low-only").onchange = renderBlocks;
$("layout-mode").value = new URL(location.href).searchParams.get("layout") === "geometry" ? "geometry" : "page";
listDocuments();
const initialId = new URL(location.href).searchParams.get("document");
if (initialId) selectDocument(initialId);
