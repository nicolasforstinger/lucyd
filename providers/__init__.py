"""LLM Provider interface and shared types.

Defines the contract between the agentic loop and any LLM backend.
Provider-specific features (caching, thinking) are handled inside
implementations, not in the interface.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from messages import AssistantMessage, Message

if TYPE_CHECKING:
    from conversion import CurrencyConverter
    from metering import MeteringDB


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def context_tokens(self) -> int:
        """Total context tokens the model processed.

        Providers report input_tokens differently:
        - Anthropic: input_tokens = uncached only, cache_read = cached portion
        - OpenAI: input_tokens = full context, cache_read = 0

        This property normalizes to always return the true context size.
        """
        return self.input_tokens + self.cache_read_tokens


@dataclass
class ModelCapabilities:
    """Structured capability declaration for a model provider.

    Populated from provider config (TOML) at construction time.
    The framework checks these before using features — never ad-hoc config keys.
    """
    supports_tools: bool = True
    supports_vision: bool = False
    supports_streaming: bool = False
    supports_system_prompt: bool = True
    max_context_tokens: int = 0
    supports_thinking: bool = False


@dataclass
class CostContext:
    """Cost tracking parameters that travel together through the call chain."""
    metering: MeteringDB | None
    session_id: str
    model_name: str
    cost_rates: list[float]
    provider_name: str = ""
    currency: str = "EUR"
    converter: CurrencyConverter | None = None

    @classmethod
    def none(cls) -> "CostContext":
        """Null-object: no metering, empty fields. Avoids 'if cost else' patterns."""
        return cls(metering=None, session_id="", model_name="", cost_rates=[])


@dataclass
class LLMResponse:
    text: str | None
    tool_calls: list[ToolCall]
    stop_reason: str  # "end_turn" | "tool_use" | "max_tokens"
    usage: Usage
    thinking: str | None = None
    raw: Any = None  # Provider-specific raw response (Anthropic/OpenAI/Mistral); no common type
    # Full thinking block with signature for Anthropic tool-use continuity
    _thinking_block: dict[str, Any] | None = field(default=None, repr=False)
    # API call latency (set by _call_provider_with_retry for metering)
    _api_latency_ms: int | None = field(default=None, repr=False)
    # Set by agentic loop when per-message cost limit is exceeded
    cost_limited: bool = False
    # File paths produced by tools during the agentic loop
    attachments: list[str] = field(default_factory=list)
    # Agentic loop turn count (set by run_agentic_loop)
    turns: int = 0
    # Accumulated cost across all turns (set by agentic loop / single-shot)
    total_cost: float = 0.0

    def to_internal_message(self) -> AssistantMessage:
        """Convert to internal message format for session storage."""
        msg: AssistantMessage = {"role": "agent"}
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
            "context_tokens": self.usage.context_tokens,
        }
        return msg


@dataclass
class StreamDelta:
    """A single chunk from a streaming response.

    text/tool deltas are incremental (append to previous).
    stop_reason and usage are set only on the final chunk.
    status is for intermediate user-facing messages ("Running tools...").
    """
    text: str = ""
    tool_call_index: int = -1  # which tool call is being built (-1 = none)
    tool_call_id: str = ""
    tool_name: str = ""        # set on first chunk of a tool call
    tool_args_delta: str = ""  # incremental JSON fragment
    thinking: str = ""
    stop_reason: str = ""      # set on final chunk
    usage: Usage | None = None  # set on final chunk
    status: str = ""           # intermediate status ("Running tools: read, search...")


SystemPrompt = list[dict[str, Any]] | str
"""Provider-formatted system prompt. Anthropic uses a list of content blocks,
OpenAI and SmokeLocal use a single string."""


class LLMProvider(Protocol):
    """Protocol for LLM provider implementations."""

    @property
    def capabilities(self) -> ModelCapabilities:
        """Structured capability declaration for this provider/model."""
        ...

    def format_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert generic tool schemas to provider-specific format."""
        ...

    def format_system(self, blocks: list[dict[str, str]]) -> SystemPrompt:
        """Convert system prompt blocks to provider format."""
        ...

    def format_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Convert internal message format to provider's API format."""
        ...

    async def complete(
        self, system: SystemPrompt, messages: list[dict[str, Any]], tools: list[dict[str, Any]], **kwargs: Any,
    ) -> LLMResponse:
        """Send to LLM, return normalized response."""
        ...

    def stream(
        self, system: SystemPrompt, messages: list[dict[str, Any]], tools: list[dict[str, Any]], **kwargs: Any,
    ) -> AsyncIterator[StreamDelta]:
        """Stream response deltas from LLM.

        Yields StreamDelta chunks as they arrive. The final chunk has
        stop_reason set and usage populated.

        If the provider does not support streaming, yields a single
        StreamDelta constructed from complete().
        """
        ...


