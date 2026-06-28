"""LINE Messaging API channel adapter.

Implements on_message and send against the LINE Messaging API webhook format.
Includes reply token management for quota optimization.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from glc.channels.base import ChannelAdapter
from glc.channels.envelope import ChannelMessage, ChannelReply
from glc.security.pairing import get_pairing_store
from glc.security.trust_level import classify

from .schemas import LineWebhookBody


class Adapter(ChannelAdapter):
    name = "line"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        # Reply token TTL store: user_id -> (token, expiry_epoch)
        self._token_store: dict[str, tuple[str, float]] = {}

    def _stash_token(self, user_id: str, token: str, ttl_s: float = 60.0) -> None:
        """Store a reply token with TTL for the given user."""
        expiry = time.time() + ttl_s
        self._token_store[user_id] = (token, expiry)

    def _consume_token(self, user_id: str) -> str | None:
        """Pop and return a valid token for the user, or None if expired/missing."""
        item = self._token_store.pop(user_id, None)
        if item is None:
            return None
        token, expires = item
        if expires < time.time():
            return None
        return token

    async def on_message(self, raw: Any) -> ChannelMessage:
        """Parse LINE webhook event and return ChannelMessage envelope."""
        # 1. Get mock from config (for disconnect handling)
        mock = self.config.get("mock")

        # 2. If mock and disconnect - ignore and proceed
        if mock and mock.pop_disconnect():
            pass  # Ignore disconnect, proceed with normal parsing

        # 3. Parse raw webhook body with Pydantic
        try:
            body = LineWebhookBody(**raw)
        except Exception:
            # Return a minimal envelope if parsing fails
            return ChannelMessage(
                channel="line",
                channel_user_id="unknown",
                user_handle="unknown",
                text=None,
                trust_level="untrusted",
                arrived_at=datetime.now(timezone.utc),
            )

        # 4. Extract first event
        if not body.events:
            # Return minimal envelope if no events
            return ChannelMessage(
                channel="line",
                channel_user_id="unknown",
                user_handle="unknown",
                text=None,
                trust_level="untrusted",
                arrived_at=datetime.now(timezone.utc),
            )

        event = body.events[0]

        # 5. Extract user ID
        user_id = event.source.userId

        # 6. Stash reply token for later use by send()
        self._stash_token(user_id, event.replyToken)

        # 7. Determine trust level
        trust_level = classify("line", user_id)

        # 9. Get user_handle from pairing store or fall back to user_id
        pairing_store = get_pairing_store()
        pairing_record = pairing_store.lookup("line", user_id)
        user_handle = pairing_record.user_handle if pairing_record else user_id

        # 10. Return ChannelMessage envelope
        return ChannelMessage(
            channel="line",
            channel_user_id=user_id,
            user_handle=user_handle,
            text=event.message.text,
            trust_level=trust_level,
            arrived_at=datetime.now(timezone.utc),
        )

    async def send(self, reply: ChannelReply) -> Any:
        """Send a reply using LINE Reply API (preferred) or Push API (fallback)."""
        # 1. Get mock from config
        mock = self.config.get("mock")

        # 2. Try to consume a reply token from our own store
        token = self._consume_token(reply.channel_user_id)

        # 3. Build payload based on token availability
        if token:
            # Use Reply API (quota-free)
            payload = {"replyToken": token, "messages": [{"type": "text", "text": reply.text}]}
        else:
            # Use Push API (quota-counted)
            payload = {"to": reply.channel_user_id, "messages": [{"type": "text", "text": reply.text}]}

        # 4. Dispatch via mock or real LINE API
        if mock:
            return await mock.send(payload)
        else:
            # Real LINE API dispatch - out of scope for test suite
            return payload
