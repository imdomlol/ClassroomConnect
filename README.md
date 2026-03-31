# ClassroomConnect

ClassroomConnect is a local-only classroom board that runs on your host device and is reachable by anyone connected to that device's Wi-Fi hotspot.

Version 1 behavior:
- Interactive posting from connected users
- Near-real-time updates via server-sent events (SSE) with 2-second polling fallback
- Instructor view for publishing multiple choice or free response prompts
- Student name entered once per browser session and reused for actions
- One response per student name per prompt, editable until instructor locks responses
- Student response results hidden from class until instructor locks the question
- Student view never displays responder names; names are instructor-only
- Instructor lesson display mode for text slides or uploaded image slides
- Live lesson navigation to keep all student screens in sync
- Open access for anyone on the hotspot network
- In-memory storage only (data resets when app restarts)

## Quick Start (Raspberry Pi / Linux Host)

1. Clone this repository on the host device.
2. Create and activate a virtual environment.
3. Install dependencies.
4. Run the app bound to all interfaces.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

The app listens on `0.0.0.0:5000` by default.

## Access from Client Devices

1. Connect phones/laptops to the host hotspot Wi-Fi.
2. Find the host hotspot IP (for example `192.168.4.1`).
3. Open this URL on any connected client:

```text
http://<HOST_IP>:5000
```

Example:

```text
http://192.168.4.1:5000
```

Instructor controls:

```text
http://<HOST_IP>:5000/instructor
```

## API Endpoints

- `GET /api/submissions`
	- Returns current submissions and server time.
- `GET /api/stream`
	- Streams student-safe live snapshot events (no responder names).
- `GET /api/instructor/state`
	- Returns instructor state including responder names for free-response review.
- `GET /api/instructor/stream`
	- Streams instructor state including responder names.
- `GET /api/prompt`
	- Returns active prompt and prompt response summary.
- `GET /api/lesson`
	- Returns active lesson deck and current slide index.
- `POST /api/submissions`
	- JSON body: `{ "name": "...", "message": "..." }`
	- Enforces basic validation and rate limiting.
- `POST /api/instructor/prompt`
	- JSON body: `{ "questionType": "multiple_choice"|"free_response", "prompt": "...", "options": ["..."] }`
	- Creates/replaces the active prompt.
- `POST /api/instructor/prompt/close`
	- Closes the active prompt.
- `POST /api/instructor/prompt/lock`
	- Locks the active prompt so no further response edits are allowed.
- `POST /api/prompt/respond`
	- JSON body: `{ "name": "...", "answer": "..." }`
	- Creates or updates that participant's single response for the active prompt while it is unlocked.
- `POST /api/instructor/lesson/custom`
	- JSON body: `{ "title": "...", "slides": [{ "title": "...", "body": "..." }] }`
	- Publishes a text-based lesson deck.
- `POST /api/instructor/lesson/upload-images`
	- Multipart form data: `title` and `files` (image slides)
	- Publishes an image-based lesson deck.
- `POST /api/instructor/lesson/navigate`
	- JSON body: `{ "direction": "next"|"prev" }` or `{ "index": 0 }`
	- Moves the lesson to a different slide.
- `POST /api/instructor/lesson/clear`
	- Clears the active lesson deck.

## Safety Limits in v1

- Name max length: 40
- Message max length: 280
- Max retained posts: 200
- Per-IP request limit: 10 requests per 10 seconds

## Run on Boot with systemd (Optional)

This repo includes a service template at `systemd/classroomconnect.service`.

1. Edit `User`, `WorkingDirectory`, and `ExecStart` to match your device.
2. Copy to systemd location:

```bash
sudo cp systemd/classroomconnect.service /etc/systemd/system/classroomconnect.service
```

3. Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable classroomconnect
sudo systemctl start classroomconnect
```

4. Check status:

```bash
sudo systemctl status classroomconnect
```

## Troubleshooting

- Clients cannot open the page:
	- Confirm app is running on host and bound to `0.0.0.0`.
	- Confirm client is on the same hotspot network.
	- Confirm firewall allows TCP port `5000`.
- Posts do not appear quickly:
	- Clients use SSE when available and fall back to 2-second polling.
- Data disappeared after restart:
	- Expected in v1; storage is in memory only.