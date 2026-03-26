"""Deterministic local provider for audit smoke tests.

Uses no external SDKs and no network. Intended for audit/integration plumbing
checks where the daemon must complete a full message cycle autonomously.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from context import _estimate_tokens

from . import LLMResponse, ModelCapabilities, StreamDelta, Usage, stream_fallback


class SmokeLocalProvider:
    def __init__(
        self,
        model: str,
        reply_text: str = "SMOKE_TEST_OK",
        max_tokens: int = 64,
        capabilities: ModelCapabilities | None = None,
    ):
        self.model = model
        self.reply_text = reply_text
        self.max_tokens = max_tokens
        self._capabilities = capabilities or ModelCapabilities(
            supports_tools=False,
            supports_streaming=False,
            max_context_tokens=8192,
        )

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    def format_tools(self, tools: list[dict]) -> list[dict]:
        return []

    def format_system(self, blocks: list[dict]) -> str:
        return "\n\n".join(b.get("text", "") for b in blocks)

    def format_messages(self, messages: list[dict]) -> list[dict]:
        return messages

    async def complete(
        self, system: Any, messages: list[dict], tools: list[dict], **kwargs,
    ) -> LLMResponse:
        usage = Usage(
            input_tokens=max(
                1,
                _estimate_tokens(system or "")
                + sum(
                    _estimate_tokens(msg.get("content", msg.get("text", "")))
                    for msg in messages
                ),
            ),
            output_tokens=max(1, _estimate_tokens(self.reply_text)),
        )
        return LLMResponse(
            text=self.reply_text,
            tool_calls=[],
            stop_reason="end_turn",
            usage=usage,
            raw={"provider": "smoke-local", "model": self.model},
        )

    async def stream(
        self, system: Any, messages: list[dict], tools: list[dict], **kwargs,
    ) -> AsyncIterator[StreamDelta]:
        async for delta in stream_fallback(self, system, messages, tools, **kwargs):
            yield delta
