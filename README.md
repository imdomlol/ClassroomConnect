# ClassroomConnect

ClassroomConnect is a local-only classroom board that runs on your host device and is reachable by anyone connected to that device's Wi-Fi hotspot.

Version 1 behavior:
- Interactive posting from connected users
- Near-real-time updates via 2-second polling
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

## API Endpoints

- `GET /api/submissions`
	- Returns current submissions and server time.
- `POST /api/submissions`
	- JSON body: `{ "name": "...", "message": "..." }`
	- Enforces basic validation and rate limiting.

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
	- Feed refreshes every 2 seconds by design.
- Data disappeared after restart:
	- Expected in v1; storage is in memory only.