async def stream_fallback(
    provider: LLMProvider, system: SystemPrompt, messages: list[dict[str, Any]],
    tools: list[dict[str, Any]], **kwargs: Any,
) -> AsyncIterator[StreamDelta]:
    """Non-streaming fallback: call complete() and yield one StreamDelta."""
    response = await provider.complete(system, messages, tools, **kwargs)
    yield StreamDelta(
        text=response.text or "",
        thinking=response.thinking or "",
        stop_reason=response.stop_reason,
        usage=response.usage,
    )


def _parse_json(raw: str) -> dict[str, Any] | None:
    """Parse JSON tool arguments. Returns dict on success, None on failure.

    No repair heuristics — invalid JSON returns None and the tool executor
    returns an error to the model via tool_call_retry.
    """
    try:
        result = json.loads(raw)  # Any: JSON deserialization is inherently untyped
        return result if isinstance(result, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _build_capabilities(model_config: dict[str, Any]) -> ModelCapabilities:
    """Extract ModelCapabilities from a model config dict."""
    thinking = model_config.get("thinking_enabled", False)
    if not thinking and model_config.get("thinking_mode", "") in ("adaptive", "budgeted"):
        thinking = True
    if not thinking and model_config.get("thinking_budget", 0) > 0:
        thinking = True
    return ModelCapabilities(
        supports_tools=model_config.get("supports_tools", True),
        supports_vision=model_config.get("supports_vision", False),
        supports_streaming=model_config.get("supports_streaming", False),
        supports_system_prompt=model_config.get("supports_system_prompt", True),
        max_context_tokens=model_config.get("max_context_tokens", 0),
        supports_thinking=thinking,
    )


def create_provider(model_config: dict[str, Any], api_key: str = "") -> LLMProvider:
    """Factory: create provider from model config section."""
    provider_type = model_config.get("provider", "")
    caps = _build_capabilities(model_config)

    if provider_type == "smoke-local":
        from .smoke_local import SmokeLocalProvider
        smoke_caps = ModelCapabilities(
            supports_tools=model_config.get("supports_tools", False),
            supports_vision=model_config.get("supports_vision", False),
            supports_streaming=model_config.get("supports_streaming", False),
            supports_system_prompt=model_config.get("supports_system_prompt", True),
            max_context_tokens=model_config.get("max_context_tokens", 8192),
            supports_thinking=False,
        )
        return SmokeLocalProvider(
            model=model_config["model"],
            reply_text=model_config.get("reply_text", "SMOKE_TEST_OK"),
            max_tokens=model_config.get("max_tokens", 64),
            capabilities=smoke_caps,
            provider_name=provider_type,
        )
    elif provider_type == "anthropic":
        from .anthropic import AnthropicProvider
        return AnthropicProvider(
            api_key=api_key,
            model=model_config["model"],
            max_tokens=model_config.get("max_tokens", 4096),
            base_url=model_config.get("base_url", ""),
            cache_control=model_config.get("cache_control", False),
            thinking_enabled=model_config.get("thinking_enabled", False),
            thinking_budget=model_config.get("thinking_budget", 10000),
            thinking_effort=model_config.get("thinking_effort", ""),
            thinking_mode=model_config.get("thinking_mode", ""),
            capabilities=caps,
            provider_name=provider_type,
        )
    elif provider_type == "openai":
        from .openai import OpenAIProvider
        return OpenAIProvider(
            api_key=api_key,
            model=model_config["model"],
            max_tokens=model_config.get("max_tokens", 4096),
            base_url=model_config.get("base_url", ""),
            thinking_budget=model_config.get("thinking_budget", 0),
            slot_id=model_config.get("slot_id", -1),
            capabilities=caps,
            provider_name=provider_type,
        )
    elif provider_type == "mistral":
        try:
            from .mistral import MistralProvider
        except ImportError:
            raise ImportError(
                "Mistral provider requires the mistralai package: pip install lucyd[mistral]"
            ) from None
        mp: LLMProvider = MistralProvider(
            api_key=api_key,
            model=model_config["model"],
            max_tokens=model_config.get("max_tokens", 4096),
            base_url=model_config.get("base_url", ""),
            capabilities=caps,
            provider_name=provider_type,
        )
        return mp
    else:
        raise ValueError(f"Unknown provider type: {provider_type!r}")
