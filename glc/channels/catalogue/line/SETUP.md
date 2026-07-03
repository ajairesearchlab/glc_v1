# LINE Channel — End-to-End Setup Guide

This guide walks a new team member through connecting the GLC gateway to a live LINE bot. It
covers every step needed: installing dependencies, getting LINE credentials, opening a public
tunnel, and verifying messages flow in both directions.

**Repo:** https://github.com/ajairesearchlab/glc_v1

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | Download from **python.org** — do NOT use the Microsoft Store version, it has SSL restrictions on Windows |
| LINE developer account | Free at https://developers.line.biz/ |
| A LINE channel (Messaging API type) | Created in the LINE Developer Console |
| cloudflared binary | See Stage C.2 — needed for the public HTTPS tunnel |
| Windows with Norton antivirus | See "Windows / Norton TLS note" below |

### Windows / Norton TLS note

Norton Antivirus intercepts all outbound HTTPS connections and re-signs them with its own
certificate. This breaks ngrok (authentication failure), serveo.net, and localhost.run.
**cloudflared** is the solution: it falls back to QUIC (UDP port 7844) which Norton does
not intercept. The `truststore` library handles the same issue for Python's own SSL
connections (LINE API calls).

Both fixes are already wired into the codebase — you just need to install the right tools.

---

## Stage A — Repository Setup

```powershell
# 1. Clone
git clone https://github.com/ajairesearchlab/glc_v1.git
cd glc_v1

# 2. Create a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 3. Install the project and ALL dependencies (including truststore on Windows)
python -m pip install -e .

# 4. Install dev tools (run tests, lint, type-check)
python -m pip install pytest pytest-asyncio pytest-cov ruff mypy
```

> **Do not use the .exe wrappers** (`ruff.exe`, `pytest.exe`, `mypy.exe`) directly.
> On Windows the wrappers embed an absolute path to the original Python at venv creation
> time; if the venv was ever copied or moved those paths are stale and the launchers will
> crash with "Unable to create process". Always use `python -m <tool>` instead:
> ```
> python -m pytest tests/channels/test_line.py -v
> python -m ruff check glc/channels/catalogue/line/
> python -m mypy glc/channels/catalogue/line/
> ```

---

## Stage B — Start the GLC Gateway

Open **Terminal 1** and start the gateway. Run from the `glc_v1/` directory:

```powershell
python -m uvicorn glc.main:app --host 0.0.0.0 --port 8111
```

Alternatively, if the CLI script is installed:

```powershell
python -m glc.cli serve
```

The gateway logs something like:

```
INFO:     Uvicorn running on http://0.0.0.0:8111
```

### Get the install token

The gateway generates a per-installation token on first boot and saves it to
`~/.glc/install_token`. Retrieve it with either:

```powershell
# Option A: CLI
python -m glc.cli token

# Option B: read the file directly
Get-Content "$env:USERPROFILE\.glc\install_token"
```

Copy this token — you will put it in `.env` as `GLC_INSTALL_TOKEN`.

---

## Stage C — LINE Channel Setup

### C.1 — Collect credentials from LINE Developer Console

1. Go to https://developers.line.biz/ → your provider → your Messaging API channel.
2. **Basic settings** tab → **Channel secret** — copy it.
3. **Messaging API** tab → **Channel access token (long-lived)** → click **Issue** → copy it.
4. Note your **Bot basic ID** (`@XXXXXXXX`) — optional, documentation only.

### C.2 — Download and start cloudflared (Windows)

cloudflared creates a secure public HTTPS tunnel to your local runner. It uses QUIC (UDP)
which bypasses Norton's TCP HTTPS interception.

```powershell
# Download the binary once
Invoke-WebRequest `
  -Uri "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" `
  -OutFile "$env:USERPROFILE\Downloads\cloudflared.exe"
```

Open **Terminal 2** and start the tunnel (runner listens on port 8120):

```powershell
& "$env:USERPROFILE\Downloads\cloudflared.exe" tunnel --url http://localhost:8120
```

Wait for a line like:

```
Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):
https://example-words-here.trycloudflare.com
```

Copy that `https://...trycloudflare.com` URL — you will register it in LINE next.

> **Important:** This URL changes every time you restart cloudflared. You must re-register
> the webhook URL in LINE Console after each restart. For a permanent URL you need a named
> Cloudflare Tunnel (free Cloudflare account required).

### C.3 — Create the .env file

Copy the example and fill in real values:

```powershell
# Run from glc_v1/  (NOT from inside the line subdirectory)
Copy-Item glc\channels\catalogue\line\.env.example .env
```

Edit `glc_v1/.env`:

```dotenv
LINE_CHANNEL_SECRET=<paste your channel secret here>
LINE_CHANNEL_ACCESS_TOKEN=<paste your long-lived token here>
GLC_INSTALL_TOKEN=<paste the token from Stage B>

# These defaults work for local dev — leave them unless you changed ports
GLC_GATEWAY_WS_URL=ws://localhost:8111/v1/channels/line
LINE_WEBHOOK_HOST=0.0.0.0
LINE_WEBHOOK_PORT=8120
LINE_WEBHOOK_PATH=/webhook/line
LINE_BOT_BASIC_ID=@yourBotId
```

> The `.env` file must live at `glc_v1/.env` (project root), not inside the line
> subfolder. The runner calls `load_dotenv()` from its working directory and Python's
> dotenv library searches upward from there.

### C.4 — Register the webhook URL in LINE Console

