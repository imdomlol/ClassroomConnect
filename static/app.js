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
const joinEmailInput = document.getElementById("join-email");
const joinNameFields = document.getElementById("join-name-fields");
const joinRosterWarning = document.getElementById("join-roster-warning");
const joinFirstNameInput = document.getElementById("join-first-name");
const joinLastNameInput = document.getElementById("join-last-name");
const joinStatusNode = document.getElementById("join-status");
const activeStudentNode = document.getElementById("active-student");
const chatSidebar = document.getElementById("chat-sidebar");
const chatBody = document.getElementById("chat-body");
const chatToggleButton = document.getElementById("chat-toggle");
const lessonStatusNode = document.getElementById("lesson-status");
const lessonStageNode = document.getElementById("lesson-stage");

let latestKnownId = 0;
let pollingTimer = null;
let stream = null;
let currentPrompt = null;
let sessionName = "";
let sessionEmail = "";
let sessionFirstName = "";
let sessionLastName = "";
let sessionRosterMatched = false;
let presenceTimer = null;

const SESSION_NAME_KEY = "classroomconnect_student_name";
const SESSION_EMAIL_KEY = "classroomconnect_student_email";
const SESSION_FIRST_NAME_KEY = "classroomconnect_student_first_name";
const SESSION_LAST_NAME_KEY = "classroomconnect_student_last_name";
const SESSION_ROSTER_MATCHED_KEY = "classroomconnect_student_roster_matched";
const SESSION_ID_KEY = "classroomconnect_presence_session_id";
const CHAT_COLLAPSED_KEY = "classroomconnect_chat_collapsed";
const PRESENCE_HEARTBEAT_MS = 15000;

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

function normalizeEmail(value) {
  return value.trim().toLowerCase();
}

function buildStudentPayload(extra = {}) {
  return {
    email: sessionEmail,
    firstName: sessionFirstName,
    lastName: sessionLastName,
    name: sessionName,
    ...extra,
  };
}

function updateSessionBanner() {
  activeStudentNode.textContent = sessionName && sessionEmail ? `Signed in as ${sessionName} (${sessionEmail})` : "";
}

function getPresenceSessionId() {
  let sessionId = sessionStorage.getItem(SESSION_ID_KEY);
  if (!sessionId) {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      sessionId = window.crypto.randomUUID();
    } else {
      sessionId = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    }
    sessionStorage.setItem(SESSION_ID_KEY, sessionId);
  }
  return sessionId;
}

async function sendPresenceHeartbeat() {
  if (!sessionName) {
    return;
  }

  try {
    await fetch("/api/presence/heartbeat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildStudentPayload({ sessionId: getPresenceSessionId() })),
      keepalive: true,
    });
  } catch {
    // Presence is advisory; normal classroom actions should keep working offline.
  }
}

function startPresenceHeartbeat() {
  if (!sessionName) {
    return;
  }

  sendPresenceHeartbeat();
  if (presenceTimer) {
    return;
  }
  presenceTimer = setInterval(sendPresenceHeartbeat, PRESENCE_HEARTBEAT_MS);
}

function sendPresenceDisconnect() {
  if (!sessionName) {
    return;
  }

  const payload = JSON.stringify(buildStudentPayload({ sessionId: getPresenceSessionId() }));
  if (navigator.sendBeacon) {
    const blob = new Blob([payload], { type: "application/json" });
    navigator.sendBeacon("/api/presence/disconnect", blob);
    return;
  }

  fetch("/api/presence/disconnect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: payload,
    keepalive: true,
  }).catch(() => {});
}

function setSessionStudent(student) {
  sessionEmail = normalizeEmail(student.email || "");
  sessionFirstName = (student.firstName || "").trim();
  sessionLastName = (student.lastName || "").trim();
  sessionName = [sessionFirstName, sessionLastName].filter(Boolean).join(" ") || (student.name || "").trim();
  sessionRosterMatched = Boolean(student.rosterMatched);

  sessionStorage.setItem(SESSION_EMAIL_KEY, sessionEmail);
  sessionStorage.setItem(SESSION_FIRST_NAME_KEY, sessionFirstName);
  sessionStorage.setItem(SESSION_LAST_NAME_KEY, sessionLastName);
  sessionStorage.setItem(SESSION_NAME_KEY, sessionName);
  sessionStorage.setItem(SESSION_ROSTER_MATCHED_KEY, sessionRosterMatched ? "1" : "0");
  nameGate.hidden = true;
  updateSessionBanner();
  startPresenceHeartbeat();
}

