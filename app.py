from collections import defaultdict, deque
from datetime import datetime, timezone
from threading import Lock
import os
import time

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

# In-memory state for v1; resets when the process restarts.
state_lock = Lock()
submissions = []
next_id = 1

# Basic per-IP rate limiting to avoid accidental spam in a classroom setting.
RATE_LIMIT_WINDOW_SECONDS = 10
RATE_LIMIT_MAX_REQUESTS = 10
request_history = defaultdict(deque)

MAX_MESSAGE_LENGTH = 280
MAX_NAME_LENGTH = 40
MAX_STORED_SUBMISSIONS = 200


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_rate_limited(client_ip: str) -> bool:
    current_time = time.time()
    history = request_history[client_ip]

    while history and current_time - history[0] > RATE_LIMIT_WINDOW_SECONDS:
        history.popleft()

    if len(history) >= RATE_LIMIT_MAX_REQUESTS:
        return True

    history.append(current_time)
    return False


@app.route("/")
def index():
    return render_template("index.html")


@app.get("/api/submissions")
def get_submissions():
    with state_lock:
        return jsonify(
            {
                "submissions": list(submissions),
                "count": len(submissions),
                "serverTime": now_iso(),
            }
        )


@app.post("/api/submissions")
def create_submission():
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown")
    if is_rate_limited(client_ip):
        return (
            jsonify({"error": "Too many requests. Please wait a few seconds."}),
            429,
        )

    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    message = str(payload.get("message", "")).strip()

    if not name or not message:
        return jsonify({"error": "Name and message are required."}), 400

    if len(name) > MAX_NAME_LENGTH:
        return jsonify({"error": f"Name must be {MAX_NAME_LENGTH} characters or fewer."}), 400

    if len(message) > MAX_MESSAGE_LENGTH:
        return (
            jsonify(
                {
                    "error": f"Message must be {MAX_MESSAGE_LENGTH} characters or fewer."
                }
            ),
            400,
        )

    global next_id
    with state_lock:
        submission = {
            "id": next_id,
            "name": name,
            "message": message,
            "createdAt": now_iso(),
        }
        next_id += 1
        submissions.append(submission)

        if len(submissions) > MAX_STORED_SUBMISSIONS:
            del submissions[0 : len(submissions) - MAX_STORED_SUBMISSIONS]

    return jsonify({"ok": True, "submission": submission}), 201


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
