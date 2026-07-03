"""Bridge process for the live LINE adapter.
1. Reads config from environment variables (see .env.example),
2. Hosts a FastAPI webhook receiver on LINE_WEBHOOK_PORT, and
3. Maintains a WebSocket connection to the GLC gateway.
Bridges traffic in both directions:
  Inbound:  LINE platform --HTTPS POST--> webhook.py --on_event-->
            adapter.on_message() --ChannelMessage--> WS to gateway
  Outbound: gateway --ChannelReply (JSON)--> WS client -->
            adapter.send() --> live_client.dispatch() --> LINE /reply or /push
Run with:
    uv run python -m glc.channels.catalogue.line.runner
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
import sys
from typing import Any

import truststore
import uvicorn
import websockets
from dotenv import load_dotenv
from websockets.exceptions import ConnectionClosed

from glc.channels.catalogue.line._dns_bypass import make_bypass_transport
from glc.channels.catalogue.line.adapter import Adapter
from glc.channels.catalogue.line.live_client import LiveLineClient
from glc.channels.catalogue.line.schemas import LiveLineConfig
from glc.channels.catalogue.line.webhook import build_webhook_app
from glc.channels.envelope import ChannelReply

# Patch ssl to use the Windows certificate store (needed on Windows with Norton).
# Must run before any SSL connection is opened — imports themselves don't open sockets.
truststore.inject_into_ssl()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
)
log = logging.getLogger("glc.line.runner")

class LineRunner:
    """Owns the adapter, the live HTTP client, and the WebSocket to the gateway."""
    def __init__(self, cfg: LiveLineConfig) -> None:
        self.cfg = cfg
        transport = make_bypass_transport()
        self.live_client = LiveLineClient(channel_access_token=cfg.channel_access_token, transport=transport)
        self.adapter = Adapter(config={"live_client": self.live_client})
        self._ws: Any = None
        self._ws_lock = asyncio.Lock()
        self._shutdown = asyncio.Event()

    # ---------------------INBOUND-------------------------------------------
    async def on_webhook_event(self, payload: dict[str, Any]) -> None:
        """Invoked by webhook.py for each verified, deduplicated event.

        Non-message events (follow, unfollow, join, leave, postback, …) are
        handled here before they reach the adapter, so adapter.on_message is
        only ever called with a "message" event and can always return a
        ChannelMessage without special-casing None.
        """
        events = payload.get("events") or []
        event = events[0] if events else {}
        event_type = event.get("type")

        if event_type != "message":
            if event_type in ("follow", "unfollow", "join", "leave", "postback"):
                log.info(
                    "lifecycle event type=%s source=%s",
                    event_type,
                    event.get("source", {}),
                )
            elif event_type:
                log.debug("ignoring event type=%s", event_type)
            return

        try:
            envelope = await self.adapter.on_message(payload)
        except Exception as e:
            log.exception("adapter.on_message failed: %r", e)
            return

        log.info(
            "inbound user=%s trust=%s text=%r",
            envelope.channel_user_id,
            envelope.trust_level,
            envelope.text,
        )
        await self._send_to_gateway(envelope.model_dump_json())

    async def _send_to_gateway(self, json_text: str) -> None:
        async with self._ws_lock:
            if self._ws is None:
                log.warning("WS not connected; dropping inbound (gateway will not see it)")
                return
            try:
                await self._ws.send(json_text)
            except ConnectionClosed:
                log.warning("WS closed mid-send; reconnect loop will pick this up")
                self._ws = None

    # ----------------------OUTBOUND-----------------------------------------

    async def _ws_loop(self) -> None:
        """Maintain the WebSocket to the gateway, reconnecting with backoff."""
        backoff = 1.0
        headers = [("Authorization", f"Bearer {self.cfg.install_token}")]
        while not self._shutdown.is_set():
            try:
                log.info("connecting WS -> %s", self.cfg.gateway_ws_url)
                async with websockets.connect(
                    self.cfg.gateway_ws_url,
                    additional_headers=headers,
                    open_timeout=10,
                    ping_interval=30,
                    ping_timeout=10,
                ) as ws:
                    self._ws = ws
                    log.info("WS connected")
                    backoff = 1.0  # reset on success
                    await self._consume_gateway_replies(ws)
            except Exception as e:
                log.warning("WS error: %r -- reconnecting in %.1fs", e, backoff)
            finally:
                self._ws = None

            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=backoff)
                return  # shutdown requested
            except TimeoutError:
                backoff = min(backoff * 2, 30.0)

    async def _consume_gateway_replies(self, ws: Any) -> None:
        """Read ChannelReply JSON frames from the gateway and dispatch them."""
        async for raw in ws:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("non-JSON frame from gateway: %r", raw[:200])
                continue

            # Gateway may also send {"error": ...} or {"status": 429, ...}
            if "channel" not in payload or "channel_user_id" not in payload:
                log.info("gateway control frame: %s", payload)
                continue
            try:
                reply = ChannelReply.model_validate(payload)
            except Exception as e:
                log.warning("invalid ChannelReply from gateway: %r (%r)", payload, e)
                continue

            log.info("outbound user=%s text=%r", reply.channel_user_id, reply.text)

            try:
                result = await self.adapter.send(reply)
                if isinstance(result, dict) and result.get("status") == 429:
                    log.warning("LINE rate-limited the outbound: %s", result)
            except Exception as e:
                log.exception("adapter.send failed: %r", e)

    # ------------------------LIFECYCLE--------------------------------------
    async def serve(self) -> None:
        webhook_app = build_webhook_app(
            channel_secret=self.cfg.channel_secret,
            webhook_path=self.cfg.webhook_path,
            on_event=self.on_webhook_event,
        )

        uv_config = uvicorn.Config(
            webhook_app,
            host=self.cfg.webhook_host,
            port=self.cfg.webhook_port,
            log_level="info",
            access_log=False,
        )

        server = uvicorn.Server(uv_config)

        # Install signal handlers (POSIX). Windows uvicorn handles Ctrl+C itself.

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._shutdown.set)

        log.info(
            "LINE runner starting -- webhook on %s:%d%s -> gateway %s",
            self.cfg.webhook_host,
            self.cfg.webhook_port,
            self.cfg.webhook_path,
            self.cfg.gateway_ws_url,
        )

        ws_task = asyncio.create_task(self._ws_loop(), name="ws_loop")
        server_task = asyncio.create_task(server.serve(), name="webhook_server")
        shutdown_task = asyncio.create_task(self._shutdown.wait(), name="shutdown")

        done, pending = await asyncio.wait(
            {ws_task, server_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        log.info("shutting down...")
        self._shutdown.set()
        server.should_exit = True
        for t in pending:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        await self.live_client.aclose()
        log.info("bye")

def main() -> int:
    load_dotenv()  # picks up .env if present
    try:
        cfg = LiveLineConfig.from_env()
    except KeyError as e:
        log.error("missing required env var: %s", e)
        log.error("copy .env.example to .env and fill in the values")
        return 2
    except Exception as e:
        log.error("config validation failed: %r", e)
        return 2

    runner = LineRunner(cfg)
    try:
        asyncio.run(runner.serve())
    except KeyboardInterrupt:
        pass
    return 0

if __name__ == "__main__":
    sys.exit(main())
