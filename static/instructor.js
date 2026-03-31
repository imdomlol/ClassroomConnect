const promptForm = document.getElementById("prompt-form");
const questionTypeSelect = document.getElementById("question-type");
const promptTextInput = document.getElementById("prompt-text");
const promptOptionsInput = document.getElementById("prompt-options");
const optionsGroup = document.getElementById("options-group");
const promptSubmitButton = document.getElementById("prompt-submit-button");
const lockPromptButton = document.getElementById("prompt-lock-button");
const closePromptButton = document.getElementById("prompt-close-button");
const promptStatusNode = document.getElementById("prompt-status");
const activePromptTextNode = document.getElementById("active-prompt-text");
const activePromptResultsNode = document.getElementById("active-prompt-results");
const lastUpdatedNode = document.getElementById("last-updated");
const lessonCustomForm = document.getElementById("lesson-custom-form");
const lessonTitleInput = document.getElementById("lesson-title-input");
const lessonCustomModeAppend = document.getElementById("lesson-custom-mode-append");
const lessonSlidesInput = document.getElementById("lesson-slides-input");
const lessonPublishCustomButton = document.getElementById("lesson-publish-custom");
const lessonUploadForm = document.getElementById("lesson-upload-form");
const lessonImageTitleInput = document.getElementById("lesson-image-title");
const lessonImageModeAppend = document.getElementById("lesson-image-mode-append");
const lessonImageFilesInput = document.getElementById("lesson-image-files");
const lessonUploadButton = document.getElementById("lesson-upload-button");
const lessonPrevButton = document.getElementById("lesson-prev");
const lessonNextButton = document.getElementById("lesson-next");
const lessonClearButton = document.getElementById("lesson-clear");
const lessonSyncStatusNode = document.getElementById("lesson-sync-status");
const lessonStatusNode = document.getElementById("lesson-status");
const lessonPreviewNode = document.getElementById("lesson-preview");
const lessonTimelineNode = document.getElementById("lesson-timeline");

let stream = null;
let pollingTimer = null;
let currentLesson = null;
let currentLessonSlideIndex = 0;

