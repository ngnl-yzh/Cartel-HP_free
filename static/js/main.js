function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function renderInlineMarkdown(value) {
  let text = escapeHtml(value);
  text = text.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  text = text.replace(/\[(.+?)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return text;
}

function renderMarkdown(raw) {
  const lines = String(raw || "").replace(/\r\n/g, "\n").split("\n");
  const html = [];
  let inList = false;

  for (const line of lines) {
    const trimmed = line.trim();

    if (!trimmed) {
      if (inList) { html.push("</ul>"); inList = false; }
      continue;
    }

    const heading = trimmed.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      if (inList) { html.push("</ul>"); inList = false; }
      const level = heading[1].length + 2;
      html.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
      continue;
    }

    const bullet = trimmed.match(/^[-*]\s+(.+)$/);
    if (bullet) {
      if (!inList) { html.push("<ul>"); inList = true; }
      html.push(`<li>${renderInlineMarkdown(bullet[1])}</li>`);
      continue;
    }

    if (inList) { html.push("</ul>"); inList = false; }
    html.push(`<p>${renderInlineMarkdown(trimmed)}</p>`);
  }

  if (inList) html.push("</ul>");
  return html.join("");
}

/** @활동명 멘션을 프로필 링크로 변환 */
function renderMentions(html) {
  return html.replace(/@([\w가-힣\-_]+)/g, (match, name) => {
    return `<a href="/profile/${encodeURIComponent(name)}" class="mention">@${escapeHtml(name)}</a>`;
  });
}

// ── 관리자 폼 파싱 ───────────────────────────────────────────────────────────

function setParseStatus(message, type = "") {
  const status = document.getElementById("parseStatus");
  if (!status) return;
  status.textContent = message;
  status.className = `parse-status ${type}`.trim();
}

function fillCompetitionForm(data) {
  const fields = ["title", "organizer", "start_date", "deadline", "announcement_date", "prize", "link", "description"];
  for (const field of fields) {
    const input = document.getElementById(field);
    if (input && data[field] !== undefined && data[field] !== null) {
      input.value = data[field];
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }
  }

  const selectedTags = Array.isArray(data.tags) ? data.tags : [];
  document.querySelectorAll('input[name="tags"]').forEach((checkbox) => {
    checkbox.checked = selectedTags.includes(checkbox.value);
  });

  if (data._image_path) {
    const hiddenPath = document.getElementById("comp_image_path");
    const imageChanged = document.getElementById("image_changed");
    if (hiddenPath) hiddenPath.value = data._image_path;
    // GPT가 이미지를 파싱했음을 서버에 알려 기존 이미지를 교체하게 함
    if (imageChanged) imageChanged.value = "yes";
    const wrap = document.getElementById("imagePreviewWrap");
    const preview = document.getElementById("imagePreview");
    if (wrap && preview) {
      preview.src = `/uploads/${data._image_path}`;
      wrap.style.display = "flex";
    }
  }

  // GPT 파싱 결과에 review_dates 가 있으면 심사 일정 섹션 자동 채우기
  if (Array.isArray(data.review_dates) && data.review_dates.length > 0) {
    fillReviewDates(data.review_dates);
  }
}

async function parseWithText() {
  const rawText = document.getElementById("raw_text");
  if (!rawText || !rawText.value.trim()) {
    setParseStatus("공고문 텍스트를 입력하세요.", "error");
    return;
  }
  setParseStatus("GPT가 공모전 정보를 정리하는 중입니다.", "loading");
  const formData = new FormData();
  formData.append("text", rawText.value);
  const response = await fetch("/admin/api/parse", { method: "POST", body: formData });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || "텍스트 파싱에 실패했습니다.");
  }
  fillCompetitionForm(await response.json());
  setParseStatus("입력 폼에 반영했습니다.", "success");
}

async function parseWithImage() {
  const imageInput = document.getElementById("parse_image");
  if (!imageInput || !imageInput.files.length) {
    setParseStatus("공고 이미지를 선택하세요.", "error");
    return;
  }
  setParseStatus("이미지에서 공모전 정보를 추출하는 중입니다.", "loading");
  const formData = new FormData();
  formData.append("image", imageInput.files[0]);
  const response = await fetch("/admin/api/parse-image", { method: "POST", body: formData });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || "이미지 파싱에 실패했습니다.");
  }
  fillCompetitionForm(await response.json());
  setParseStatus("입력 폼에 반영했습니다.", "success");
}

async function parseWithDocument() {
  const docInput = document.getElementById("parse_doc");
  if (!docInput || !docInput.files.length) {
    setParseStatus("PDF 또는 HWP 파일을 선택하세요.", "error");
    return;
  }
  setParseStatus("문서에서 공모전 정보를 추출하는 중입니다...", "loading");
  const formData = new FormData();
  formData.append("document", docInput.files[0]);
  const response = await fetch("/admin/api/parse-document", { method: "POST", body: formData });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || "문서 파싱에 실패했습니다.");
  }
  fillCompetitionForm(await response.json());
  setParseStatus("입력 폼에 반영했습니다.", "success");
}

