"""FastAPI webhook receiver for the LINE Messaging API.
LINE POSTs every event (message, follow, postback, etc.) to a single webhook URL,
signed via the X-Line-Signature header.
The signature is HMAC-SHA256 of the RAW REQUEST BODY (not the parsed JSON) using the
channel secret as the key.

Three production rules:
  1. Verify the signature over the raw bytes BEFORE parsing.
  2. Return 200 within ~1s — LINE retries on non-2xx.
    Long work goes onto an asyncio task so the response returns fast.
  3. Deduplicate by webhookEventId — LINE may redeliver the same event.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse

log = logging.getLogger("glc.line.webhook")

# Bounded LRU for webhookEventId dedupe — survives one process lifetime.
# LINE redelivers within minutes, so 4096 entries is comfortable.

_DEDUPE_MAX = 4096

def _verify_signature(secret: str, raw_body: bytes, header_sig: str) -> bool:
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, header_sig or "")

def build_webhook_app(
    *,
    channel_secret: str,
    webhook_path: str,
    on_event: Callable[[dict[str, Any]], Awaitable[None]],
) -> FastAPI:

    """Builds a FastAPI app that handles LINE webhooks.
    `on_event` is awaited in the background for each individual event after
    signature verification + dedupe. Errors inside `on_event` are logged but
    do not affect the 200 returned to LINE.
    """
    app = FastAPI(title="GLC LINE webhook receiver")
    seen_event_ids: OrderedDict[str, None] = OrderedDict()

    def _remember(eid: str) -> bool:
        """Returns True if this event id is new, False if duplicate."""
        if eid in seen_event_ids:
            return False
        seen_event_ids[eid] = None
        if len(seen_event_ids) > _DEDUPE_MAX:
            seen_event_ids.popitem(last=False)
        return True

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.post(webhook_path)
    async def line_webhook(request: Request) -> JSONResponse:
        raw_body = await request.body()
        sig = request.headers.get("X-Line-Signature")

        if not _verify_signature(channel_secret, raw_body, sig or ""):
            log.warning("rejected webhook: bad/missing X-Line-Signature")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bad signature")
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as e:
            log.warning("rejected webhook: invalid JSON (%r)", e)
            raise HTTPException(status_code=400, detail="invalid JSON") from None

        # LINE's "Verify" button posts an empty events array; respond 200 cleanly.
        events = payload.get("events") or []
        destination = payload.get("destination")

        for ev in events:
            eid = ev.get("webhookEventId") or ""
            if eid and not _remember(eid):
                log.info("dedup: skipping replayed webhookEventId=%s", eid)
                continue

            # Wrap event in the original envelope so on_message can access
            # `destination` (some adapters log it).
            single_payload = {"destination": destination, "events": [ev]}

            # Fire-and-forget: LINE wants 200 within ~1s. The runner's
            # on_event implements its own error logging.
            asyncio.create_task(_safe_dispatch(on_event, single_payload))
        return JSONResponse({"ok": True})

    @app.get("/")
    async def index() -> PlainTextResponse:
        return PlainTextResponse("GLC LINE webhook receiver. POST events to " + webhook_path)
    return app

async def _safe_dispatch(
    on_event: Callable[[dict[str, Any]], Awaitable[None]],
    payload: dict[str, Any],
) -> None:
    try:
        await on_event(payload)
    except Exception as e:  # pragma: no cover — pure safety net
        log.exception("on_event raised: %r", e)
