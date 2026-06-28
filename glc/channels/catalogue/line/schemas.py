"""Pydantic schemas for LINE Messaging API webhook parsing.

These models parse the LINE webhook body structure according to:
https://developers.line.biz/en/reference/messaging-api/#message-event
"""

from __future__ import annotations

from pydantic import BaseModel


class LineWebhookSource(BaseModel):
    """Source information from LINE webhook event."""

    userId: str


class LineWebhookMessage(BaseModel):
    """Message information from LINE webhook event."""

    id: str
    type: str
    text: str | None = None


class LineWebhookEvent(BaseModel):
    """Single event from LINE webhook POST body."""

    type: str
    source: LineWebhookSource
    message: LineWebhookMessage
    replyToken: str
    timestamp: int


class LineWebhookBody(BaseModel):
    """Complete LINE webhook POST body."""

    destination: str
    events: list[LineWebhookEvent]