// ── 심사 일정 동적 관리 ──────────────────────────────────────────────────────

let _reviewContainer = null;
let _reviewHidden = null;

function _getReviewRows() {
  if (!_reviewContainer) return [];
  return Array.from(_reviewContainer.querySelectorAll(".review-row")).map((row) => ({
    label: row.querySelector(".review-label").value.trim(),
    date:  row.querySelector(".review-date").value,
  })).filter((r) => r.label || r.date);
}

const _ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const _ANNOUNCE_KWS = ["발표", "결과", "시상", "당선"];

function _syncAnnouncementDate() {
  const announceEl = document.getElementById("announcement_date");
  if (!announceEl || !_reviewContainer) return;
  // 이미 값이 있으면 덮어쓰지 않음
  if (announceEl.value) return;

  for (const row of _reviewContainer.querySelectorAll(".review-row")) {
    const label = (row.querySelector(".review-label")?.value || "").trim();
    const dt    = (row.querySelector(".review-date")?.value || "").trim();
    if (_ANNOUNCE_KWS.some((kw) => label.includes(kw)) && _ISO_DATE_RE.test(dt)) {
      announceEl.value = dt;
      // 잠깐 하이라이트해서 자동 채워진 걸 알려줌
      announceEl.style.transition = "background .3s";
      announceEl.style.background = "#fef9c3";
      setTimeout(() => { announceEl.style.background = ""; }, 1800);
      break;
    }
  }
}

function _updateReviewHidden() {
  if (_reviewHidden) _reviewHidden.value = JSON.stringify(_getReviewRows());
  _syncAnnouncementDate();
}

function _addReviewRow(label = "", dt = "") {
  if (!_reviewContainer) return;
  const row = document.createElement("div");
  row.className = "review-row";
  row.style.cssText = "display:grid;grid-template-columns:1fr 1fr auto;gap:.5rem;align-items:end;margin-bottom:.5rem";
  row.innerHTML = `
    <label class="field" style="margin:0">
      <span>단계명</span>
      <input type="text" class="review-label" placeholder="예: 1차 심사" value="${escapeHtml(label)}">
    </label>
    <label class="field" style="margin:0">
      <span>일자</span>
      <input type="text" class="review-date" placeholder="예: 2026-07-14 또는 7월 말" value="${escapeHtml(dt)}">
    </label>
    <button type="button" class="btn btn-danger btn-sm remove-review-btn" style="margin-bottom:0">삭제</button>
  `;
  row.querySelector(".remove-review-btn").addEventListener("click", () => {
    row.remove();
    _updateReviewHidden();
  });
  row.querySelector(".review-label").addEventListener("input", _updateReviewHidden);
  row.querySelector(".review-date").addEventListener("change", _updateReviewHidden);
  _reviewContainer.appendChild(row);
  _updateReviewHidden();
}

function fillReviewDates(reviewDates) {
  if (!_reviewContainer) return;
  _reviewContainer.innerHTML = "";
  (Array.isArray(reviewDates) ? reviewDates : []).forEach((r) =>
    _addReviewRow(r.label || "", r.date || "")
  );
  _updateReviewHidden();
}

function initReviewDates() {
  _reviewContainer = document.getElementById("reviewDatesContainer");
  _reviewHidden    = document.getElementById("reviewDatesJson");
  const addBtn     = document.getElementById("addReviewDateBtn");
  if (!_reviewContainer || !_reviewHidden) return;

  if (addBtn) addBtn.addEventListener("click", () => _addReviewRow());

  // 폼 제출 직전 hidden 필드 최종 동기화
  const form = _reviewHidden.closest("form");
  if (form) form.addEventListener("submit", _updateReviewHidden, { capture: true });

  // 기존 데이터 로드 (서버에서 내려준 JSON)
  try {
    const existing = JSON.parse(_reviewHidden.value || "[]");
    if (Array.isArray(existing) && existing.length > 0) {
      existing.forEach((r) => _addReviewRow(r.label || "", r.date || ""));
      _syncAnnouncementDate();  // 로드 후에도 자동 동기화
    }
  } catch {}
}

// ── 모바일 햄버거 메뉴 ───────────────────────────────────────────────────────

function initMobileNav() {
  const hamburger = document.getElementById("navHamburger");
  const overlay   = document.getElementById("mobileNavOverlay");
  const closeBtn  = document.getElementById("mobileNavClose");
  if (!hamburger || !overlay) return;

  function openMenu() {
    overlay.classList.add("is-open");
    hamburger.setAttribute("aria-expanded", "true");
    document.body.style.overflow = "hidden";
  }
  function closeMenu() {
    overlay.classList.remove("is-open");
    hamburger.setAttribute("aria-expanded", "false");
    document.body.style.overflow = "";
  }

  hamburger.addEventListener("click", openMenu);
  if (closeBtn) closeBtn.addEventListener("click", closeMenu);

  // 패널 바깥(어두운 오버레이) 클릭 시 닫기
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeMenu();
  });

  // ESC 키로 닫기
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeMenu();
  });
}

