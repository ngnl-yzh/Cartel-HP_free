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
    if (hiddenPath) hiddenPath.value = data._image_path;
    const wrap = document.getElementById("imagePreviewWrap");
    const preview = document.getElementById("imagePreview");
    if (wrap && preview) {
      preview.src = `/uploads/${data._image_path}`;
      wrap.style.display = "flex";
    }
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

// ── DOMContentLoaded ─────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
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
        const hiddenPath = document.getElementById("comp_image_path");
        if (hiddenPath) hiddenPath.value = "";
      }
    });
  }
});
