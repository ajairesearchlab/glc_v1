# LINE Channel — Architecture Reference

This document describes the design of the LINE Messaging API integration within GLC v1.
It is written for team members who need to understand, debug, or extend the code.

---

## Overview

The LINE channel bridges the LINE Messaging API and the GLC gateway. It is implemented as
a standalone Python process (the "runner") that:

1. Hosts a FastAPI webhook receiver on port 8120 to accept events from LINE.
2. Maintains a WebSocket connection to the GLC gateway on port 8111.
3. Translates between LINE's wire format and GLC's internal `ChannelMessage`/`ChannelReply` envelopes.

A public HTTPS tunnel (cloudflared) exposes port 8120 so that LINE's servers can reach the
runner. On Windows, `truststore` is used to patch Python's SSL so it trusts the Windows
certificate store (needed when Norton Antivirus acts as a TLS interceptor).

```
LINE servers
    │  HTTPS POST (webhook)
    ▼
cloudflared (QUIC tunnel)
    │  HTTP POST localhost:8120
    ▼
webhook.py  ──── signature verify (HMAC-SHA256)
    │             deduplicate (webhookEventId)
    ▼
runner.on_webhook_event  ──── event type filter
    │                          (non-message events handled here)
    ▼
adapter.on_message  ──── always returns ChannelMessage
    │
    ▼
WebSocket → GLC gateway (port 8111)
    │  allowlist check, agent dispatch
    ▼
agent reply → ChannelReply (JSON via WS)
    │
    ▼
adapter.send  ──── reply token check
    │               /reply (free) or /push (metered)
    ▼
LINE user receives message
```

---

## File-by-file Description

### `adapter.py` — Pure Message Translator

The adapter implements the `ChannelAdapter` ABC (`glc/channels/base.py`) and has exactly
two public methods:

**`on_message(raw) → ChannelMessage`**

Translates a single verified LINE event payload into a `ChannelMessage`. The adapter
assumes the caller (runner) has already filtered for `type == "message"` events, so
this method always returns a `ChannelMessage` — it never returns `None` and has no
special-case paths for lifecycle events.

Key behaviour:
- Extracts `userId` / `groupId` / `roomId` from `event.source` as the user identity.
- Stashes the event's `replyToken` in an in-memory TTL dict keyed by `channel_user_id`.
- Returns structured metadata: `line_message_id`, `line_message_type`, `destination`,
  `timestamp_ms`, `source_type`.

```python
async def on_message(self, raw: Any) -> ChannelMessage:
    # Runner guarantees only "message" events reach here — no None path.
    events = (raw or {}).get("events") or []
    event = events[0] if events else {}
    source = event.get("source", {}) or {}
    user_id = (source.get("userId") or source.get("groupId") or source.get("roomId") or "")
    msg_obj = event.get("message", {}) or {}
    msg_type = msg_obj.get("type")
    text = msg_obj.get("text") if msg_type == "text" else None
    reply_token = event.get("replyToken")
    if reply_token and user_id:
        self._reply_tokens[user_id] = (reply_token, time.time() + REPLY_TOKEN_TTL_SECONDS)
    trust = classify("line", user_id)
    return ChannelMessage(channel="line", channel_user_id=user_id, ...)
```

**`send(reply) → Any`**

Sends an outbound message. Pops the user's reply token if one is fresh (< 60 s old)
and uses the `/reply` endpoint (free, no quota). Falls back to `/push` (counts against
monthly quota) if no fresh token exists. Handles a transparent retry if LINE rejects
the token as expired.