// ── DOMContentLoaded ─────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  initMobileNav();
  initReviewDates();
  // data-confirm 폼
  document.querySelectorAll("[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm || "진행하시겠습니까?")) {
        event.preventDefault();
      }
    });
  });

  // 마크다운 렌더링
  document.querySelectorAll("[data-markdown]").forEach((element) => {
    element.innerHTML = renderMentions(renderMarkdown(element.dataset.raw || ""));
  });

  // 설명 미리보기
  const description = document.getElementById("description");
  const preview = document.getElementById("descriptionPreview");
  if (description && preview) {
    const updatePreview = () => { preview.innerHTML = renderMentions(renderMarkdown(description.value)); };
    description.addEventListener("input", updatePreview);
    updatePreview();
  }

  // 텍스트 파싱 버튼
  const parseTextButton = document.getElementById("parseTextBtn");
  if (parseTextButton) {
    parseTextButton.addEventListener("click", async () => {
      parseTextButton.disabled = true;
      try { await parseWithText(); }
      catch (error) { setParseStatus(error.message, "error"); }
      finally { parseTextButton.disabled = false; }
    });
  }

  // 이미지 파싱 버튼
  const parseImageButton = document.getElementById("parseImageBtn");
  if (parseImageButton) {
    parseImageButton.addEventListener("click", async () => {
      parseImageButton.disabled = true;
      try { await parseWithImage(); }
      catch (error) { setParseStatus(error.message, "error"); }
      finally { parseImageButton.disabled = false; }
    });
  }

  // 문서 파싱 버튼
  const parseDocButton = document.getElementById("parseDocBtn");
  if (parseDocButton) {
    parseDocButton.addEventListener("click", async () => {
      parseDocButton.disabled = true;
      try { await parseWithDocument(); }
      catch (error) { setParseStatus(error.message, "error"); }
      finally { parseDocButton.disabled = false; }
    });
  }

  // 팀 탈퇴 모달
  const leaveModal  = document.getElementById("leaveModal");
  const leaveForm   = document.getElementById("leaveForm");
  const leaveDesc   = document.getElementById("leaveDesc");
  const leaveNick   = document.getElementById("leaveNickname");
  const leavePw     = document.getElementById("leavePassword");
  const leaveFields = document.getElementById("leaveFields");
  const leaveCancel = document.getElementById("leaveCancel");

  document.querySelectorAll(".leave-trigger").forEach((btn) => {
    btn.addEventListener("click", () => {
      const memberId = btn.dataset.memberId;
      const compId   = btn.dataset.compId;
      const teamId   = btn.dataset.teamId;
      const nickname = btn.dataset.nickname;
      const isAdmin  = btn.dataset.admin === "true";

      leaveForm.action = `/competition/${compId}/team/${teamId}/leave/${memberId}`;
      leaveDesc.textContent = `'${nickname}' 님의 팀 참여를 취소합니다.`;

      if (isAdmin) {
        leaveFields.style.display = "none";
        leaveNick.required = false;
        leavePw.required   = false;
        leaveNick.value    = nickname;
        leavePw.value      = "__admin__";
      } else {
        leaveFields.style.display = "block";
        leaveNick.required = true;
        leavePw.required   = true;
        leaveNick.value    = "";
        leavePw.value      = "";
      }

      leaveModal.style.display = "flex";
      if (!isAdmin) setTimeout(() => leaveNick.focus(), 50);
    });
  });

  if (leaveCancel) {
    leaveCancel.addEventListener("click", () => { leaveModal.style.display = "none"; });
  }
  if (leaveModal) {
    leaveModal.addEventListener("click", (e) => {
      if (e.target === leaveModal) leaveModal.style.display = "none";
    });
  }

  // 직접 이미지 업로드 미리보기
  // ※ comp_image_path(기존 이미지 경로)는 지우지 않음 — 서버에서 새 파일이 있으면 우선 사용,
  //    없으면 기존 경로 보존. 직접 업로드가 성공하면 서버가 new_image를 우선하므로 안전.
  const compImageInput = document.getElementById("comp_image_input");
  if (compImageInput) {
    compImageInput.addEventListener("change", () => {
      const file = compImageInput.files[0];
      if (!file) return;
      const wrap    = document.getElementById("imagePreviewWrap");
      const preview = document.getElementById("imagePreview");
      if (wrap && preview) {
        preview.src = URL.createObjectURL(file);
        wrap.style.display = "flex";
      }
    });
  }
});
