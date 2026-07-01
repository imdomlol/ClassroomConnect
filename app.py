from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
import csv
import io
import json
import os
import sqlite3
import time

from flask import Flask, Response, jsonify, render_template, request, send_from_directory, stream_with_context
from werkzeug.utils import secure_filename

app = Flask(__name__)

UPLOAD_DIR = Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
DATABASE_PATH = Path(os.getenv("CLASSROOMCONNECT_DB", Path(__file__).parent / "classroomconnect.sqlite3"))
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
ALLOWED_LESSON_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}

# SQLite state is protected by this process-level lock for compound mutations.
state_lock = Lock()

# Basic per-IP rate limiting to avoid accidental spam in a classroom setting.
RATE_LIMIT_WINDOW_SECONDS = 10
RATE_LIMIT_MAX_REQUESTS = 10
request_history = defaultdict(deque)

MAX_MESSAGE_LENGTH = 280
MAX_NAME_LENGTH = 40
MAX_EMAIL_LENGTH = 254
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
PRESENCE_STALE_SECONDS = 45


@contextmanager
def get_db():
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def ensure_column(db: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
    columns = {
        row["name"]
        for row in db.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    if column_name not in columns:
        db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def init_db() -> None:
    with get_db() as db:
        db.executescript(
            """
            PRAGMA journal_mode = WAL;

            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS participants (
                identity TEXT PRIMARY KEY,
                email TEXT,
                first_name TEXT,
                last_name TEXT,
                display_name TEXT NOT NULL,
                roster_matched INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS roster_students (
                email TEXT PRIMARY KEY,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                display_name TEXT NOT NULL,
                uploaded_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identity TEXT NOT NULL,
                name TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (identity) REFERENCES participants(identity)
            );

            CREATE TABLE IF NOT EXISTS prompts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL CHECK (type IN ('multiple_choice', 'free_response')),
                prompt TEXT NOT NULL,
                options_json TEXT NOT NULL DEFAULT '[]',
                locked INTEGER NOT NULL DEFAULT 0,
                locked_at TEXT,
                closed_at TEXT,
                is_active INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prompt_id INTEGER NOT NULL,
                identity TEXT NOT NULL,
                name TEXT NOT NULL,
                answer TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                UNIQUE (prompt_id, identity),
                FOREIGN KEY (prompt_id) REFERENCES prompts(id) ON DELETE CASCADE,
                FOREIGN KEY (identity) REFERENCES participants(identity)
            );

            CREATE TABLE IF NOT EXISTS uploaded_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL UNIQUE,
                relative_path TEXT NOT NULL,
                url TEXT NOT NULL,
                content_type TEXT,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL DEFAULT 'mixed',
                title TEXT NOT NULL,
                current_slide_index INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS lesson_slides (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lesson_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('text', 'image')),
                title TEXT NOT NULL,
                body TEXT,
                image_url TEXT,
                upload_id INTEGER,
                FOREIGN KEY (lesson_id) REFERENCES lessons(id) ON DELETE CASCADE,
                FOREIGN KEY (upload_id) REFERENCES uploaded_files(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS presence_sessions (
                session_id TEXT PRIMARY KEY,
                identity TEXT NOT NULL,
                name TEXT NOT NULL,
                connected_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                disconnected_at TEXT,
                disconnect_reason TEXT,
                user_agent TEXT,
                ip_address TEXT,
                FOREIGN KEY (identity) REFERENCES participants(identity)
            );

            CREATE TABLE IF NOT EXISTS presence_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                identity TEXT NOT NULL,
                name TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK (event_type IN ('connect', 'disconnect')),
                reason TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (identity) REFERENCES participants(identity)
            );

            CREATE INDEX IF NOT EXISTS idx_submissions_created_at ON submissions(created_at, id);
            CREATE INDEX IF NOT EXISTS idx_prompts_active ON prompts(is_active);
            CREATE INDEX IF NOT EXISTS idx_responses_prompt_id ON responses(prompt_id);
            CREATE INDEX IF NOT EXISTS idx_lessons_active ON lessons(is_active);
            CREATE INDEX IF NOT EXISTS idx_lesson_slides_lesson_position
                ON lesson_slides(lesson_id, position);
            CREATE INDEX IF NOT EXISTS idx_presence_sessions_identity
                ON presence_sessions(identity);
            CREATE INDEX IF NOT EXISTS idx_presence_sessions_last_seen
                ON presence_sessions(last_seen_at);
            CREATE INDEX IF NOT EXISTS idx_presence_events_created_at
                ON presence_events(created_at);
            CREATE INDEX IF NOT EXISTS idx_participants_email
                ON participants(email);
            """
        )
        ensure_column(db, "participants", "email", "TEXT")
        ensure_column(db, "participants", "first_name", "TEXT")
        ensure_column(db, "participants", "last_name", "TEXT")
        ensure_column(db, "participants", "roster_matched", "INTEGER NOT NULL DEFAULT 0")
        db.execute(
            "INSERT OR IGNORE INTO app_meta (key, value) VALUES ('state_version', '0')"
        )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_identity(name: str) -> str:
    return " ".join(name.strip().lower().split())


def normalize_email(email: str) -> str:
    return str(email or "").strip().lower()


def is_valid_email(email: str) -> bool:
    return (
        bool(email)
        and len(email) <= MAX_EMAIL_LENGTH
        and "@" in email
        and "." in email.rsplit("@", 1)[-1]
        and " " not in email
    )


def display_name(first_name: str, last_name: str, fallback: str = "") -> str:
    name = " ".join(part for part in [first_name.strip(), last_name.strip()] if part)
    return name or fallback.strip()


def split_name(value: str) -> tuple[str, str]:
    parts = str(value or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def first_present(row: dict, keys: list[str]) -> str:
    lowered = {str(key).strip().lower(): value for key, value in row.items()}
    for key in keys:
        value = lowered.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def parse_roster_csv(file_storage) -> list[dict]:
    raw = file_storage.read()
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV must include headers.")

    students = []
    seen = set()
    for row_number, row in enumerate(reader, start=2):
        email = normalize_email(first_present(row, ["email", "student_email", "email_address", "mail"]))
        if not email:
            continue
        if not is_valid_email(email):
            raise ValueError(f"Row {row_number} has an invalid email address.")

        first_name = first_present(row, ["first_name", "firstname", "first", "given_name", "given"])
        last_name = first_present(row, ["last_name", "lastname", "last", "family_name", "surname"])
        if not first_name and not last_name:
            first_name, last_name = split_name(first_present(row, ["name", "full_name", "student_name"]))

        first_name = first_name[:MAX_NAME_LENGTH]
        last_name = last_name[:MAX_NAME_LENGTH]
        name = display_name(first_name, last_name, fallback=email)
        if not first_name or not last_name:
            raise ValueError(f"Row {row_number} must include first and last name.")

        if email in seen:
            continue
        seen.add(email)
        students.append(
            {
                "email": email,
                "firstName": first_name,
                "lastName": last_name,
                "name": name,
            }
        )

    if not students:
        raise ValueError("CSV did not include any student rows.")
    return students


def allowed_lesson_image(filename: str) -> bool:
    if "." not in filename:
        return False
    extension = filename.rsplit(".", 1)[1].lower()
    return extension in ALLOWED_LESSON_IMAGE_EXTENSIONS


def clamp_slide_index(index: int, total: int) -> int:
    if total <= 0:
        return 0
    return max(0, min(index, total - 1))


def normalize_lesson_mode(value: str) -> str:
    mode = str(value or "append").strip().lower()
    if mode not in {"append", "replace"}:
        return "append"
    return mode


def row_to_prompt(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    prompt = {
        "id": row["id"],
        "type": row["type"],
        "prompt": row["prompt"],
        "options": json.loads(row["options_json"] or "[]"),
        "locked": bool(row["locked"]),
        "createdAt": row["created_at"],
    }
    if row["locked_at"]:
        prompt["lockedAt"] = row["locked_at"]
    return prompt


def row_to_response(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "promptId": row["prompt_id"],
        "identity": row["identity"],
        "name": row["name"],
        "answer": row["answer"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def fetch_active_prompt(db: sqlite3.Connection) -> dict | None:
    row = db.execute(
        "SELECT * FROM prompts WHERE is_active = 1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row_to_prompt(row)


def fetch_submissions(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute(
        """
        SELECT id, name, message, created_at
        FROM submissions
        ORDER BY id ASC
        """
    ).fetchall()
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "message": row["message"],
            "createdAt": row["created_at"],
        }
        for row in rows
    ]


def fetch_active_lesson(db: sqlite3.Connection) -> tuple[dict | None, int]:
    lesson_row = db.execute(
        "SELECT * FROM lessons WHERE is_active = 1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not lesson_row:
        return None, 0

    slide_rows = db.execute(
        """
        SELECT *
        FROM lesson_slides
        WHERE lesson_id = ?
        ORDER BY position ASC
        """,
        (lesson_row["id"],),
    ).fetchall()
    slides = []
    for row in slide_rows:
        slide = {"kind": row["kind"], "title": row["title"]}
        if row["kind"] == "image":
            slide["imageUrl"] = row["image_url"]
        else:
            slide["body"] = row["body"] or ""
        slides.append(slide)

    lesson = {
        "id": lesson_row["id"],
        "type": lesson_row["type"],
        "title": lesson_row["title"],
        "slides": slides,
        "createdAt": lesson_row["created_at"],
        "updatedAt": lesson_row["updated_at"],
    }
    return lesson, clamp_slide_index(lesson_row["current_slide_index"], len(slides))


def get_state_version(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT value FROM app_meta WHERE key = 'state_version'").fetchone()
    return int(row["value"]) if row else 0


def iso_to_epoch(iso_string: str | None) -> float | None:
    if not iso_string:
        return None
    try:
        return datetime.fromisoformat(iso_string).timestamp()
    except ValueError:
        return None


def fetch_roster_student(db: sqlite3.Connection, email: str) -> dict | None:
    row = db.execute(
        "SELECT email, first_name, last_name, display_name FROM roster_students WHERE email = ?",
        (normalize_email(email),),
    ).fetchone()
    if not row:
        return None
    return {
        "email": row["email"],
        "firstName": row["first_name"],
        "lastName": row["last_name"],
        "name": row["display_name"],
    }


def participant_from_payload(db: sqlite3.Connection, payload: dict) -> tuple[dict | None, str | None]:
    email = normalize_email(payload.get("email", ""))
    if not email:
        legacy_name = str(payload.get("name", "")).strip()
        if not legacy_name:
            return None, "Email is required."
        return {
            "identity": normalize_identity(legacy_name),
            "email": None,
            "firstName": "",
            "lastName": "",
            "name": legacy_name,
            "rosterMatched": False,
        }, None

    if not is_valid_email(email):
        return None, "Enter a valid email address."

    roster_student = fetch_roster_student(db, email)
    if roster_student:
        return {
            "identity": email,
            "email": email,
            "firstName": roster_student["firstName"],
            "lastName": roster_student["lastName"],
            "name": roster_student["name"],
            "rosterMatched": True,
        }, None

    first_name = str(payload.get("firstName", "")).strip()
    last_name = str(payload.get("lastName", "")).strip()
    if not first_name or not last_name:
        return None, "First and last name are required when the email is not on the class roster."
    if len(first_name) > MAX_NAME_LENGTH or len(last_name) > MAX_NAME_LENGTH:
        return None, f"First and last name must each be {MAX_NAME_LENGTH} characters or fewer."

    return {
        "identity": email,
        "email": email,
        "firstName": first_name,
        "lastName": last_name,
        "name": display_name(first_name, last_name, fallback=email),
        "rosterMatched": False,
    }, None


def upsert_participant(
    db: sqlite3.Connection,
    identity: str,
    name: str,
    timestamp: str,
    email: str | None = None,
    first_name: str = "",
    last_name: str = "",
    roster_matched: bool = False,
) -> None:
    db.execute(
        """
        INSERT INTO participants
            (identity, email, first_name, last_name, display_name, roster_matched, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(identity) DO UPDATE SET
            email = excluded.email,
            first_name = excluded.first_name,
            last_name = excluded.last_name,
            display_name = excluded.display_name,
            roster_matched = excluded.roster_matched,
            last_seen_at = excluded.last_seen_at
        """,
        (
            identity,
            email,
            first_name,
            last_name,
            name,
            1 if roster_matched else 0,
            timestamp,
            timestamp,
        ),
    )


def record_presence_event(
    db: sqlite3.Connection,
    session_id: str,
    identity: str,
    name: str,
    event_type: str,
    timestamp: str,
    reason: str | None = None,
) -> None:
    db.execute(
        """
        INSERT INTO presence_events
            (session_id, identity, name, event_type, reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (session_id, identity, name, event_type, reason, timestamp),
    )


def mark_stale_presence_sessions_locked(db: sqlite3.Connection, timestamp: str) -> bool:
    cutoff = time.time() - PRESENCE_STALE_SECONDS
    stale_rows = db.execute(
        """
        SELECT *
        FROM presence_sessions
        WHERE disconnected_at IS NULL
        """
    ).fetchall()
    changed = False
    for row in stale_rows:
        last_seen = iso_to_epoch(row["last_seen_at"])
        if last_seen is None or last_seen >= cutoff:
            continue

        db.execute(
            """
            UPDATE presence_sessions
            SET disconnected_at = ?, disconnect_reason = 'stale'
            WHERE session_id = ? AND disconnected_at IS NULL
            """,
            (timestamp, row["session_id"]),
        )
        record_presence_event(
            db,
            row["session_id"],
            row["identity"],
            row["name"],
            "disconnect",
            timestamp,
            reason="stale",
        )
        changed = True
    return changed


def fetch_presence_summary(db: sqlite3.Connection) -> dict:
    timestamp = now_iso()
    if mark_stale_presence_sessions_locked(db, timestamp):
        bump_state_version_locked(db)

    rows = db.execute(
        """
        SELECT
            p.identity,
            p.email,
            p.roster_matched,
            p.display_name,
            p.first_seen_at,
            p.last_seen_at AS participant_last_seen_at,
            MAX(s.last_seen_at) AS last_seen_at,
            MAX(CASE WHEN s.disconnected_at IS NULL THEN 1 ELSE 0 END) AS has_open_session,
            SUM(CASE WHEN s.disconnected_at IS NULL THEN 1 ELSE 0 END) AS open_sessions
        FROM participants p
        LEFT JOIN presence_sessions s ON s.identity = p.identity
        GROUP BY
            p.identity,
            p.email,
            p.roster_matched,
            p.display_name,
            p.first_seen_at,
            p.last_seen_at
        ORDER BY COALESCE(MAX(s.last_seen_at), p.last_seen_at) DESC
        """
    ).fetchall()
    participants = []
    connected_count = 0
    for row in rows:
        last_seen_at = row["last_seen_at"] or row["participant_last_seen_at"]
        is_connected = bool(row["has_open_session"])
        if is_connected:
            connected_count += 1
        participants.append(
            {
                "identity": row["identity"],
                "email": row["email"],
                "name": row["display_name"],
                "rosterMatched": bool(row["roster_matched"]),
                "connected": is_connected,
                "openSessions": row["open_sessions"] or 0,
                "firstSeenAt": row["first_seen_at"],
                "lastSeenAt": last_seen_at,
            }
        )

    event_rows = db.execute(
        """
        SELECT id, session_id, identity, name, event_type, reason, created_at
        FROM presence_events
        ORDER BY id DESC
        LIMIT 50
        """
    ).fetchall()
    events = [
        {
            "id": row["id"],
            "sessionId": row["session_id"],
            "identity": row["identity"],
            "name": row["name"],
            "eventType": row["event_type"],
            "reason": row["reason"],
            "createdAt": row["created_at"],
        }
        for row in event_rows
    ]
    return {
        "connectedCount": connected_count,
        "participants": participants,
        "recentEvents": events,
        "staleAfterSeconds": PRESENCE_STALE_SECONDS,
    }


def fetch_roster_summary(db: sqlite3.Connection) -> dict:
    row = db.execute("SELECT COUNT(*) AS count FROM roster_students").fetchone()
    return {"count": row["count"] if row else 0}


def trim_table_by_id(db: sqlite3.Connection, table_name: str, max_rows: int) -> None:
    db.execute(
        f"""
        DELETE FROM {table_name}
        WHERE id NOT IN (
            SELECT id FROM {table_name}
            ORDER BY id DESC
            LIMIT ?
        )
        """,
        (max_rows,),
    )


def insert_lesson_slides(
    db: sqlite3.Connection, lesson_id: int, slides: list[dict], start_position: int = 0
) -> None:
    for offset, slide in enumerate(slides):
        db.execute(
            """
            INSERT INTO lesson_slides
                (lesson_id, position, kind, title, body, image_url, upload_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lesson_id,
                start_position + offset,
                slide["kind"],
                slide["title"],
                slide.get("body"),
                slide.get("imageUrl"),
                slide.get("uploadId"),
            ),
        )


def apply_lesson_update_locked(
    db: sqlite3.Connection, title: str, incoming_slides: list[dict], mode: str
) -> tuple[dict, int]:
    active_lesson, _ = fetch_active_lesson(db)

    timestamp = now_iso()
    if mode == "replace" or not active_lesson:
        db.execute("UPDATE lessons SET is_active = 0 WHERE is_active = 1")
        cursor = db.execute(
            """
            INSERT INTO lessons (type, title, current_slide_index, is_active, created_at, updated_at)
            VALUES ('mixed', ?, 0, 1, ?, ?)
            """,
            (title, timestamp, timestamp),
        )
        lesson_id = cursor.lastrowid
        insert_lesson_slides(db, lesson_id, incoming_slides)
        return fetch_active_lesson(db)

    current_slides = active_lesson.get("slides", [])
    total_after = len(current_slides) + len(incoming_slides)
    if total_after > MAX_LESSON_SLIDES:
        raise ValueError(f"Lesson can include up to {MAX_LESSON_SLIDES} slides.")

    lesson_id = active_lesson["id"]
    if title:
        db.execute("UPDATE lessons SET title = ?, updated_at = ? WHERE id = ?", (title, timestamp, lesson_id))

    new_index = len(current_slides)
    insert_lesson_slides(db, lesson_id, incoming_slides, start_position=new_index)
    db.execute(
        "UPDATE lessons SET current_slide_index = ?, updated_at = ? WHERE id = ?",
        (new_index, timestamp, lesson_id),
    )
    return fetch_active_lesson(db)


def build_prompt_stats_locked(db: sqlite3.Connection, include_names: bool = False) -> dict | None:
    active_prompt = fetch_active_prompt(db)
    if not active_prompt:
        return None

    if not include_names and not active_prompt.get("locked"):
        if active_prompt["type"] == "multiple_choice":
            return {
                "totalResponses": 0,
                "options": [
                    {"option": option, "count": 0} for option in active_prompt["options"]
                ],
            }
        return {"totalResponses": 0, "latestResponses": []}

    if active_prompt["type"] == "multiple_choice":
        counts = {option: 0 for option in active_prompt["options"]}
        rows = db.execute(
            "SELECT answer FROM responses WHERE prompt_id = ? ORDER BY id ASC",
            (active_prompt["id"],),
        ).fetchall()
        for row in rows:
            answer = row["answer"]
            if answer in counts:
                counts[answer] += 1

        return {
            "totalResponses": len(rows),
            "options": [
                {"option": option, "count": counts[option]} for option in active_prompt["options"]
            ],
        }

    latest = db.execute(
        """
        SELECT *
        FROM responses
        WHERE prompt_id = ?
        ORDER BY id DESC
        LIMIT 20
        """,
        (active_prompt["id"],),
    ).fetchall()
    total_row = db.execute(
        "SELECT COUNT(*) AS count FROM responses WHERE prompt_id = ?",
        (active_prompt["id"],),
    ).fetchone()
    latest_responses = []
    for row in latest:
        entry = {
            "answer": row["answer"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }
        if include_names:
            entry["name"] = row["name"]
        latest_responses.append(entry)

    return {
        "totalResponses": total_row["count"],
        "latestResponses": latest_responses,
    }


def snapshot_state_locked(db: sqlite3.Connection, include_names: bool = False) -> dict:
    active_lesson, current_slide_index = fetch_active_lesson(db)
    submissions = fetch_submissions(db)
    return {
        "submissions": submissions,
        "count": len(submissions),
        "activePrompt": fetch_active_prompt(db),
        "promptStats": build_prompt_stats_locked(db, include_names=include_names),
        "activeLesson": active_lesson,
        "currentSlideIndex": current_slide_index,
        "presence": fetch_presence_summary(db) if include_names else None,
        "roster": fetch_roster_summary(db) if include_names else None,
        "serverTime": now_iso(),
        "stateVersion": get_state_version(db),
    }


def snapshot_state(include_names: bool = False) -> dict:
    with state_lock:
        with get_db() as db:
            return snapshot_state_locked(db, include_names=include_names)


def snapshot_instructor_state() -> dict:
    return snapshot_state(include_names=True)


def bump_state_version_locked(db: sqlite3.Connection) -> None:
    db.execute(
        """
        INSERT INTO app_meta (key, value)
        VALUES ('state_version', '1')
        ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)
        """
    )


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
    return jsonify(snapshot_state(include_names=False))


@app.get("/api/instructor/state")
def get_instructor_state():
    return jsonify(snapshot_instructor_state())


@app.get("/api/roster/lookup")
def lookup_roster_student():
    email = normalize_email(request.args.get("email", ""))
    if not email:
        return jsonify({"error": "Email is required."}), 400
    if not is_valid_email(email):
        return jsonify({"error": "Enter a valid email address."}), 400

    with state_lock:
        with get_db() as db:
            student = fetch_roster_student(db, email)

    if not student:
        return jsonify({"matched": False, "email": email})

    return jsonify(
        {
            "matched": True,
            "email": student["email"],
            "firstName": student["firstName"],
            "lastName": student["lastName"],
            "name": student["name"],
        }
    )


@app.post("/api/instructor/roster/upload")
def upload_roster():
    incoming = request.files.get("file")
    if not incoming:
        return jsonify({"error": "Upload a CSV file."}), 400

    filename = secure_filename(incoming.filename or "")
    if not filename.lower().endswith(".csv"):
        return jsonify({"error": "Roster must be a CSV file."}), 400

    try:
        students = parse_roster_csv(incoming)
    except UnicodeDecodeError:
        return jsonify({"error": "CSV must be UTF-8 encoded."}), 400
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    with state_lock:
        timestamp = now_iso()
        with get_db() as db:
            db.execute("DELETE FROM roster_students")
            for student in students:
                db.execute(
                    """
                    INSERT INTO roster_students
                        (email, first_name, last_name, display_name, uploaded_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        student["email"],
                        student["firstName"],
                        student["lastName"],
                        student["name"],
                        timestamp,
                    ),
                )
                existing = db.execute(
                    "SELECT identity FROM participants WHERE identity = ?",
                    (student["email"],),
                ).fetchone()
                if existing:
                    upsert_participant(
                        db,
                        student["email"],
                        student["name"],
                        timestamp,
                        email=student["email"],
                        first_name=student["firstName"],
                        last_name=student["lastName"],
                        roster_matched=True,
                    )
            bump_state_version_locked(db)

    return jsonify({"ok": True, "count": len(students)}), 201


@app.get("/api/prompt")
def get_prompt():
    snapshot = snapshot_state(include_names=False)
    return jsonify(
        {
            "activePrompt": snapshot["activePrompt"],
            "promptStats": snapshot["promptStats"],
            "serverTime": snapshot["serverTime"],
        }
    )


@app.get("/api/lesson")
def get_lesson():
    snapshot = snapshot_state(include_names=False)
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
            snapshot = snapshot_state(include_names=False)
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


@app.get("/api/instructor/stream")
def stream_instructor_state():
    @stream_with_context
    def event_stream():
        last_seen_version = -1
        last_ping = 0.0
        last_snapshot = 0.0

        while True:
            snapshot = snapshot_instructor_state()
            latest = snapshot["stateVersion"]
            now = time.time()

            if latest != last_seen_version or now - last_snapshot > 10:
                last_seen_version = latest
                last_snapshot = now
                yield f"event: snapshot\ndata: {json.dumps(snapshot)}\n\n"

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
    message = str(payload.get("message", "")).strip()

    if not message:
        return jsonify({"error": "Message is required."}), 400

    if len(message) > MAX_MESSAGE_LENGTH:
        return (
            jsonify(
                {
                    "error": f"Message must be {MAX_MESSAGE_LENGTH} characters or fewer."
                }
            ),
            400,
        )

    with state_lock:
        timestamp = now_iso()
        with get_db() as db:
            participant, error = participant_from_payload(db, payload)
            if error:
                return jsonify({"error": error}), 400
            upsert_participant(
                db,
                participant["identity"],
                participant["name"],
                timestamp,
                email=participant["email"],
                first_name=participant["firstName"],
                last_name=participant["lastName"],
                roster_matched=participant["rosterMatched"],
            )
            cursor = db.execute(
                """
                INSERT INTO submissions (identity, name, message, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (participant["identity"], participant["name"], message, timestamp),
            )
            submission = {
                "id": cursor.lastrowid,
                "name": participant["name"],
                "message": message,
                "createdAt": timestamp,
            }
            trim_table_by_id(db, "submissions", MAX_STORED_SUBMISSIONS)
            bump_state_version_locked(db)

    return jsonify({"ok": True, "submission": submission}), 201


@app.post("/api/presence/heartbeat")
def heartbeat_presence():
    payload = request.get_json(silent=True) or {}
    session_id = str(payload.get("sessionId", "")).strip()

    if not session_id or len(session_id) > 120:
        return jsonify({"error": "A valid sessionId is required."}), 400

    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown")
    user_agent = request.headers.get("User-Agent", "")
    with state_lock:
        timestamp = now_iso()
        with get_db() as db:
            participant, error = participant_from_payload(db, payload)
            if error:
                return jsonify({"error": error}), 400
            upsert_participant(
                db,
                participant["identity"],
                participant["name"],
                timestamp,
                email=participant["email"],
                first_name=participant["firstName"],
                last_name=participant["lastName"],
                roster_matched=participant["rosterMatched"],
            )
            existing = db.execute(
                "SELECT * FROM presence_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            was_disconnected = bool(existing and existing["disconnected_at"])
            if existing:
                db.execute(
                    """
                    UPDATE presence_sessions
                    SET identity = ?,
                        name = ?,
                        last_seen_at = ?,
                        disconnected_at = NULL,
                        disconnect_reason = NULL,
                        user_agent = ?,
                        ip_address = ?
                    WHERE session_id = ?
                    """,
                    (participant["identity"], participant["name"], timestamp, user_agent, client_ip, session_id),
                )
                if was_disconnected:
                    record_presence_event(db, session_id, participant["identity"], participant["name"], "connect", timestamp)
            else:
                db.execute(
                    """
                    INSERT INTO presence_sessions
                        (session_id, identity, name, connected_at, last_seen_at, user_agent, ip_address)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        participant["identity"],
                        participant["name"],
                        timestamp,
                        timestamp,
                        user_agent,
                        client_ip,
                    ),
                )
                record_presence_event(db, session_id, participant["identity"], participant["name"], "connect", timestamp)

            bump_state_version_locked(db)

    return jsonify({"ok": True}), 200


@app.post("/api/presence/disconnect")
def disconnect_presence():
    payload = request.get_json(silent=True) or {}
    session_id = str(payload.get("sessionId", "")).strip()
    email = normalize_email(payload.get("email", ""))

    if not session_id:
        return jsonify({"ok": True}), 200

    with state_lock:
        timestamp = now_iso()
        with get_db() as db:
            existing = db.execute(
                """
                SELECT *
                FROM presence_sessions
                WHERE session_id = ? AND (? = '' OR identity = ?) AND disconnected_at IS NULL
                """,
                (session_id, email, email),
            ).fetchone()
            if existing:
                db.execute(
                    """
                    UPDATE presence_sessions
                    SET disconnected_at = ?, disconnect_reason = 'client'
                    WHERE session_id = ?
                    """,
                    (timestamp, session_id),
                )
                record_presence_event(
                    db,
                    session_id,
                    existing["identity"],
                    existing["name"],
                    "disconnect",
                    timestamp,
                    reason="client",
                )
                bump_state_version_locked(db)

    return jsonify({"ok": True}), 200


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

    with state_lock:
        timestamp = now_iso()
        with get_db() as db:
            db.execute("UPDATE prompts SET is_active = 0, closed_at = ? WHERE is_active = 1", (timestamp,))
            cursor = db.execute(
                """
                INSERT INTO prompts (type, prompt, options_json, locked, is_active, created_at)
                VALUES (?, ?, ?, 0, 1, ?)
                """,
                (question_type, prompt_text, json.dumps(options), timestamp),
            )
            active_prompt = {
                "id": cursor.lastrowid,
                "type": question_type,
                "prompt": prompt_text,
                "options": options,
                "locked": False,
                "createdAt": timestamp,
            }
            bump_state_version_locked(db)

    return jsonify({"ok": True, "activePrompt": active_prompt}), 201


@app.post("/api/instructor/prompt/close")
def close_prompt():
    with state_lock:
        with get_db() as db:
            active_prompt = fetch_active_prompt(db)
            if not active_prompt:
                return jsonify({"error": "No active prompt to close."}), 404

            db.execute(
                "UPDATE prompts SET is_active = 0, closed_at = ? WHERE id = ?",
                (now_iso(), active_prompt["id"]),
            )
            bump_state_version_locked(db)

    return jsonify({"ok": True})


@app.post("/api/instructor/prompt/lock")
def lock_prompt():
    with state_lock:
        with get_db() as db:
            active_prompt = fetch_active_prompt(db)
            if not active_prompt:
                return jsonify({"error": "No active prompt to lock."}), 404

            if active_prompt.get("locked"):
                return jsonify({"error": "Prompt is already locked."}), 400

            locked_at = now_iso()
            db.execute(
                "UPDATE prompts SET locked = 1, locked_at = ? WHERE id = ?",
                (locked_at, active_prompt["id"]),
            )
            active_prompt["locked"] = True
            active_prompt["lockedAt"] = locked_at
            bump_state_version_locked(db)

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
    answer = str(payload.get("answer", "")).strip()

    if not answer:
        return jsonify({"error": "Answer is required."}), 400

    with state_lock:
        with get_db() as db:
            participant, error = participant_from_payload(db, payload)
            if error:
                return jsonify({"error": error}), 400

            active_prompt = fetch_active_prompt(db)
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

            timestamp = now_iso()
            upsert_participant(
                db,
                participant["identity"],
                participant["name"],
                timestamp,
                email=participant["email"],
                first_name=participant["firstName"],
                last_name=participant["lastName"],
                roster_matched=participant["rosterMatched"],
            )
            existing = db.execute(
                "SELECT * FROM responses WHERE prompt_id = ? AND identity = ?",
                (active_prompt["id"], participant["identity"]),
            ).fetchone()
            if existing:
                db.execute(
                    """
                    UPDATE responses
                    SET name = ?, answer = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (participant["name"], answer, timestamp, existing["id"]),
                )
                row = db.execute("SELECT * FROM responses WHERE id = ?", (existing["id"],)).fetchone()
            else:
                cursor = db.execute(
                    """
                    INSERT INTO responses (prompt_id, identity, name, answer, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, NULL)
                    """,
                    (active_prompt["id"], participant["identity"], participant["name"], answer, timestamp),
                )
                row = db.execute("SELECT * FROM responses WHERE id = ?", (cursor.lastrowid,)).fetchone()

            trim_table_by_id(db, "responses", MAX_STORED_RESPONSES)
            bump_state_version_locked(db)
            response = row_to_response(row)

    return jsonify({"ok": True, "response": response}), 201


@app.post("/api/instructor/lesson/custom")
def create_custom_lesson():
    payload = request.get_json(silent=True) or {}
    title = str(payload.get("title", "")).strip()
    mode = normalize_lesson_mode(payload.get("mode", "append"))
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

    with state_lock:
        with get_db() as db:
            try:
                updated_lesson, updated_index = apply_lesson_update_locked(db, title, slides, mode)
            except ValueError as exc:
                db.rollback()
                return jsonify({"error": str(exc)}), 400

            bump_state_version_locked(db)

    return jsonify({"ok": True, "activeLesson": updated_lesson, "currentSlideIndex": updated_index}), 201


@app.post("/api/instructor/lesson/upload-images")
def upload_image_lesson():
    title = str(request.form.get("title", "")).strip()
    mode = normalize_lesson_mode(request.form.get("mode", "append"))
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
                "originalName": original_name,
                "storedName": stored_name,
                "relativePath": str(Path("uploads") / stored_name),
                "contentType": incoming.mimetype,
                "sizeBytes": destination.stat().st_size,
            }
        )

    with state_lock:
        with get_db() as db:
            for slide in slides:
                cursor = db.execute(
                    """
                    INSERT INTO uploaded_files
                        (original_name, stored_name, relative_path, url, content_type, size_bytes, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        slide["originalName"],
                        slide["storedName"],
                        slide["relativePath"],
                        slide["imageUrl"],
                        slide["contentType"],
                        slide["sizeBytes"],
                        now_iso(),
                    ),
                )
                slide["uploadId"] = cursor.lastrowid

            try:
                updated_lesson, updated_index = apply_lesson_update_locked(db, title, slides, mode)
            except ValueError as exc:
                db.rollback()
                return jsonify({"error": str(exc)}), 400

            bump_state_version_locked(db)

    return jsonify({"ok": True, "activeLesson": updated_lesson, "currentSlideIndex": updated_index}), 201


@app.post("/api/instructor/lesson/navigate")
def navigate_lesson():
    payload = request.get_json(silent=True) or {}
    direction = str(payload.get("direction", "")).strip().lower()
    requested_index = payload.get("index")

    with state_lock:
        with get_db() as db:
            active_lesson, current_slide_index = fetch_active_lesson(db)
            if not active_lesson:
                return jsonify({"error": "No active lesson."}), 404

            total = len(active_lesson.get("slides", []))
            if total == 0:
                return jsonify({"error": "Active lesson has no slides."}), 400

            if isinstance(requested_index, int):
                updated_index = clamp_slide_index(requested_index, total)
            elif direction == "next":
                updated_index = clamp_slide_index(current_slide_index + 1, total)
            elif direction == "prev":
                updated_index = clamp_slide_index(current_slide_index - 1, total)
            else:
                return jsonify({"error": "Provide direction (next/prev) or a numeric index."}), 400

            db.execute(
                "UPDATE lessons SET current_slide_index = ?, updated_at = ? WHERE id = ?",
                (updated_index, now_iso(), active_lesson["id"]),
            )
            bump_state_version_locked(db)
            snapshot = snapshot_state_locked(db)

    return jsonify(
        {
            "ok": True,
            "activeLesson": snapshot["activeLesson"],
            "currentSlideIndex": snapshot["currentSlideIndex"],
        }
    )


@app.post("/api/instructor/lesson/remove-slide")
def remove_lesson_slide():
    payload = request.get_json(silent=True) or {}
    requested_index = payload.get("index")
    if not isinstance(requested_index, int):
        return jsonify({"error": "A numeric slide index is required."}), 400

    with state_lock:
        with get_db() as db:
            active_lesson, current_slide_index = fetch_active_lesson(db)
            if not active_lesson:
                return jsonify({"error": "No active lesson."}), 404

            slides = active_lesson.get("slides", [])
            if requested_index < 0 or requested_index >= len(slides):
                return jsonify({"error": "Slide index is out of range."}), 400

            lesson_id = active_lesson["id"]
            db.execute(
                "DELETE FROM lesson_slides WHERE lesson_id = ? AND position = ?",
                (lesson_id, requested_index),
            )
            db.execute(
                """
                UPDATE lesson_slides
                SET position = position - 1
                WHERE lesson_id = ? AND position > ?
                """,
                (lesson_id, requested_index),
            )

            remaining = len(slides) - 1
            if remaining <= 0:
                db.execute("UPDATE lessons SET is_active = 0, current_slide_index = 0, updated_at = ? WHERE id = ?", (now_iso(), lesson_id))
            else:
                if current_slide_index > requested_index:
                    current_slide_index -= 1
                elif current_slide_index == requested_index:
                    current_slide_index = clamp_slide_index(current_slide_index, remaining)

                db.execute(
                    "UPDATE lessons SET current_slide_index = ?, updated_at = ? WHERE id = ?",
                    (current_slide_index, now_iso(), lesson_id),
                )

            bump_state_version_locked(db)
            snapshot = snapshot_state_locked(db, include_names=True)

    return jsonify(
        {
            "ok": True,
            "activeLesson": snapshot["activeLesson"],
            "currentSlideIndex": snapshot["currentSlideIndex"],
        }
    )


@app.post("/api/instructor/lesson/reorder-slides")
def reorder_lesson_slides():
    payload = request.get_json(silent=True) or {}
    from_index = payload.get("fromIndex")
    to_index = payload.get("toIndex")

    if not isinstance(from_index, int) or not isinstance(to_index, int):
        return jsonify({"error": "fromIndex and toIndex must be numeric indices."}), 400

    with state_lock:
        with get_db() as db:
            active_lesson, current_slide_index = fetch_active_lesson(db)
            if not active_lesson:
                return jsonify({"error": "No active lesson."}), 404

            slides = active_lesson.get("slides", [])
            total = len(slides)
            if total <= 1:
                return jsonify({"error": "Need at least two slides to reorder."}), 400

            if from_index < 0 or from_index >= total or to_index < 0 or to_index >= total:
                return jsonify({"error": "Slide index is out of range."}), 400

            if from_index == to_index:
                snapshot = snapshot_state_locked(db, include_names=True)
                return jsonify(
                    {
                        "ok": True,
                        "activeLesson": snapshot["activeLesson"],
                        "currentSlideIndex": snapshot["currentSlideIndex"],
                    }
                )

            slide_rows = db.execute(
                """
                SELECT id
                FROM lesson_slides
                WHERE lesson_id = ?
                ORDER BY position ASC
                """,
                (active_lesson["id"],),
            ).fetchall()
            ordered_ids = [row["id"] for row in slide_rows]
            moving_id = ordered_ids.pop(from_index)
            ordered_ids.insert(to_index, moving_id)
            for position, slide_id in enumerate(ordered_ids):
                db.execute("UPDATE lesson_slides SET position = ? WHERE id = ?", (position, slide_id))

            if current_slide_index == from_index:
                current_slide_index = to_index
            elif from_index < current_slide_index <= to_index:
                current_slide_index -= 1
            elif to_index <= current_slide_index < from_index:
                current_slide_index += 1

            db.execute(
                "UPDATE lessons SET current_slide_index = ?, updated_at = ? WHERE id = ?",
                (current_slide_index, now_iso(), active_lesson["id"]),
            )
            bump_state_version_locked(db)
            snapshot = snapshot_state_locked(db, include_names=True)

    return jsonify(
        {
            "ok": True,
            "activeLesson": snapshot["activeLesson"],
            "currentSlideIndex": snapshot["currentSlideIndex"],
        }
    )


@app.post("/api/instructor/lesson/clear")
def clear_lesson():
    with state_lock:
        with get_db() as db:
            db.execute(
                "UPDATE lessons SET is_active = 0, current_slide_index = 0, updated_at = ? WHERE is_active = 1",
                (now_iso(),),
            )
            bump_state_version_locked(db)

    return jsonify({"ok": True})


init_db()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