**What `adapter.py` does NOT do:**
- Does not filter events by type (that is the runner's job).
- Does not manage the HTTP client (that is `live_client.py`'s job).
- Does not open any network connections itself.

---

### `runner.py` — Process Entry Point and Event Router

The runner is the executable process. It owns the adapter, the live HTTP client, and the
WebSocket connection to the gateway.

**Key design decision: event type filtering happens here, not in the adapter.**

`on_webhook_event` receives every verified, deduplicated LINE event and filters by type
before calling the adapter. This keeps the adapter's contract clean: it is only ever called
with a `"message"` event and can always return a `ChannelMessage`.

```python
async def on_webhook_event(self, payload: dict[str, Any]) -> None:
    events = payload.get("events") or []
    event = events[0] if events else {}
    event_type = event.get("type")

    if event_type != "message":
        # Lifecycle events (follow, unfollow, join, leave, postback) are logged here.
        if event_type in ("follow", "unfollow", "join", "leave", "postback"):
            log.info("lifecycle event type=%s source=%s", event_type, event.get("source", {}))
        elif event_type:
            log.debug("ignoring event type=%s", event_type)
        return  # <-- non-message events stop here, never reach adapter

    # Only message events reach the adapter.
    envelope = await self.adapter.on_message(payload)
    await self._send_to_gateway(envelope.model_dump_json())
```

**WebSocket management:**

`_ws_loop` maintains the connection to the gateway with exponential backoff reconnection
(1s → 2s → 4s … → 30s cap). The WS socket reference is stored in `self._ws` and guarded
by `asyncio.Lock` so concurrent inbound dispatches do not race on sends.

**truststore — Windows SSL fix:**

```python
import truststore
# ... all other imports ...
truststore.inject_into_ssl()  # after all imports, before any network call
```

This patches Python's `ssl` module to read from the Windows certificate store instead of
the bundled OpenSSL trust bundle. Required on Windows when Norton Antivirus acts as a TLS
MITM proxy and re-signs LINE API responses with its own certificate. The call is placed
after all imports (satisfying ruff E402) and before any SSL connection is opened.

**What `runner.py` does NOT do:**
- Does not parse or verify LINE signatures (that is `webhook.py`'s job).
- Does not implement the LINE HTTP API directly (that is `live_client.py`'s job).

---

### `webhook.py` — FastAPI Webhook Receiver

A self-contained FastAPI application built by `build_webhook_app(...)`. It handles:

1. **Signature verification** — HMAC-SHA256 over the raw request bytes using the channel
   secret. Verified before JSON parsing to prevent spoofed events.
2. **Deduplication** — LRU dict of `webhookEventId` (capped at 4096 entries) prevents
   double-processing of LINE's automatic redeliveries.
3. **Fast response** — Returns 200 immediately. Each event is dispatched to `on_event`
   via `asyncio.create_task` (fire-and-forget) so LINE's ~1 s response deadline is always met.
4. **Verify button compatibility** — LINE's "Verify" button sends an empty `events` array.
   The webhook returns 200 OK without calling `on_event` (there's nothing to dispatch).

```
POST /webhook/line
  │  HMAC-SHA256 verify
  │  JSON parse
  │  for each event:
  │    dedupe by webhookEventId
  │    asyncio.create_task(on_event({destination, events: [ev]}))
  └→ 200 {"ok": true}
```

**What `webhook.py` does NOT do:**
- Does not know about LINE event types — it passes every verified event to `on_event`.
- Does not call the LINE API — it is purely inbound.

---

### `live_client.py` — LINE Messaging API HTTP Client

`LiveLineClient` is an async httpx wrapper for LINE's two outbound endpoints:

| Endpoint | URL | Cost |
|---|---|---|
| `/reply` | `https://api.line.me/v2/bot/message/reply` | Free (uses reply token) |
| `/push` | `https://api.line.me/v2/bot/message/push` | Counts against monthly quota |

`dispatch(body)` picks the endpoint based on which key is in the body:
- `"replyToken"` key → `/reply`
- `"to"` key → `/push`

Error handling:
- **429** — surfaces immediately (do not retry, let the caller log).
- **400 + "Invalid reply token"** — surfaces `{"invalid_reply_token": True}` so adapter
  can transparently fall back to `/push`.
- **5xx** — exponential backoff, up to 3 attempts.
- **401/403** — surfaces `{"auth_error": True}`, never retried (wrong token).

The client is instantiated once at runner startup and shared across all sends to reuse
the underlying httpx connection pool.

**What `live_client.py` does NOT do:**
- Does not manage reply tokens (that is the adapter's job).
- Does not handle retries for non-5xx errors.

---

### `schemas.py` — Configuration Model

`LiveLineConfig` is a Pydantic v2 `BaseModel` that validates all required environment
variables at startup. Fields:

| Field | Env Var | Default |
|---|---|---|
| `channel_secret` | `LINE_CHANNEL_SECRET` | required |
| `channel_access_token` | `LINE_CHANNEL_ACCESS_TOKEN` | required |
| `install_token` | `GLC_INSTALL_TOKEN` | required |
| `gateway_ws_url` | `GLC_GATEWAY_WS_URL` | `ws://localhost:8111/v1/channels/line` |
| `webhook_host` | `LINE_WEBHOOK_HOST` | `0.0.0.0` |
| `webhook_port` | `LINE_WEBHOOK_PORT` | `8120` |
| `webhook_path` | `LINE_WEBHOOK_PATH` | `/webhook/line` |
| `bot_basic_id` | `LINE_BOT_BASIC_ID` | `""` (docs only) |

`LiveLineConfig.from_env()` reads from `os.environ`. The runner calls `load_dotenv()` first
so values can also come from `.env`.

---

### `.env.example` — Credential Template

Template for the `.env` file. Copy to `glc_v1/.env` (project root — one directory above
`glc/`). Never commit `.env` — it is git-ignored.

```
LINE_CHANNEL_SECRET=your_channel_secret_here
LINE_CHANNEL_ACCESS_TOKEN=your_long_lived_channel_access_token_here
GLC_INSTALL_TOKEN=your_install_token_here
```

---

## Reply Token State Machine

LINE's webhook includes a `replyToken` with every inbound message. Tokens are:
- Valid for ~60 seconds after delivery.
- One-shot — using it a second time returns 400 "Invalid reply token".
- Free — calling `/reply` does not consume push quota.

The adapter maintains a per-user TTL dict `{channel_user_id: (token, expires_at)}`:

```
inbound event
  │  adapter.on_message stashes replyToken with TTL = now + 60s
  ▼
outbound reply
  │  adapter._consume_reply_token pops and checks TTL
  ├─ token fresh  → body = {"replyToken": ..., "messages": [...]} → /reply  (free)
  └─ no token / expired → body = {"to": user_id, "messages": [...]} → /push (metered)
         │
         └─ if LINE returns 400 invalid_reply_token  → retry as /push
```

---

## Trust Classification

`classify("line", user_id)` (from `glc.security.trust_level`) checks the user ID against
the owner/admin/blocked lists configured for the installation and returns a `TrustLevel`.
The runner does not act on trust level directly — it is carried in `ChannelMessage.trust_level`
and the gateway enforces policy.

---

## Error Handling Map

| Event | Handler | Action |
|---|---|---|
| Bad HMAC signature | `webhook.py` | 403, drop |
| Invalid JSON | `webhook.py` | 400, drop |
| Duplicate webhookEventId | `webhook.py` | skip silently |
| Non-message event (follow, etc.) | `runner.on_webhook_event` | log.info, return |
| Unknown event type | `runner.on_webhook_event` | log.debug, return |
| `adapter.on_message` exception | `runner.on_webhook_event` | log.exception, return |
| Gateway WS not connected | `runner._send_to_gateway` | log.warning, drop |
| Gateway WS closed mid-send | `runner._send_to_gateway` | clear `_ws`, reconnect loop picks up |
| LINE 429 rate limit | `live_client._post` | surface immediately |
| LINE 400 invalid reply token | `live_client._post` | `{"invalid_reply_token": True}` |
| LINE 5xx error | `live_client._post` | retry up to 3× with backoff |
| `adapter.send` exception | `runner._consume_gateway_replies` | log.exception |

---

## Configuration: channels.yaml

The gateway checks `glc/channels.yaml` before forwarding any message. The LINE section
must be enabled and the user's LINE ID must be in `allowed_senders`:

```yaml
channels:
  line:
    enabled: true
    allowed_senders: ["U<your_line_user_id>"]
```

If `enabled: false` or the sender is not in `allowed_senders`, the gateway sends back a
control frame like `{"error": "dropped: channel 'line' is disabled"}` and the message
is not forwarded to the agent.

---

## Testing

All 7 LINE tests in `tests/channels/test_line.py` use a `LineMock` in-process mock — no
real credentials or network connections needed.

```powershell
python -m pytest tests/channels/test_line.py -v
```

The mock covers:
- Basic text message round-trip (on_message → ChannelMessage)
- Non-text message types (sticker, image) — `text` is None, metadata preserved
- Reply token: first reply uses token (`/reply`), second uses `/push`
- Expired reply token: treated as no token (push)
- Invalid reply token from LINE API: adapter falls back to push
- Rate limit (429) propagated unchanged
- Disconnect handling (pop_disconnect drain)

---

## Production Notes

- **Webhook access log**: The runner starts uvicorn with `access_log=False` to reduce noise.
  HTTP request logs are suppressed; only application-level logs appear.
- **LINE Verify button**: Sends `{"events": [], "destination": "..."}`. The webhook returns
  200 without calling `on_event`. No runner log appears — this is expected.
- **cloudflared URL rotation**: Free quick tunnels get a new URL on every restart. Webhook
  URL must be re-registered in LINE Console after each restart.
- **Reply token TTL**: Set to 60 s to match LINE's documented expiry. In practice tokens
  may expire faster if the LINE platform is under load.
- **Push quota**: LINE free-tier bots have a monthly push message quota. Prefer reply tokens
  (inbound-triggered conversations) to avoid burning quota.
