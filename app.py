from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
import json
import os
import time

from flask import Flask, Response, jsonify, render_template, request, send_from_directory, stream_with_context
from werkzeug.utils import secure_filename

app = Flask(__name__)

UPLOAD_DIR = Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
ALLOWED_LESSON_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}

# In-memory state for v1; resets when the process restarts.
state_lock = Lock()
submissions = []
next_id = 1
active_prompt = None
next_prompt_id = 1
responses = []
next_response_id = 1
state_version = 0
active_lesson = None
current_slide_index = 0
next_lesson_id = 1

# Basic per-IP rate limiting to avoid accidental spam in a classroom setting.
RATE_LIMIT_WINDOW_SECONDS = 10
RATE_LIMIT_MAX_REQUESTS = 10
request_history = defaultdict(deque)

MAX_MESSAGE_LENGTH = 280
MAX_NAME_LENGTH = 40
MAX_STORED_SUBMISSIONS = 200
MAX_PROMPT_LENGTH = 280
MAX_OPTION_LENGTH = 120
MAX_OPTIONS = 6
MAX_FREE_RESPONSE_LENGTH = 280
MAX_STORED_RESPONSES = 500
MAX_LESSON_TITLE_LENGTH = 120
MAX_LESSON_SLIDE_TITLE_LENGTH = 120
MAX_LESSON_SLIDE_BODY_LENGTH = 3000
MAX_LESSON_SLIDES = 80


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_identity(name: str) -> str:
    return " ".join(name.strip().lower().split())


def allowed_lesson_image(filename: str) -> bool:
    if "." not in filename:
        return False
    extension = filename.rsplit(".", 1)[1].lower()
    return extension in ALLOWED_LESSON_IMAGE_EXTENSIONS


def clamp_slide_index(index: int, total: int) -> int:
    if total <= 0:
        return 0
    return max(0, min(index, total - 1))


def build_prompt_stats_locked() -> dict | None:
    if not active_prompt:
        return None

    prompt_responses = [item for item in responses if item["promptId"] == active_prompt["id"]]
    if active_prompt["type"] == "multiple_choice":
        counts = {option: 0 for option in active_prompt["options"]}
        for item in prompt_responses:
            answer = item["answer"]
            if answer in counts:
                counts[answer] += 1

        return {
            "totalResponses": len(prompt_responses),
            "options": [
                {"option": option, "count": counts[option]} for option in active_prompt["options"]
            ],
        }

    latest = list(reversed(prompt_responses))[:20]
    return {
        "totalResponses": len(prompt_responses),
        "latestResponses": [
            {
                "name": item["name"],
                "answer": item["answer"],
                "createdAt": item["createdAt"],
                "updatedAt": item.get("updatedAt"),
            }
            for item in latest
        ],
    }


def snapshot_state_locked() -> dict:
    return {
        "submissions": list(submissions),
        "count": len(submissions),
        "activePrompt": dict(active_prompt) if active_prompt else None,
        "promptStats": build_prompt_stats_locked(),
        "activeLesson": dict(active_lesson) if active_lesson else None,
        "currentSlideIndex": current_slide_index,
        "serverTime": now_iso(),
        "stateVersion": state_version,
    }


def snapshot_state() -> dict:
    with state_lock:
        return {
            **snapshot_state_locked(),
        }


def bump_state_version_locked() -> None:
    global state_version
    state_version += 1


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


@app.route("/instructor")
def instructor_view():
    return render_template("instructor.html")


@app.route("/uploads/<path:filename>")
def serve_upload(filename: str):
    return send_from_directory(UPLOAD_DIR, filename)


@app.get("/api/submissions")
def get_submissions():
    return jsonify(snapshot_state())


@app.get("/api/prompt")
def get_prompt():
    snapshot = snapshot_state()
    return jsonify(
        {
            "activePrompt": snapshot["activePrompt"],
            "promptStats": snapshot["promptStats"],
            "serverTime": snapshot["serverTime"],
        }
    )


@app.get("/api/lesson")
def get_lesson():
    snapshot = snapshot_state()
    return jsonify(
        {
            "activeLesson": snapshot["activeLesson"],
            "currentSlideIndex": snapshot["currentSlideIndex"],
            "serverTime": snapshot["serverTime"],
        }
    )


@app.get("/api/stream")
def stream_submissions():
    @stream_with_context
    def event_stream():
        last_seen_version = -1
        last_ping = 0.0

        while True:
            snapshot = snapshot_state()
            latest = snapshot["stateVersion"]

            if latest != last_seen_version:
                last_seen_version = latest
                yield f"event: snapshot\ndata: {json.dumps(snapshot)}\n\n"

            now = time.time()
            if now - last_ping > 20:
                last_ping = now
                yield "event: ping\ndata: ok\n\n"

            time.sleep(1)

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return Response(event_stream(), mimetype="text/event-stream", headers=headers)


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

        bump_state_version_locked()

    return jsonify({"ok": True, "submission": submission}), 201


