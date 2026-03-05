"""CLI channel — stdin/stdout for testing.

The simplest possible channel. No Telegram setup needed.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

from . import InboundMessage


class CLIChannel:
    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def receive(self) -> AsyncIterator[InboundMessage]:
        while True:
            try:
                text = await asyncio.to_thread(input, "You> ")
            except (EOFError, KeyboardInterrupt):
                return
            if not text.strip():
                continue
            yield InboundMessage(
                text=text,
                sender="cli",
                timestamp=time.time(),
                source="cli",
            )

    async def send(self, target: str, text: str, attachments: list[str] | None = None) -> None:
        if text:
            print(f"Agent> {text}", flush=True)
        if attachments:
            for a in attachments:
                print(f"Agent> [attachment: {a}]", flush=True)

    async def send_typing(self, target: str) -> None:
        pass

    async def send_reaction(self, target: str, emoji: str, ts: int) -> None:
        pass
