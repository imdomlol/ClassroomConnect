const promptForm = document.getElementById("prompt-form");
const questionTypeSelect = document.getElementById("question-type");
const promptTextInput = document.getElementById("prompt-text");
const promptOptionsInput = document.getElementById("prompt-options");
const optionsGroup = document.getElementById("options-group");
const promptSubmitButton = document.getElementById("prompt-submit-button");
const closePromptButton = document.getElementById("prompt-close-button");
const promptStatusNode = document.getElementById("prompt-status");
const activePromptTextNode = document.getElementById("active-prompt-text");
const activePromptResultsNode = document.getElementById("active-prompt-results");
const lastUpdatedNode = document.getElementById("last-updated");

let stream = null;
let pollingTimer = null;

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

  if (!prompt) {
    activePromptTextNode.textContent = "No prompt is active.";
    renderPromptResults(null, null);
  } else {
    const label = prompt.type === "multiple_choice" ? "Multiple choice" : "Free response";
    activePromptTextNode.textContent = `${label}: ${prompt.prompt}`;
    renderPromptResults(prompt, stats);
  }

  lastUpdatedNode.textContent = `Last updated: ${toLocalTime(data.serverTime)}`;
}

async function refreshSnapshot() {
  try {
    const response = await fetch("/api/submissions", { cache: "no-store" });
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

  stream = new EventSource("/api/stream");
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

optionsGroup.hidden = false;
connectStream();
startPolling();
