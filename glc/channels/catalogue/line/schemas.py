"""Channel-specific Pydantic types for the LINE adapter.
The canonical envelope (ChannelMessage, ChannelReply, Attachment) lives in
glc.channels.envelope and is shared across all 15 channel adapters.
This module is reserved for LINE-specific types — currently just the live-runner
config model.
"""
from __future__ import annotations

import os

from pydantic import BaseModel, Field, field_validator


class LiveLineConfig(BaseModel):
    """Configuration for the live LINE runner.
    All values come from environment variables (see .env.example). Loading
    via this model gives us validation + helpful error messages at startup
    instead of obscure runtime KeyErrors deep inside the request loop.
    """
    # LINE side
    channel_secret: str = Field(min_length=32, description="LINE channel secret (HMAC key)")
    channel_access_token: str = Field(min_length=20, description="LINE bearer token")

    # GLC side
    install_token: str = Field(min_length=8, description="GLC per-installation token")
    gateway_ws_url: str = "ws://localhost:8111/v1/channels/line"

    # Runner side
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8120
    webhook_path: str = "/webhook/line"

    # Documentation only
    bot_basic_id: str = ""

    @field_validator("webhook_path")
    @classmethod
    def _path_starts_with_slash(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("webhook_path must start with '/'")
        return v

    @classmethod
    def from_env(cls) -> LiveLineConfig:
        return cls(
            channel_secret=os.environ["LINE_CHANNEL_SECRET"],
            channel_access_token=os.environ["LINE_CHANNEL_ACCESS_TOKEN"],
            install_token=os.environ["GLC_INSTALL_TOKEN"],
            gateway_ws_url=os.environ.get(
                "GLC_GATEWAY_WS_URL", "ws://localhost:8111/v1/channels/line"
            ),

            webhook_host=os.environ.get("LINE_WEBHOOK_HOST", "0.0.0.0"),
            webhook_port=int(os.environ.get("LINE_WEBHOOK_PORT", "8120")),
            webhook_path=os.environ.get("LINE_WEBHOOK_PATH", "/webhook/line"),
            bot_basic_id=os.environ.get("LINE_BOT_BASIC_ID", ""),
        )