function toLocalTime(isoString) {
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) {
    return "unknown time";
  }
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function escapeHtml(value) {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function showPromptStatus(message, type = "") {
  promptStatusNode.textContent = message;
  promptStatusNode.classList.remove("success", "error");
  if (type) {
    promptStatusNode.classList.add(type);
  }
}

function showLessonStatus(message, type = "") {
  lessonStatusNode.textContent = message;
  lessonStatusNode.classList.remove("success", "error");
  if (type) {
    lessonStatusNode.classList.add(type);
  }
}

function parseLessonSlides(rawText) {
  return rawText
    .split(/\n\s*---\s*\n/g)
    .map((block) => block.trim())
    .filter(Boolean)
    .map((block) => {
      const lines = block.split("\n");
      const title = (lines.shift() || "Slide").trim();
      const body = lines.join("\n").trim();
      return { title, body };
    });
}

function renderLessonPreview(lesson, currentSlideIndex) {
  if (!lesson || !lesson.slides || !lesson.slides.length) {
    currentLesson = null;
    currentLessonSlideIndex = 0;
    lessonSyncStatusNode.textContent = "No active lesson.";
    lessonPreviewNode.hidden = true;
    lessonPreviewNode.innerHTML = "";
    lessonTimelineNode.hidden = true;
    lessonTimelineNode.innerHTML = "";
    lessonPrevButton.disabled = true;
    lessonNextButton.disabled = true;
    lessonClearButton.disabled = true;
    return;
  }

  const index = Math.max(0, Math.min(currentSlideIndex || 0, lesson.slides.length - 1));
  currentLesson = lesson;
  currentLessonSlideIndex = index;
  const slide = lesson.slides[index];
  lessonSyncStatusNode.textContent = `${lesson.title} (${index + 1}/${lesson.slides.length})`;
  lessonPreviewNode.hidden = false;

  if (slide.kind === "image") {
    lessonPreviewNode.innerHTML = `
      <figure class="lesson-image-slide compact">
        <img src="${slide.imageUrl}" alt="${escapeHtml(slide.title || "Slide")}" />
        <figcaption>${escapeHtml(slide.title || "")}</figcaption>
      </figure>
    `;
  } else {
    const body = escapeHtml(slide.body || "").replaceAll("\n", "<br>");
    lessonPreviewNode.innerHTML = `
      <article class="lesson-text-slide compact">
        <h3>${escapeHtml(slide.title || "Slide")}</h3>
        <p>${body || "No content."}</p>
      </article>
    `;
  }

  lessonPrevButton.disabled = index <= 0;
  lessonNextButton.disabled = index >= lesson.slides.length - 1;
  lessonClearButton.disabled = false;

  const timeline = lesson.slides
    .map((item, itemIndex) => {
      const activeClass = itemIndex === index ? " active" : "";
      const kindLabel = item.kind === "image" ? "Image" : "Text";
      return `
        <div class="timeline-slide${activeClass}" data-slide-index="${itemIndex}">
          <button type="button" class="timeline-slide-main" data-slide-index="${itemIndex}" title="Go to slide ${itemIndex + 1}">
            <span class="timeline-slide-kind">${kindLabel} ${itemIndex + 1}</span>
            <strong>${escapeHtml(item.title || `Slide ${itemIndex + 1}`)}</strong>
          </button>
          <button type="button" class="timeline-slide-remove" data-remove-index="${itemIndex}" title="Remove this slide">X</button>
        </div>
      `;
    })
    .join("");

  lessonTimelineNode.hidden = false;
  lessonTimelineNode.innerHTML = timeline;
}

async function navigateToSlide(index) {
  const response = await fetch("/api/instructor/lesson/navigate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ index }),
  });

  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || "Unable to navigate lesson");
  }

  await refreshSnapshot();
}

async function removeSlide(index) {
  const response = await fetch("/api/instructor/lesson/remove-slide", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ index }),
  });

  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || "Unable to remove slide");
  }

  await refreshSnapshot();
}

