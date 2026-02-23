"""Anthropic-compatible provider.

Works with any model accessible through the Anthropic Messages API.
Supports prompt caching (cache_control) and extended thinking (adaptive/budgeted).
Conditional import — fails with clear message if anthropic SDK not installed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from . import LLMResponse, ToolCall, Usage

log = logging.getLogger(__name__)

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore[assignment]


def _safe_parse_args(raw: Any) -> dict:
    """Parse tool input, handling both dict and string forms.

    Anthropic usually returns a dict, but may return a string in edge cases.
    Matches the OpenAI provider's fallback pattern ({"raw": ...}).
    """
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"raw": raw}


class AnthropicCompatProvider:
    def __init__(
        self,
        api_key: str,
        model: str,
        max_tokens: int = 4096,
        base_url: str = "",
        cache_control: bool = False,
        thinking_enabled: bool = False,
        thinking_budget: int = 10000,
        thinking_effort: str = "",
        thinking_mode: str = "",
    ):
        if anthropic is None:
            raise RuntimeError(
                "Anthropic provider requires: pip install anthropic"
            )
        if base_url:
            self.client = anthropic.Anthropic(api_key=api_key, base_url=base_url)
        else:
            self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.max_tokens = max_tokens
        self.cache_control = cache_control
        self.thinking_enabled = thinking_enabled
        self.thinking_budget = thinking_budget
        self.thinking_effort = thinking_effort  # "high", "medium", "low" or ""
        # "adaptive" | "budgeted" | "disabled" — overrides thinking_enabled if set
        self.thinking_mode = thinking_mode

    def format_tools(self, tools: list[dict]) -> list[dict]:
        formatted = []
        for t in tools:
            formatted.append({
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["input_schema"],
            })
        return formatted

    def format_system(self, blocks: list[dict]) -> list[dict]:
        """Convert cache-tier blocks to Anthropic system format.

        Each block: {"text": str, "tier": "stable"|"semi_stable"|"dynamic"}
        """
        result = []
        for block in blocks:
            entry: dict[str, Any] = {"type": "text", "text": block["text"]}
            if self.cache_control and block.get("tier") in ("stable", "semi_stable"):
                entry["cache_control"] = {"type": "ephemeral"}
            result.append(entry)
        return result

    @staticmethod
    def _convert_content_blocks(content: Any) -> Any:
        """Convert neutral image blocks to Anthropic API format.

        Text blocks pass through unchanged. Neutral image blocks
        {"type": "image", "media_type": ..., "data": ...} become
        {"type": "image", "source": {"type": "base64", ...}}.
        """
        if not isinstance(content, list):
            return content
        result = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image" and "media_type" in block:
                result.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": block["media_type"],
                        "data": block["data"],
                    },
                })
            else:
                result.append(block)
        return result

    def format_messages(self, messages: list[dict]) -> list[dict]:
        """Convert internal format to Anthropic API format.

        Preserves thinking blocks with signatures for tool-use continuity.
        Converts neutral image blocks to Anthropic's nested source format.
        """
        result = []
        for msg in messages:
            role = msg.get("role", "")

            if role == "user":
                content = msg.get("content", msg.get("text", ""))
                result.append({"role": "user", "content": self._convert_content_blocks(content)})

            elif role == "assistant":
                content_blocks = []
                # Preserve thinking blocks with signature for API continuity
                if msg.get("thinking_block"):
                    content_blocks.append(msg["thinking_block"])
                elif msg.get("thinking"):
                    content_blocks.append({
                        "type": "thinking",
                        "thinking": msg["thinking"],
                    })
                if msg.get("text"):
                    content_blocks.append({
                        "type": "text",
                        "text": msg["text"],
                    })
                for tc in msg.get("tool_calls", []):
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["name"],
                        "input": tc["arguments"],
                    })
                if content_blocks:
                    result.append({"role": "assistant", "content": content_blocks})

            elif role == "tool_results":
                tool_content = []
                for r in msg.get("results", []):
                    tool_content.append({
                        "type": "tool_result",
                        "tool_use_id": r["tool_call_id"],
                        "content": r["content"],
                    })
                if tool_content:
                    result.append({"role": "user", "content": tool_content})

        return result

    def _build_thinking_param(self) -> dict | None:
        """Build the thinking parameter based on config."""
        # Explicit thinking_mode takes precedence
        if self.thinking_mode == "disabled":
            return None
        if self.thinking_mode == "adaptive":
            param: dict[str, Any] = {"type": "adaptive"}
            if self.thinking_effort:
                param["effort"] = self.thinking_effort
            return param
        if self.thinking_mode == "budgeted":
            return {
                "type": "enabled",
                "budget_tokens": self.thinking_budget,
            }
        # Fallback: use thinking_enabled flag
        if not self.thinking_enabled:
            return None
        # Default to budgeted for non-adaptive models
        return {
            "type": "enabled",
            "budget_tokens": self.thinking_budget,
        }

    async def complete(
        self, system: Any, messages: list[dict], tools: list[dict], **kwargs
    ) -> LLMResponse:
        """Call Anthropic Messages API."""
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "system": system,
            "messages": messages,
        }
        if tools:
            params["tools"] = tools

        thinking_param = self._build_thinking_param()
        if thinking_param:
            params["thinking"] = thinking_param

        if thinking_param:
            # Streaming required when thinking is enabled — SDK enforces this
            # to avoid HTTP timeouts on long reasoning chains.
            def _stream_to_message():
                with self.client.messages.stream(**params) as stream:
                    return stream.get_final_message()

            response = await asyncio.to_thread(_stream_to_message)
        else:
            response = await asyncio.to_thread(
                self.client.messages.create, **params
            )

        # Extract from response
        text_parts = []
        tool_calls = []
        thinking_text = None
        thinking_block = None  # Full block with signature for preservation

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=_safe_parse_args(block.input),
                ))
            elif block.type == "thinking":
                thinking_text = block.thinking
                # Preserve the full block (including signature) for tool-use continuity
                thinking_block = {
                    "type": "thinking",
                    "thinking": block.thinking,
                }
                if hasattr(block, "signature") and block.signature:
                    thinking_block["signature"] = block.signature
            elif block.type == "redacted_thinking":
                # Preserve redacted blocks as-is
                if thinking_block is None:
                    thinking_block = {"type": "redacted_thinking", "data": block.data}

        usage = Usage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
        )

        stop = "end_turn"
        if response.stop_reason == "tool_use":
            stop = "tool_use"
        elif response.stop_reason == "max_tokens":
            stop = "max_tokens"

        return LLMResponse(
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            stop_reason=stop,
            usage=usage,
            thinking=thinking_text,
            raw=response,
            _thinking_block=thinking_block,
        )
