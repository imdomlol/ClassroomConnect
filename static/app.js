const form = document.getElementById("submission-form");
const messageInput = document.getElementById("message");
const submitButton = document.getElementById("submit-button");
const statusNode = document.getElementById("status");
const feedNode = document.getElementById("feed");
const emptyStateNode = document.getElementById("empty-state");
const lastUpdatedNode = document.getElementById("last-updated");
const answerForm = document.getElementById("answer-form");
const answerInputArea = document.getElementById("answer-input-area");
const answerSubmitButton = document.getElementById("answer-submit-button");
const answerStatusNode = document.getElementById("answer-status");
const questionStatusNode = document.getElementById("question-status");
const questionTextNode = document.getElementById("question-text");
const promptResultsNode = document.getElementById("prompt-results");
const nameGate = document.getElementById("name-gate");
const joinForm = document.getElementById("join-form");
const joinNameInput = document.getElementById("join-name");
const joinStatusNode = document.getElementById("join-status");
const activeStudentNode = document.getElementById("active-student");
const chatSidebar = document.getElementById("chat-sidebar");
const chatBody = document.getElementById("chat-body");
const chatToggleButton = document.getElementById("chat-toggle");

let latestKnownId = 0;
let pollingTimer = null;
let stream = null;
let currentPrompt = null;
let sessionName = "";

const SESSION_NAME_KEY = "classroomconnect_student_name";
const CHAT_COLLAPSED_KEY = "classroomconnect_chat_collapsed";

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

function showJoinStatus(message, type = "") {
  joinStatusNode.textContent = message;
  joinStatusNode.classList.remove("success", "error");
  if (type) {
    joinStatusNode.classList.add(type);
  }
}

function updateSessionBanner() {
  activeStudentNode.textContent = sessionName ? `Signed in as ${sessionName}` : "";
}

function setSessionName(name) {
  sessionName = name;
  sessionStorage.setItem(SESSION_NAME_KEY, name);
  nameGate.hidden = true;
  updateSessionBanner();
}

function ensureSessionName() {
  const stored = (sessionStorage.getItem(SESSION_NAME_KEY) || "").trim();
  if (stored) {
    sessionName = stored;
    nameGate.hidden = true;
    updateSessionBanner();
    return;
  }

  nameGate.hidden = false;
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

  feedNode.scrollTop = feedNode.scrollHeight;
}

function setChatCollapsed(isCollapsed) {
  chatSidebar.classList.toggle("collapsed", isCollapsed);
  chatBody.hidden = isCollapsed;
  chatToggleButton.textContent = isCollapsed ? "Open Chat" : "Minimize";
  sessionStorage.setItem(CHAT_COLLAPSED_KEY, isCollapsed ? "1" : "0");
}

function initializeChatState() {
  const collapsed = sessionStorage.getItem(CHAT_COLLAPSED_KEY) === "1";
  setChatCollapsed(collapsed);
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

  answerSubmitButton.disabled = !sessionName || Boolean(prompt.locked);
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
    const promptType = incomingPrompt.type === "multiple_choice" ? "Multiple choice" : "Free response";
    questionStatusNode.textContent = incomingPrompt.locked ? `${promptType} - Locked` : promptType;
    questionTextNode.textContent = incomingPrompt.prompt;
    if (promptChanged) {
      showAnswerStatus("", "");
      renderPromptInput(incomingPrompt);
    }
    if (incomingPrompt.locked) {
      answerSubmitButton.disabled = true;
      showAnswerStatus("Instructor locked this question.", "error");
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

  if (!sessionName) {
    showStatus("Enter your name to join first.", "error");
    nameGate.hidden = false;
    return;
  }

  const payload = {
    name: sessionName,
    message: messageInput.value.trim(),
  };

  if (!payload.message) {
    showStatus("Message is required.", "error");
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

  if (!sessionName) {
    showAnswerStatus("Enter your name to join first.", "error");
    nameGate.hidden = false;
    return;
  }

  if (!currentPrompt) {
    showAnswerStatus("No active question right now.", "error");
    return;
  }

  if (currentPrompt.locked) {
    showAnswerStatus("This question is locked.", "error");
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
      body: JSON.stringify({ name: sessionName, answer }),
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

joinForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const candidate = joinNameInput.value.trim();

  if (!candidate) {
    showJoinStatus("Name is required.", "error");
    return;
  }

  setSessionName(candidate);
  showJoinStatus("", "");
  showStatus("", "");
  showAnswerStatus("", "");
  if (currentPrompt) {
    renderPromptInput(currentPrompt);
  }
});

chatToggleButton.addEventListener("click", () => {
  const isCollapsed = !chatSidebar.classList.contains("collapsed");
  setChatCollapsed(isCollapsed);
});

ensureSessionName();
initializeChatState();
connectStream();
startPolling();