@app.post("/api/instructor/prompt")
def create_prompt():
    payload = request.get_json(silent=True) or {}
    question_type = str(payload.get("questionType", "")).strip().lower()
    prompt_text = str(payload.get("prompt", "")).strip()
    raw_options = payload.get("options", [])

    if question_type not in {"multiple_choice", "free_response"}:
        return jsonify({"error": "questionType must be multiple_choice or free_response."}), 400

    if not prompt_text:
        return jsonify({"error": "Prompt text is required."}), 400

    if len(prompt_text) > MAX_PROMPT_LENGTH:
        return jsonify({"error": f"Prompt must be {MAX_PROMPT_LENGTH} characters or fewer."}), 400

    options = []
    if question_type == "multiple_choice":
        if isinstance(raw_options, list):
            parsed = [str(item).strip() for item in raw_options]
        elif isinstance(raw_options, str):
            parsed = [line.strip() for line in raw_options.splitlines()]
        else:
            parsed = []

        options = [item for item in parsed if item]
        options = list(dict.fromkeys(options))

        if len(options) < 2:
            return jsonify({"error": "Multiple choice prompts need at least 2 options."}), 400

        if len(options) > MAX_OPTIONS:
            return jsonify({"error": f"Multiple choice prompts can have up to {MAX_OPTIONS} options."}), 400

        for option in options:
            if len(option) > MAX_OPTION_LENGTH:
                return jsonify({"error": f"Each option must be {MAX_OPTION_LENGTH} characters or fewer."}), 400

    global active_prompt, next_prompt_id
    with state_lock:
        active_prompt = {
            "id": next_prompt_id,
            "type": question_type,
            "prompt": prompt_text,
            "options": options,
            "locked": False,
            "createdAt": now_iso(),
        }
        next_prompt_id += 1
        bump_state_version_locked()

    return jsonify({"ok": True, "activePrompt": active_prompt}), 201


@app.post("/api/instructor/prompt/close")
def close_prompt():
    global active_prompt
    with state_lock:
        if not active_prompt:
            return jsonify({"error": "No active prompt to close."}), 404

        active_prompt = None
        bump_state_version_locked()

    return jsonify({"ok": True})


@app.post("/api/instructor/prompt/lock")
def lock_prompt():
    global active_prompt
    with state_lock:
        if not active_prompt:
            return jsonify({"error": "No active prompt to lock."}), 404

        if active_prompt.get("locked"):
            return jsonify({"error": "Prompt is already locked."}), 400

        active_prompt["locked"] = True
        active_prompt["lockedAt"] = now_iso()
        bump_state_version_locked()

    return jsonify({"ok": True, "activePrompt": active_prompt})


@app.post("/api/prompt/respond")
def submit_prompt_response():
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown")
    if is_rate_limited(client_ip):
        return (
            jsonify({"error": "Too many requests. Please wait a few seconds."}),
            429,
        )

    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    answer = str(payload.get("answer", "")).strip()
    identity = normalize_identity(name)

    if not name:
        return jsonify({"error": "Name is required."}), 400

    if len(name) > MAX_NAME_LENGTH:
        return jsonify({"error": f"Name must be {MAX_NAME_LENGTH} characters or fewer."}), 400

    if not identity:
        return jsonify({"error": "Name is required."}), 400

    if not answer:
        return jsonify({"error": "Answer is required."}), 400

    global next_response_id
    with state_lock:
        if not active_prompt:
            return jsonify({"error": "No active question right now."}), 400

        if active_prompt.get("locked"):
            return jsonify({"error": "This question is locked. Responses are closed."}), 400

        if active_prompt["type"] == "multiple_choice":
            if answer not in active_prompt["options"]:
                return jsonify({"error": "Answer must be one of the listed options."}), 400
        else:
            if len(answer) > MAX_FREE_RESPONSE_LENGTH:
                return jsonify({"error": f"Answer must be {MAX_FREE_RESPONSE_LENGTH} characters or fewer."}), 400

        response = None
        for item in responses:
            if item["promptId"] == active_prompt["id"] and item.get("identity") == identity:
                item["name"] = name
                item["answer"] = answer
                item["updatedAt"] = now_iso()
                response = item
                break

        if response is None:
            response = {
                "id": next_response_id,
                "promptId": active_prompt["id"],
                "identity": identity,
                "name": name,
                "answer": answer,
                "createdAt": now_iso(),
                "updatedAt": None,
            }
            next_response_id += 1
            responses.append(response)

        if len(responses) > MAX_STORED_RESPONSES:
            del responses[0 : len(responses) - MAX_STORED_RESPONSES]

        bump_state_version_locked()

    return jsonify({"ok": True, "response": response}), 201


