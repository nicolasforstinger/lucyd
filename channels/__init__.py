"""Channel interface and shared types.

Defines the contract between the daemon and messaging transports.
Each channel implements receive/send for its transport.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from config import Config


@dataclass
class Attachment:
    content_type: str    # "image/jpeg", "audio/ogg", etc.
    local_path: str      # Absolute path on disk
    filename: str        # Original filename or ""
    size: int            # Bytes
    is_voice: bool = False  # True = recorded voice message; False = audio file


@dataclass
class InboundMessage:
    text: str
    sender: str           # Phone number, username, "cli", etc.
    timestamp: float
    source: str           # "telegram", "cli", etc.
    group_id: str | None = None
    group_name: str | None = None
    quote: str | None = None
    attachments: list[Attachment] | None = None


class Channel(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    def receive(self) -> AsyncIterator[InboundMessage]: ...
    async def send(self, target: str, text: str, attachments: list[str] | None = None) -> None: ...
    async def send_typing(self, target: str) -> None: ...
    async def send_reaction(self, target: str, emoji: str, ts: int) -> None: ...


def create_channel(config: Config) -> Channel:
    """Factory: create channel from config."""
    ch_type = config.channel_type

    if ch_type == "cli":
        from .cli import CLIChannel
        return CLIChannel()
    if ch_type == "telegram":
        from .telegram import TelegramChannel
        tg = config.raw("channel", "telegram", default={})
        return TelegramChannel(tg)
    raise ValueError(f"Unknown channel type: {ch_type!r}")