1. In LINE Developer Console → your channel → **Messaging API** tab.
2. **Webhook URL** → paste: `https://example-words-here.trycloudflare.com/webhook/line`
3. Toggle **Use webhook** ON.
4. Click **Verify** — LINE sends a test POST with an empty `events` array. The runner
   responds 200 immediately (it just skips the empty array). You should see ✓ **Success**.

> Note: LINE's Verify does not log in the runner because no events are processed — only
> a 200 OK is returned. This is normal.

### C.5 — Enable the LINE channel in channels.yaml

The gateway allowlists channels in `glc/channels.yaml`. Enable LINE and add your LINE
user ID to `allowed_senders`:

```yaml
# glc/channels.yaml
channels:
  line:
    enabled: true
    allowed_senders: ["U<your_line_user_id>"]
```

To find your LINE user ID: send the bot any message and look for `user=U...` in the
runner logs after Stage C.6.

Alternatively, use the GLC pairing API once the runner is up:

```powershell
Invoke-WebRequest -Uri "http://localhost:8111/v1/control/pair" `
  -Method Post `
  -ContentType "application/json" `
  -Headers @{Authorization="Bearer <install_token>"} `
  -Body '{"channel":"line","channel_user_id":"U<your_id>","role":"owner"}'
```

### C.6 — Start the LINE runner

Open **Terminal 3** and run from `glc_v1/`:

```powershell
python -m glc.channels.catalogue.line.runner
```

You should see:

```
INFO     glc.line.runner :: LINE runner starting -- webhook on 0.0.0.0:8120/webhook/line -> gateway ws://localhost:8111/v1/channels/line
INFO     glc.line.runner :: connecting WS -> ws://localhost:8111/v1/channels/line
INFO     glc.line.runner :: WS connected
```

If you see `WS error: ... ConnectionRefusedError` the gateway (Terminal 1) is not running.

---

## Stage D — Verify End to End

1. Open LINE on your phone and send a message to your bot.
2. In the runner terminal you should see:
   ```
   INFO  glc.line.runner :: inbound user=U<id> trust=owner text='Hello'
   INFO  glc.line.runner :: outbound user=U<id> text='[glc echo] Hello'
   ```
3. Within a second or two the bot should reply in LINE with `[glc echo] Hello`.

---

## Running the tests

All LINE adapter tests are mocked — no credentials needed:

```powershell
python -m pytest tests/channels/test_line.py -v
```

Expected: 7 tests, all green. Run these before starting a live session to confirm nothing
is broken locally.

---

## 4-Terminal Layout

| Terminal | Command | Purpose |
|---|---|---|
| 1 | `python -m uvicorn glc.main:app --host 0.0.0.0 --port 8111` | GLC gateway |
| 2 | `& "$env:USERPROFILE\Downloads\cloudflared.exe" tunnel --url http://localhost:8120` | Public HTTPS tunnel |
| 3 | `python -m glc.channels.catalogue.line.runner` | LINE webhook receiver + WS bridge |
| 4 | (Optional) watch logs | Tail any of the above |

Start in order: gateway first, then cloudflared, then register the new URL in LINE Console,
then start the runner.

---

## Daily Restart Procedure

cloudflared's free quick tunnels generate a **new URL on every restart**. After restarting:

1. Start cloudflared (Terminal 2) and copy the new URL from its output.
2. Go to LINE Developer Console → **Messaging API** tab → **Webhook URL** → paste the new
   URL (append `/webhook/line`) → click **Verify** → confirm ✓ Success.
3. Start the runner (Terminal 3).

If you want a permanent URL, create a named Cloudflare Tunnel with a free Cloudflare
account (`cloudflared tunnel create my-line-bot`) and map it to port 8120.

---

## Common Issues

### "WS error: ConnectionRefusedError"
The gateway is not running. Start Terminal 1 first.

### "gateway control frame: dropped: channel 'line' is disabled in channels.yaml"
`line: enabled: false` in `glc/channels.yaml`. Change it to `enabled: true`.

### "gateway control frame: dropped: sender not in allowlist"
Your LINE user ID is not in `allowed_senders` in `channels.yaml`. Add it.

### "missing required env var: 'LINE_CHANNEL_SECRET'"
The `.env` file is missing or in the wrong location. It must be at `glc_v1/.env`.
Make sure you ran `Copy-Item glc\channels\catalogue\line\.env.example .env` from `glc_v1/`.

### "No module named 'truststore'"
Run `python -m pip install -e .` — this installs truststore (Windows-only dependency
declared in `pyproject.toml`). If that still fails: `python -m pip install truststore`.

### "No module named 'pydantic'" or other missing imports
The project wasn't installed as a package. Run `python -m pip install -e .` from `glc_v1/`.

### LINE Verify fails / times out
- Confirm cloudflared is running and shows a URL.
- Confirm the URL in LINE Console has `/webhook/line` appended.
- Confirm the runner is running (Terminal 3) — Verify won't reach the webhook without it.
- If cloudflared was restarted since Verify last worked, register the new URL.

### ngrok / serveo / localhost.run tunnel failures
These do not work on Windows with Norton Antivirus:
- **ngrok**: Norton intercepts its HTTPS authentication → TLS certificate error.
- **serveo.net**: SSH connection closes immediately or shows Norton interstitial.
- **localhost.run**: SSH connection closes immediately.
Use **cloudflared** (QUIC/UDP, Norton-transparent) instead.

### Pytest loads wrong conftest / pydantic missing
This happens when the project is not installed. Run `python -m pip install -e .` first.
