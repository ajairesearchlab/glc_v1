"""LINE Messaging API channel adapter.

Wire-format references:
- Webhook events: https://developers.line.biz/en/reference/messaging-api/#message-event
- Reply message: https://developers.line.biz/en/reference/messaging-api/#send-reply-message
- Push message: https://developers.line.biz/en/reference/messaging-api/#send-push-message

Behavioural contract (test_channel_specific_behaviour_reply_token_then_push):
- Inbound webhooks carry a one-shot `replyToken` valid ~60s.
- First outbound after an inbound MUST use the /reply (free) endpoint
  (cheap, quota-free): `{"replyToken": ..., "messages": [...]}`.
- Subsequent outbounds with no in-flight (no fresh inbound) token MUST fall back to /push (metered):
  `{"to": userId, "messages": [...]}`.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from glc.channels.base import ChannelAdapter
from glc.channels.envelope import ChannelMessage, ChannelReply
from glc.security.trust_level import classify

# LINE reply tokens expire ~30-60s after delivery, mirroring the mock's 60s TTL
REPLY_TOKEN_TTL_SECONDS = 60.0


class Adapter(ChannelAdapter):
    name = "line"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config=config)
        #keeping mock and live calls separate
        #call to mock
        self._mock = self.config.get("mock")
        #call to live client
        self._live = self.config.get("live_client")
        # Per-user reply-token TTL store: {channel_user_id: (token, expires_at)}
        self._reply_tokens: dict[str, tuple[str, float]] = {}

    # -----------------------INBOUND-------------------------------------------

    async def on_message(self, raw: Any) -> ChannelMessage:
        # Drain any pending disconnect flag so transient network blips do not
        # crash the dispatcher (test_disconnect_is_handled).
        if self._mock is not None and hasattr(self._mock, "pop_disconnect"):
            self._mock.pop_disconnect()

        # Runner (and tests) guarantee only "message" events reach here — no
        # need to filter by type or return None.
        events = (raw or {}).get("events") or []
        event = events[0] if events else {}

        source = event.get("source", {}) or {}
        user_id = (
            source.get("userId")
            or source.get("groupId")
            or source.get("roomId")
            or ""
        )

        msg_obj = event.get("message", {}) or {}
        msg_type = msg_obj.get("type")
        text = msg_obj.get("text") if msg_type == "text" else None

        # Stash the reply token so the next outbound can use the cheap
        # reply endpoint instead of consuming push quota.
        reply_token = event.get("replyToken")
        if reply_token and user_id:
            self._reply_tokens[user_id] = (
                reply_token,
                time.time() + REPLY_TOKEN_TTL_SECONDS,
            )

        trust = classify("line", user_id)

        return ChannelMessage(
            channel="line",
            channel_user_id=user_id,
            user_handle=user_id,  # LINE webhook payloads carry no display name
            text=text,
            attachments=[],
            voice_audio_ref=None,
            thread_id=source.get("groupId") or source.get("roomId"),
            trust_level=trust,
            arrived_at=datetime.now(UTC),
            metadata={
                "line_message_id": msg_obj.get("id"),
                "line_message_type": msg_type,
                "destination": (raw or {}).get("destination"),
                "timestamp_ms": event.get("timestamp"),
                "source_type": source.get("type"),
            },
        )

    # -----------------------OUTBOUND-------------------------------------------

    async def send(self, reply: ChannelReply) -> Any:
        user_id = reply.channel_user_id
        messages = [{"type": "text", "text": reply.text or ""}]

        token = self._consume_reply_token(user_id)
        if token is not None:
            body: dict[str, Any] = {"replyToken": token, "messages": messages}
        else:
            body = {"to": user_id, "messages": messages}

        # Mock path (tests)
        if self._mock is not None:
            # The mock returns the raw upstream response shape, including
            # {"status": 429, ...} on rate-limit — we propagate it unchanged.
            return await self._mock.send(body)

        # Live path (runner)
        if self._live is not None:
            result = await self._live.dispatch(body)
            # If the reply token was rejected (expired/used between stash and send),
            # transparently fall back to push so the user still gets a reply.
            if (
                isinstance(result, dict)
                and result.get("invalid_reply_token")
                and "replyToken" in body
            ):
                push_body = {"to": user_id, "messages": messages}
                return await self._live.dispatch(push_body)
            return result

        raise RuntimeError(
            "LINE adapter requires either a mock (in tests) or a live "
            "httpx client (production)."
        )

    # -----------------------HELPERS-------------------------------------------

    def _consume_reply_token(self, user_id: str) -> str | None:
        """Pop and return a non-expired reply token for `user_id`, else None."""
        item = self._reply_tokens.pop(user_id, None)
        if item is None:
            return None
        token, expires_at = item
        if expires_at < time.time():
            return None
        return token