function parseOptions(text) {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

function renderPromptResults(prompt, stats) {
  activePromptResultsNode.innerHTML = "";

  if (!prompt || !stats) {
    activePromptResultsNode.hidden = true;
    return;
  }

  activePromptResultsNode.hidden = false;

  if (prompt.type === "multiple_choice") {
    const total = Math.max(stats.totalResponses || 0, 1);
    const rows = (stats.options || [])
      .map((item) => {
        const pct = Math.round((item.count / total) * 100);
        return `
          <li class="result-row">
            <div class="result-head"><span>${escapeHtml(item.option)}</span><strong>${item.count}</strong></div>
            <div class="bar"><span style="width: ${pct}%"></span></div>
          </li>
        `;
      })
      .join("");

    activePromptResultsNode.innerHTML = `
      <p class="muted">Responses: ${stats.totalResponses || 0}</p>
      <ul class="result-list">${rows}</ul>
    `;
    return;
  }

  const answers = (stats.latestResponses || [])
    .map(
      (item) => `
      <li class="response-item">
        <header><strong>${escapeHtml(item.name)}</strong><span>${toLocalTime(item.createdAt)}</span></header>
        <p>${escapeHtml(item.answer)}</p>
      </li>
    `
    )
    .join("");

  activePromptResultsNode.innerHTML = `
    <p class="muted">Responses: ${stats.totalResponses || 0}</p>
    <ul class="response-list">${answers || '<li class="muted">No responses yet.</li>'}</ul>
  `;
}

function applySnapshot(data) {
  const prompt = data.activePrompt;
  const stats = data.promptStats;
  const lesson = data.activeLesson;
  const currentSlideIndex = data.currentSlideIndex;

  if (!prompt) {
    activePromptTextNode.textContent = "No prompt is active.";
    lockPromptButton.disabled = true;
    lockPromptButton.textContent = "Lock Responses";
    renderPromptResults(null, null);
  } else {
    const label = prompt.type === "multiple_choice" ? "Multiple choice" : "Free response";
    const status = prompt.locked ? "Locked" : "Open";
    activePromptTextNode.textContent = `${label} (${status}): ${prompt.prompt}`;
    lockPromptButton.disabled = Boolean(prompt.locked);
    lockPromptButton.textContent = prompt.locked ? "Responses Locked" : "Lock Responses";
    renderPromptResults(prompt, stats);
  }

  lastUpdatedNode.textContent = `Last updated: ${toLocalTime(data.serverTime)}`;
  renderLessonPreview(lesson, currentSlideIndex);
}

async function refreshSnapshot() {
  try {
    const response = await fetch("/api/instructor/state", { cache: "no-store" });
    if (!response.ok) {
      throw new Error("Could not load prompt state");
    }

    applySnapshot(await response.json());
  } catch {
    lastUpdatedNode.textContent = "Connection issue. Retrying...";
  }
}

function startPolling() {
  if (pollingTimer) {
    return;
  }

  refreshSnapshot();
  pollingTimer = setInterval(refreshSnapshot, 2000);
}

function stopPolling() {
  if (pollingTimer) {
    clearInterval(pollingTimer);
    pollingTimer = null;
  }
}

function connectStream() {
  if (!("EventSource" in window)) {
    startPolling();
    return;
  }

  stream = new EventSource("/api/instructor/stream");
  stream.addEventListener("snapshot", (event) => {
    try {
      applySnapshot(JSON.parse(event.data));
      stopPolling();
    } catch {
      startPolling();
    }
  });

  stream.onerror = () => {
    startPolling();
  };
}

questionTypeSelect.addEventListener("change", () => {
  optionsGroup.hidden = questionTypeSelect.value !== "multiple_choice";
});

promptForm.addEventListener("submit", async (event) => {
  event.preventDefault();

  const payload = {
    questionType: questionTypeSelect.value,
    prompt: promptTextInput.value.trim(),
    options: parseOptions(promptOptionsInput.value),
  };

  promptSubmitButton.disabled = true;
  showPromptStatus("Publishing prompt...");

  try {
    const response = await fetch("/api/instructor/prompt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "Unable to publish prompt");
    }

    showPromptStatus("Prompt published.", "success");
    await refreshSnapshot();
  } catch (error) {
    showPromptStatus(error.message || "Unable to publish prompt.", "error");
  } finally {
    promptSubmitButton.disabled = false;
  }
});

closePromptButton.addEventListener("click", async () => {
  closePromptButton.disabled = true;
  showPromptStatus("Closing prompt...");

  try {
    const response = await fetch("/api/instructor/prompt/close", { method: "POST" });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "Unable to close prompt");
    }

    showPromptStatus("Prompt closed.", "success");
    await refreshSnapshot();
  } catch (error) {
    showPromptStatus(error.message || "Unable to close prompt.", "error");
  } finally {
    closePromptButton.disabled = false;
  }
});

lockPromptButton.addEventListener("click", async () => {
  lockPromptButton.disabled = true;
  showPromptStatus("Locking responses...");

  try {
    const response = await fetch("/api/instructor/prompt/lock", { method: "POST" });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "Unable to lock prompt responses");
    }

    showPromptStatus("Prompt responses locked.", "success");
    await refreshSnapshot();
  } catch (error) {
    showPromptStatus(error.message || "Unable to lock prompt responses.", "error");
    lockPromptButton.disabled = false;
  }
});

lessonCustomForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const title = lessonTitleInput.value.trim();
  const slides = parseLessonSlides(lessonSlidesInput.value);
  const mode = lessonCustomModeAppend.checked ? "append" : "replace";

  if (!title) {
    showLessonStatus("Lesson title is required.", "error");
    return;
  }

  if (!slides.length) {
    showLessonStatus("Add at least one slide in the text area.", "error");
    return;
  }

  lessonPublishCustomButton.disabled = true;
  showLessonStatus("Publishing lesson...");

  try {
    const response = await fetch("/api/instructor/lesson/custom", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, slides, mode }),
    });

    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "Unable to publish lesson");
    }

    showLessonStatus(mode === "append" ? "Text slides added to timeline." : "Lesson replaced with text slides.", "success");
    await refreshSnapshot();
  } catch (error) {
    showLessonStatus(error.message || "Unable to publish lesson.", "error");
  } finally {
    lessonPublishCustomButton.disabled = false;
  }
});

lessonUploadForm.addEventListener("submit", async (event) => {
  event.preventDefault();

  const title = lessonImageTitleInput.value.trim();
  const mode = lessonImageModeAppend.checked ? "append" : "replace";
  const files = lessonImageFilesInput.files;
  if (!title) {
    showLessonStatus("Image lesson title is required.", "error");
    return;
  }

  if (!files || !files.length) {
    showLessonStatus("Select one or more images.", "error");
    return;
  }

  const formData = new FormData();
  formData.append("title", title);
  formData.append("mode", mode);
  Array.from(files).forEach((file) => formData.append("files", file));

  lessonUploadButton.disabled = true;
  showLessonStatus("Uploading lesson images...");

  try {
    const response = await fetch("/api/instructor/lesson/upload-images", {
      method: "POST",
      body: formData,
    });

    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "Unable to upload image lesson");
    }

    showLessonStatus(mode === "append" ? "Image slides added to timeline." : "Lesson replaced with image slides.", "success");
    lessonImageFilesInput.value = "";
    await refreshSnapshot();
  } catch (error) {
    showLessonStatus(error.message || "Unable to upload image lesson.", "error");
  } finally {
    lessonUploadButton.disabled = false;
  }
});

lessonPrevButton.addEventListener("click", async () => {
  try {
    await navigateToSlide(currentLessonSlideIndex - 1);
    showLessonStatus("Moved to previous slide.", "success");
  } catch (error) {
    showLessonStatus(error.message || "Unable to navigate lesson.", "error");
  }
});

lessonNextButton.addEventListener("click", async () => {
  try {
    await navigateToSlide(currentLessonSlideIndex + 1);
    showLessonStatus("Moved to next slide.", "success");
  } catch (error) {
    showLessonStatus(error.message || "Unable to navigate lesson.", "error");
  }
});

lessonClearButton.addEventListener("click", async () => {
  try {
    const response = await fetch("/api/instructor/lesson/clear", { method: "POST" });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "Unable to clear lesson");
    }

    showLessonStatus("Lesson cleared.", "success");
    await refreshSnapshot();
  } catch (error) {
    showLessonStatus(error.message || "Unable to clear lesson.", "error");
  }
});

lessonTimelineNode.addEventListener("click", async (event) => {
  const removeButton = event.target.closest("button[data-remove-index]");
  if (removeButton) {
    const index = Number.parseInt(removeButton.dataset.removeIndex || "", 10);
    if (Number.isNaN(index)) {
      return;
    }

    try {
      await removeSlide(index);
      showLessonStatus("Slide removed.", "success");
    } catch (error) {
      showLessonStatus(error.message || "Unable to remove slide.", "error");
    }
    return;
  }

  const goToButton = event.target.closest("button[data-slide-index]");
  if (!goToButton) {
    return;
  }

  const index = Number.parseInt(goToButton.dataset.slideIndex || "", 10);
  if (Number.isNaN(index)) {
    return;
  }

  try {
    await navigateToSlide(index);
  } catch (error) {
    showLessonStatus(error.message || "Unable to navigate lesson.", "error");
  }
});

optionsGroup.hidden = false;
lockPromptButton.disabled = true;
lessonPrevButton.disabled = true;
lessonNextButton.disabled = true;
lessonClearButton.disabled = true;
lessonTimelineNode.hidden = true;
connectStream();
startPolling();