@app.post("/api/instructor/lesson/custom")
def create_custom_lesson():
    payload = request.get_json(silent=True) or {}
    title = str(payload.get("title", "")).strip()
    raw_slides = payload.get("slides", [])

    if not title:
        return jsonify({"error": "Lesson title is required."}), 400

    if len(title) > MAX_LESSON_TITLE_LENGTH:
        return jsonify({"error": f"Lesson title must be {MAX_LESSON_TITLE_LENGTH} characters or fewer."}), 400

    if not isinstance(raw_slides, list) or not raw_slides:
        return jsonify({"error": "At least one slide is required."}), 400

    if len(raw_slides) > MAX_LESSON_SLIDES:
        return jsonify({"error": f"Lesson can include up to {MAX_LESSON_SLIDES} slides."}), 400

    slides = []
    for index, item in enumerate(raw_slides):
        if not isinstance(item, dict):
            return jsonify({"error": f"Slide {index + 1} is invalid."}), 400

        slide_title = str(item.get("title", "")).strip() or f"Slide {index + 1}"
        slide_body = str(item.get("body", "")).strip()

        if len(slide_title) > MAX_LESSON_SLIDE_TITLE_LENGTH:
            return jsonify({"error": f"Slide title must be {MAX_LESSON_SLIDE_TITLE_LENGTH} characters or fewer."}), 400

        if len(slide_body) > MAX_LESSON_SLIDE_BODY_LENGTH:
            return jsonify({"error": f"Slide body must be {MAX_LESSON_SLIDE_BODY_LENGTH} characters or fewer."}), 400

        slides.append({"kind": "text", "title": slide_title, "body": slide_body})

    global active_lesson, current_slide_index, next_lesson_id
    with state_lock:
        active_lesson = {
            "id": next_lesson_id,
            "type": "custom",
            "title": title,
            "slides": slides,
            "createdAt": now_iso(),
        }
        next_lesson_id += 1
        current_slide_index = 0
        bump_state_version_locked()

    return jsonify({"ok": True, "activeLesson": active_lesson, "currentSlideIndex": current_slide_index}), 201


@app.post("/api/instructor/lesson/upload-images")
def upload_image_lesson():
    title = str(request.form.get("title", "")).strip()
    files = request.files.getlist("files")

    if not title:
        return jsonify({"error": "Lesson title is required."}), 400

    if len(title) > MAX_LESSON_TITLE_LENGTH:
        return jsonify({"error": f"Lesson title must be {MAX_LESSON_TITLE_LENGTH} characters or fewer."}), 400

    if not files:
        return jsonify({"error": "Upload at least one image."}), 400

    if len(files) > MAX_LESSON_SLIDES:
        return jsonify({"error": f"Upload up to {MAX_LESSON_SLIDES} images per lesson."}), 400

    slides = []
    timestamp = int(time.time())
    for index, incoming in enumerate(files):
        original_name = secure_filename(incoming.filename or "")
        if not original_name or not allowed_lesson_image(original_name):
            return jsonify({"error": "Only image files are supported (png, jpg, jpeg, webp, gif)."}), 400

        stored_name = f"{timestamp}_{index}_{original_name}"
        destination = UPLOAD_DIR / stored_name
        incoming.save(destination)
        slides.append(
            {
                "kind": "image",
                "title": Path(original_name).stem,
                "imageUrl": f"/uploads/{stored_name}",
            }
        )

    global active_lesson, current_slide_index, next_lesson_id
    with state_lock:
        active_lesson = {
            "id": next_lesson_id,
            "type": "image",
            "title": title,
            "slides": slides,
            "createdAt": now_iso(),
        }
        next_lesson_id += 1
        current_slide_index = 0
        bump_state_version_locked()

    return jsonify({"ok": True, "activeLesson": active_lesson, "currentSlideIndex": current_slide_index}), 201


@app.post("/api/instructor/lesson/navigate")
def navigate_lesson():
    payload = request.get_json(silent=True) or {}
    direction = str(payload.get("direction", "")).strip().lower()
    requested_index = payload.get("index")

    global current_slide_index
    with state_lock:
        if not active_lesson:
            return jsonify({"error": "No active lesson."}), 404

        total = len(active_lesson.get("slides", []))
        if total == 0:
            return jsonify({"error": "Active lesson has no slides."}), 400

        if isinstance(requested_index, int):
            current_slide_index = clamp_slide_index(requested_index, total)
        elif direction == "next":
            current_slide_index = clamp_slide_index(current_slide_index + 1, total)
        elif direction == "prev":
            current_slide_index = clamp_slide_index(current_slide_index - 1, total)
        else:
            return jsonify({"error": "Provide direction (next/prev) or a numeric index."}), 400

        bump_state_version_locked()
        snapshot = snapshot_state_locked()

    return jsonify(
        {
            "ok": True,
            "activeLesson": snapshot["activeLesson"],
            "currentSlideIndex": snapshot["currentSlideIndex"],
        }
    )


@app.post("/api/instructor/lesson/clear")
def clear_lesson():
    global active_lesson, current_slide_index
    with state_lock:
        active_lesson = None
        current_slide_index = 0
        bump_state_version_locked()

    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
