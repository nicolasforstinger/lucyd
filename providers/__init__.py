"""LLM Provider interface and shared types.

Defines the contract between the agentic loop and any LLM backend.
Provider-specific features (caching, thinking) are handled inside
implementations, not in the interface.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class LLMResponse:
    text: str | None
    tool_calls: list[ToolCall]
    stop_reason: str  # "end_turn" | "tool_use" | "max_tokens"
    usage: Usage
    thinking: str | None = None
    raw: Any = None
    # Full thinking block with signature for Anthropic tool-use continuity
    _thinking_block: dict | None = field(default=None, repr=False)

    def to_internal_message(self) -> dict:
        """Convert to internal message format for session storage."""
        msg: dict[str, Any] = {"role": "assistant"}
        if self.text:
            msg["text"] = self.text
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in self.tool_calls
            ]
        if self.thinking:
            msg["thinking"] = self.thinking
        if self._thinking_block:
            msg["thinking_block"] = self._thinking_block
        msg["usage"] = {
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "cache_read_tokens": self.usage.cache_read_tokens,
            "cache_write_tokens": self.usage.cache_write_tokens,
        }
        return msg


class LLMProvider(Protocol):
    """Protocol for LLM provider implementations."""

    def format_tools(self, tools: list[dict]) -> list[dict]:
        """Convert generic tool schemas to provider-specific format."""
        ...

    def format_system(self, blocks: list[dict]) -> Any:
        """Convert system prompt blocks to provider format."""
        ...

    def format_messages(self, messages: list[dict]) -> list[dict]:
        """Convert internal message format to provider's API format."""
        ...

    async def complete(
        self, system: Any, messages: list[dict], tools: list[dict], **kwargs
    ) -> LLMResponse:
        """Send to LLM, return normalized response."""
        ...


def create_provider(model_config: dict, api_key: str = "") -> LLMProvider:
    """Factory: create provider from model config section."""
    provider_type = model_config.get("provider", "")

    if provider_type == "anthropic-compat":
        from .anthropic_compat import AnthropicCompatProvider
        return AnthropicCompatProvider(
            api_key=api_key,
            model=model_config["model"],
            max_tokens=model_config.get("max_tokens", 4096),
            base_url=model_config.get("base_url", ""),
            cache_control=model_config.get("cache_control", False),
            thinking_enabled=model_config.get("thinking_enabled", False),
            thinking_budget=model_config.get("thinking_budget", 10000),
            thinking_effort=model_config.get("thinking_effort", ""),
            thinking_mode=model_config.get("thinking_mode", ""),
        )
    if provider_type == "openai-compat":
        from .openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(
            api_key=api_key,
            model=model_config["model"],
            max_tokens=model_config.get("max_tokens", 4096),
            base_url=model_config.get("base_url", "https://api.openai.com/v1"),
        )
    raise ValueError(f"Unknown provider type: {provider_type!r}")
