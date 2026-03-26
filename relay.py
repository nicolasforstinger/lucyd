"""Relay channel — forwards outbound calls to a bridge via HTTP.

The daemon uses RelayChannel as its channel when running in bridge mode.
Inbound messages arrive via the HTTP API. Outbound delivery (replies,
typing, reactions, streaming) is forwarded to the bridge's HTTP server.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class RelayChannel:
    """HTTP proxy — forwards outbound to a bridge process."""

    def __init__(self, callback_url: str):
        self.url = callback_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None

    async def connect(self) -> None:
        self._client = httpx.AsyncClient(timeout=30)
        log.info("Relay channel connected: %s", self.url)

    async def disconnect(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _post(self, path: str, body: dict) -> None:
        if self._client is None:
            log.warning("Relay not connected, dropping %s", path)
            return
        try:
            resp = await self._client.post(f"{self.url}{path}", json=body)
            resp.raise_for_status()
        except Exception as e:
            log.warning("Relay delivery failed (%s): %s", path, e)

    async def send(self, target: str, text: str, attachments: list[str] | None = None) -> None:
        await self._post("/send", {"target": target, "text": text, "attachments": attachments})

    async def send_typing(self, target: str) -> None:
        await self._post("/typing", {"target": target})

    async def send_stream_chunk(self, target: str, text: str, done: bool = False) -> None:
        await self._post("/stream", {"target": target, "text": text, "done": done})


def create_channel(config: Any) -> RelayChannel | None:
    """Create channel from config. Returns None if no channel configured."""
    ch_type = config.channel_type
    if not ch_type:
        return None
    if ch_type == "relay":
        url = config.raw("channel", "relay", "callback_url", default="")
        if not url:
            raise ValueError("[channel.relay] callback_url is required")
        return RelayChannel(url)
    raise ValueError(f"Unknown channel type: {ch_type!r}")
