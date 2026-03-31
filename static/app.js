const form = document.getElementById("submission-form");
const nameInput = document.getElementById("name");
const messageInput = document.getElementById("message");
const submitButton = document.getElementById("submit-button");
const statusNode = document.getElementById("status");
const feedNode = document.getElementById("feed");
const emptyStateNode = document.getElementById("empty-state");
const lastUpdatedNode = document.getElementById("last-updated");

let latestKnownId = 0;
let pollingTimer = null;
let stream = null;

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

connectStream();
startPolling();
