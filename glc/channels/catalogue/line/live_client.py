"""HTTP client for the live LINE Messaging API.

Two endpoints, one method:
- POST https://api.line.me/v2/bot/message/reply   (cheap, replyToken required)
- POST https://api.line.me/v2/bot/message/push    (counts against monthly quota)

The dispatcher mirrors the mock surface: it returns a dict in the same shape
the LineMock returns, so glc/channels/catalogue/line/adapter.py treats the
mock and live paths identically.

Error semantics
---------------
- 200       → returns {"sentMessages": [...]} or whatever LINE sent back
- 429       → returns {"status": 429, "message": "...", "retry_after": int|None}
- 400 with  → returns {"status": 400, "invalid_reply_token": True, ...}
            "Invalid reply token"
- 401/403   → returns {"status": 4xx, "auth_error": True, ...}
- 5xx       → retries with exponential backoff (max 3 attempts), then surfaces
            the final response unchanged.

Reference: https://developers.line.biz/en/reference/messaging-api/
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

LINE_API_BASE = "https://api.line.me"
REPLY_URL = f"{LINE_API_BASE}/v2/bot/message/reply"
PUSH_URL = f"{LINE_API_BASE}/v2/bot/message/push"

_RETRY_BACKOFF_S = [0.5, 1.0, 2.0]  # 3 attempts on 5xx

log = logging.getLogger("glc.line.live_client")

class LiveLineClient:
    """Async HTTP client for the LINE Messaging API.
    Instantiated once at runner startup and shared across all sends so the
    underlying httpx connection pool is reused.
    """
    def __init__(
        self,
        channel_access_token: str,
        http: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 10.0,
    ) -> None:

        if not channel_access_token:
            raise ValueError("channel_access_token is required")
        self._token = channel_access_token
        self._http = http or httpx.AsyncClient(transport=transport, timeout=timeout_s)
        self._owns_http = http is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def dispatch(self, body: dict[str, Any]) -> dict[str, Any]:
        """Send `body` to /reply if it has replyToken, else /push.
        Returns a dict in the same shape the mock surface returns, so the
        Adapter.send() code path is identical for mock and live.
        """
        if "replyToken" in body:
            return await self._post(REPLY_URL, body, kind="reply")
        if "to" in body:
            return await self._post(PUSH_URL, body, kind="push")
        return {
            "status": 400,
            "message": "body must contain either replyToken or to",
        }

    # ----------------------------POST--------------------------------------
    async def _post(self, url: str, body: dict[str, Any], *, kind: str) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

        last_resp: httpx.Response | None = None
        for attempt, backoff in enumerate([0.0, *_RETRY_BACKOFF_S]):
            if backoff:
                await asyncio.sleep(backoff)
            try:
                resp = await self._http.post(url, json=body, headers=headers)
            except httpx.HTTPError as e:
                log.warning("LINE %s call failed (attempt %d): %r", kind, attempt + 1, e)
                if attempt >= len(_RETRY_BACKOFF_S):
                    return {"status": 599, "message": f"transport error: {e}"}
                continue
            last_resp = resp
            # Success
            if 200 <= resp.status_code < 300:
                try:
                    data = resp.json()
                    return data if data else {"sentMessages": []}
                except Exception:
                    return {"sentMessages": [], "raw": resp.text}

            # Rate limit — surface immediately, do NOT retry
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                return {
                    "status": 429,
                    "message": resp.text or "Too Many Requests",
                    "retry_after": int(retry_after) if retry_after and retry_after.isdigit() else None,
                }

            # Invalid reply token — caller (adapter) decides whether to fall back to push
            if resp.status_code == 400 and "Invalid reply token" in resp.text:
                return {
                    "status": 400,
                    "invalid_reply_token": True,
                    "message": resp.text,
                }

            # Auth error — never retry, fix the token instead
            if resp.status_code in (401, 403):
                return {
                    "status": resp.status_code,
                    "auth_error": True,
                    "message": resp.text,
                }

            # 5xx — retry
            if 500 <= resp.status_code < 600 and attempt < len(_RETRY_BACKOFF_S):
                log.warning(
                    "LINE %s returned %d (attempt %d), backing off %ss",
                    kind, resp.status_code, attempt + 1, _RETRY_BACKOFF_S[attempt],
                )
                continue

            # 4xx non-retryable, or 5xx after retries
            return {
                "status": resp.status_code,
                "message": resp.text or f"HTTP {resp.status_code}",
            }

        # Fell out of the loop without returning — return last response shape
        if last_resp is not None:
            return {"status": last_resp.status_code, "message": last_resp.text}
        return {"status": 599, "message": "no response"}
