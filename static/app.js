const form = document.getElementById("submission-form");
const nameInput = document.getElementById("name");
const messageInput = document.getElementById("message");
const submitButton = document.getElementById("submit-button");
const statusNode = document.getElementById("status");
const feedNode = document.getElementById("feed");
const emptyStateNode = document.getElementById("empty-state");
const lastUpdatedNode = document.getElementById("last-updated");
const answerForm = document.getElementById("answer-form");
const answerNameInput = document.getElementById("answer-name");
const answerInputArea = document.getElementById("answer-input-area");
const answerSubmitButton = document.getElementById("answer-submit-button");
const answerStatusNode = document.getElementById("answer-status");
const questionStatusNode = document.getElementById("question-status");
const questionTextNode = document.getElementById("question-text");
const promptResultsNode = document.getElementById("prompt-results");

let latestKnownId = 0;
let pollingTimer = null;
let stream = null;
let currentPrompt = null;

function toLocalTime(isoString) {
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) {
    return "unknown time";
  }
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function showStatus(message, type = "") {
  statusNode.textContent = message;
  statusNode.classList.remove("success", "error");
  if (type) {
    statusNode.classList.add(type);
  }
}

function showAnswerStatus(message, type = "") {
  answerStatusNode.textContent = message;
  answerStatusNode.classList.remove("success", "error");
  if (type) {
    answerStatusNode.classList.add(type);
  }
}

function renderFeed(submissions) {
  feedNode.innerHTML = "";

  if (!submissions.length) {
    emptyStateNode.hidden = false;
    return;
  }

  emptyStateNode.hidden = true;
  const ordered = [...submissions].reverse();

  ordered.forEach((entry) => {
    const li = document.createElement("li");
    li.className = "feed-item";
    li.innerHTML = `
      <header>
        <strong>${escapeHtml(entry.name)}</strong>
        <span>${toLocalTime(entry.createdAt)}</span>
      </header>
      <p>${escapeHtml(entry.message)}</p>
    `;
    feedNode.appendChild(li);
  });
}

function escapeHtml(value) {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function renderPromptInput(prompt) {
  answerInputArea.innerHTML = "";

  if (!prompt) {
    answerSubmitButton.disabled = true;
    return;
  }

  if (prompt.type === "multiple_choice") {
    const options = (prompt.options || [])
      .map(
        (option, index) => `
          <label class="choice-option" for="choice-${index}">
            <input id="choice-${index}" type="radio" name="promptChoice" value="${escapeHtml(option)}" />
            <span>${escapeHtml(option)}</span>
          </label>
        `
      )
      .join("");

    answerInputArea.innerHTML = `<fieldset class="choice-group"><legend>Choose one answer</legend>${options}</fieldset>`;
  } else {
    answerInputArea.innerHTML = `
      <label for="free-answer">Your response</label>
      <textarea id="free-answer" maxlength="280" rows="3" placeholder="Type your response"></textarea>
    `;
  }

  answerSubmitButton.disabled = false;
}

function renderPromptResults(prompt, stats) {
  promptResultsNode.innerHTML = "";

  if (!prompt || !stats) {
    promptResultsNode.hidden = true;
    return;
  }

  promptResultsNode.hidden = false;

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

    promptResultsNode.innerHTML = `
      <p class="muted">Responses: ${stats.totalResponses || 0}</p>
      <ul class="result-list">${rows}</ul>
    `;
    return;
  }

  const latest = (stats.latestResponses || [])
    .slice(0, 8)
    .map(
      (item) => `
      <li class="response-item">
        <header><strong>${escapeHtml(item.name)}</strong><span>${toLocalTime(item.createdAt)}</span></header>
        <p>${escapeHtml(item.answer)}</p>
      </li>
    `
    )
    .join("");

  promptResultsNode.innerHTML = `
    <p class="muted">Responses: ${stats.totalResponses || 0}</p>
    <ul class="response-list">${latest || '<li class="muted">No responses yet.</li>'}</ul>
  `;
}