function ensureSessionName() {
  const storedEmail = normalizeEmail(sessionStorage.getItem(SESSION_EMAIL_KEY) || "");
  const storedFirstName = (sessionStorage.getItem(SESSION_FIRST_NAME_KEY) || "").trim();
  const storedLastName = (sessionStorage.getItem(SESSION_LAST_NAME_KEY) || "").trim();
  const storedName = (sessionStorage.getItem(SESSION_NAME_KEY) || "").trim();
  if (storedEmail && storedName) {
    sessionEmail = storedEmail;
    sessionFirstName = storedFirstName;
    sessionLastName = storedLastName;
    sessionName = storedName;
    sessionRosterMatched = sessionStorage.getItem(SESSION_ROSTER_MATCHED_KEY) === "1";
    nameGate.hidden = true;
    updateSessionBanner();
    startPresenceHeartbeat();
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

  if (!prompt.locked) {
    promptResultsNode.hidden = false;
    promptResultsNode.innerHTML = '<p class="muted">Responses are hidden until the instructor locks this question.</p>';
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
        <header><strong>Student response</strong><span>${toLocalTime(item.createdAt)}</span></header>
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

function renderLesson(lesson, index) {
  if (!lesson || !Array.isArray(lesson.slides) || !lesson.slides.length) {
    lessonStatusNode.textContent = "Waiting for instructor lesson...";
    lessonStageNode.innerHTML = '<p class="muted">No active lesson yet.</p>';
    return;
  }

  const clampedIndex = Math.max(0, Math.min(index || 0, lesson.slides.length - 1));
  const slide = lesson.slides[clampedIndex];
  lessonStatusNode.textContent = `${lesson.title} (${clampedIndex + 1}/${lesson.slides.length})`;

  if (slide.kind === "image") {
    lessonStageNode.innerHTML = `
      <figure class="lesson-image-slide">
        <img src="${slide.imageUrl}" alt="${escapeHtml(slide.title || "Lesson slide")}" />
        <figcaption>${escapeHtml(slide.title || "")}</figcaption>
      </figure>
    `;
    return;
  }

  const body = escapeHtml(slide.body || "").replaceAll("\n", "<br>");
  lessonStageNode.innerHTML = `
    <article class="lesson-text-slide">
      <h3>${escapeHtml(slide.title || "Slide")}</h3>
      <p>${body || "No content on this slide."}</p>
    </article>
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
  renderLesson(data.activeLesson || null, data.currentSlideIndex || 0);

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
    showStatus("Join with your email first.", "error");
    nameGate.hidden = false;
    return;
  }

  const payload = buildStudentPayload({ message: messageInput.value.trim() });

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
    showAnswerStatus("Join with your email first.", "error");
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
      body: JSON.stringify(buildStudentPayload({ answer })),
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

async function lookupRosterEmail(email) {
  const response = await fetch(`/api/roster/lookup?email=${encodeURIComponent(email)}`, { cache: "no-store" });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || "Could not check roster");
  }
  return data;
}

async function applyRosterLookup() {
  const email = normalizeEmail(joinEmailInput.value);
  if (!email) {
    joinNameFields.hidden = true;
    joinRosterWarning.hidden = true;
    joinFirstNameInput.readOnly = false;
    joinLastNameInput.readOnly = false;
    return null;
  }

  const data = await lookupRosterEmail(email);
  if (data.matched) {
    joinFirstNameInput.value = data.firstName || "";
    joinLastNameInput.value = data.lastName || "";
    joinNameFields.hidden = false;
    joinRosterWarning.hidden = true;
    joinFirstNameInput.readOnly = true;
    joinLastNameInput.readOnly = true;
    showJoinStatus(`Roster match found for ${data.firstName} ${data.lastName}.`, "success");
    return data;
  }

  joinNameFields.hidden = false;
  joinRosterWarning.hidden = false;
  if (joinFirstNameInput.readOnly || joinLastNameInput.readOnly) {
    joinFirstNameInput.value = "";
    joinLastNameInput.value = "";
  }
  joinFirstNameInput.readOnly = false;
  joinLastNameInput.readOnly = false;
  showJoinStatus("Email not recognized. Enter your first and last name to continue.", "error");
  return data;
}

joinEmailInput.addEventListener("blur", async () => {
  try {
    await applyRosterLookup();
  } catch (error) {
    showJoinStatus(error.message || "Could not check roster.", "error");
  }
});

joinForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const email = normalizeEmail(joinEmailInput.value);

  if (!email) {
    showJoinStatus("Email is required.", "error");
    return;
  }

  let lookup;
  try {
    lookup = await applyRosterLookup();
  } catch (error) {
    showJoinStatus(error.message || "Could not check roster.", "error");
    return;
  }

  if (lookup && lookup.matched) {
    setSessionStudent({
      email,
      firstName: lookup.firstName,
      lastName: lookup.lastName,
      name: lookup.name,
      rosterMatched: true,
    });
  } else {
    const firstName = joinFirstNameInput.value.trim();
    const lastName = joinLastNameInput.value.trim();
    if (!firstName || !lastName) {
      showJoinStatus("First and last name are required for unrecognized emails.", "error");
      return;
    }
    setSessionStudent({
      email,
      firstName,
      lastName,
      name: `${firstName} ${lastName}`,
      rosterMatched: false,
    });
  }

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

window.addEventListener("pagehide", sendPresenceDisconnect);

ensureSessionName();
initializeChatState();
connectStream();
startPolling();