async function refreshFeed() {
  try {
    const response = await fetch("/api/submissions", { cache: "no-store" });
    if (!response.ok) {
      throw new Error("Could not load feed");
    }

    const data = await response.json();
    applySnapshot(data);
  } catch {
    lastUpdatedNode.textContent = "Connection issue. Retrying...";
  }
}

function applySnapshot(data) {
  renderFeed(data.submissions || []);

  const newest = (data.submissions || []).at(-1);
  if (newest && newest.id !== latestKnownId) {
    latestKnownId = newest.id;
  }

  lastUpdatedNode.textContent = `Last updated: ${toLocalTime(data.serverTime)}`;

  const incomingPrompt = data.activePrompt || null;
  const promptChanged = !currentPrompt || !incomingPrompt || currentPrompt.id !== incomingPrompt.id;
  currentPrompt = incomingPrompt;

  if (!incomingPrompt) {
    questionStatusNode.textContent = "Waiting for instructor prompt...";
    questionTextNode.textContent = "No active question yet.";
    renderPromptInput(null);
    renderPromptResults(null, null);
  } else {
    questionStatusNode.textContent = incomingPrompt.type === "multiple_choice" ? "Multiple choice" : "Free response";
    questionTextNode.textContent = incomingPrompt.prompt;
    if (promptChanged) {
      showAnswerStatus("", "");
      renderPromptInput(incomingPrompt);
    }
    renderPromptResults(incomingPrompt, data.promptStats || null);
  }
}

function startPolling() {
  if (pollingTimer) {
    return;
  }

  refreshFeed();
  pollingTimer = setInterval(refreshFeed, 2000);
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
      const data = JSON.parse(event.data);
      applySnapshot(data);
      stopPolling();
    } catch {
      startPolling();
    }
  });

  stream.onerror = () => {
    startPolling();
  };
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();

  const payload = {
    name: nameInput.value.trim(),
    message: messageInput.value.trim(),
  };

  if (!payload.name || !payload.message) {
    showStatus("Name and message are required.", "error");
    return;
  }

  submitButton.disabled = true;
  showStatus("Sending...");

  try {
    const response = await fetch("/api/submissions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "Submission failed");
    }

    messageInput.value = "";
    showStatus("Posted successfully.", "success");
    await refreshFeed();
  } catch (error) {
    showStatus(error.message || "Unable to submit right now.", "error");
  } finally {
    submitButton.disabled = false;
  }
});

answerForm.addEventListener("submit", async (event) => {
  event.preventDefault();

  if (!currentPrompt) {
    showAnswerStatus("No active question right now.", "error");
    return;
  }

  const name = answerNameInput.value.trim();
  if (!name) {
    showAnswerStatus("Name is required.", "error");
    return;
  }

  let answer = "";
  if (currentPrompt.type === "multiple_choice") {
    const selected = answerForm.querySelector('input[name="promptChoice"]:checked');
    if (!selected) {
      showAnswerStatus("Please choose an option.", "error");
      return;
    }
    answer = selected.value;
  } else {
    const freeAnswer = document.getElementById("free-answer");
    answer = freeAnswer ? freeAnswer.value.trim() : "";
    if (!answer) {
      showAnswerStatus("Please enter a response.", "error");
      return;
    }
  }

  answerSubmitButton.disabled = true;
  showAnswerStatus("Submitting answer...");

  try {
    const response = await fetch("/api/prompt/respond", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, answer }),
    });

    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "Could not submit answer");
    }

    showAnswerStatus("Answer submitted.", "success");
    await refreshFeed();
  } catch (error) {
    showAnswerStatus(error.message || "Unable to submit answer.", "error");
  } finally {
    answerSubmitButton.disabled = false;
  }
});

connectStream();
startPolling();
