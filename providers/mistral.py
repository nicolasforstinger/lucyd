"""Mistral provider.

Works with models accessible through the Mistral chat completions API.
Requires the mistralai package (pip install lucyd[mistral]).

Uses the sync SDK client with run_blocking/threaded_stream for consistency
with the Anthropic and OpenAI providers' async patterns.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from async_utils import run_blocking, threaded_stream
from messages import Message

from . import (
    LLMResponse,
    ModelCapabilities,
    StreamDelta,
    SystemPrompt,
    ToolCall,
    Usage,
    _repair_json,
    stream_fallback,
)

log = logging.getLogger(__name__)

from mistralai import Mistral


class MistralProvider:
    """Provider for Mistral API models.

    Uses the mistralai SDK for all API calls.  No httpx fallback —
    the SDK is a required dependency for this provider.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        max_tokens: int = 4096,
        base_url: str = "",
        capabilities: ModelCapabilities | None = None,
        provider_name: str = "",
    ):
        self.provider_name = provider_name
        self.api_key = api_key or "not-needed"
        self.model = model
        self.max_tokens = max_tokens
        self._capabilities = capabilities or ModelCapabilities()
        kwargs: dict[str, Any] = {"api_key": self.api_key}
        if base_url:
            kwargs["server_url"] = base_url
        self.client: Any = Mistral(**kwargs)  # mistralai.Mistral — Any due to missing stubs

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    # ── Format helpers ──────────────────────────────────────────────

    def format_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert generic tool schemas to Mistral function-calling format.

        Mistral uses the OpenAI tool format: {"type": "function", "function": {...}}.
        """
        formatted = []
        for t in tools:
            formatted.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"],
                },
            })
        return formatted

    def format_system(self, blocks: list[dict[str, str]]) -> SystemPrompt:
        """Concatenate system blocks into a single string.

        Mistral does not support per-block cache control.
        """
        return "\n\n".join(b["text"] for b in blocks)

    @staticmethod
    def _convert_content_blocks(content: Any) -> Any:
        """Convert neutral image blocks to Mistral API format.

        Text blocks pass through unchanged.  Neutral image blocks
        {"type": "image", "media_type": ..., "data": ...} become
        {"type": "image_url", "image_url": {"url": "data:mime;base64,..."}}.
        Same data-URI format used by the OpenAI vision API.
        """
        if not isinstance(content, list):
            return content
        result = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image" and "media_type" in block:
                result.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{block['media_type']};base64,{block['data']}",
                    },
                })
            else:
                result.append(block)
        return result

    def format_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Convert internal message format to Mistral API format.

        Converts neutral image blocks to Mistral's data-URI format.
        """
        result: list[dict[str, Any]] = []
        for msg in messages:
            if msg["role"] == "user":
                content: Any = msg["content"]
                image_blocks: list[dict[str, Any]] | None = msg.get("_image_blocks")  # type: ignore[assignment]  # transient key, stripped before persistence
                if image_blocks:
                    content = image_blocks + [{"type": "text", "text": content if isinstance(content, str) else ""}]
                result.append({
                    "role": "user",
                    "content": self._convert_content_blocks(content),
                })

            elif msg["role"] == "assistant":
                entry: dict[str, Any] = {"role": "assistant"}
                text = msg.get("text", "")
                tool_calls_raw = msg.get("tool_calls", [])
                if text:
                    entry["content"] = text
                if tool_calls_raw:
                    entry["tool_calls"] = [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc["arguments"])
                                    if isinstance(tc["arguments"], dict)
                                    else tc["arguments"],
                            },
                        }
                        for tc in tool_calls_raw
                    ]
                if not text and not tool_calls_raw:
                    entry["content"] = ""
                result.append(entry)

            elif msg["role"] == "tool_results":
                for r in msg["results"]:
                    result.append({
                        "role": "tool",
                        "tool_call_id": r["tool_call_id"],
                        "content": r["content"] if isinstance(r["content"], str)
                                   else json.dumps(r["content"]),
                    })

        return result

    # ── Response parsing ────────────────────────────────────────────

    @staticmethod
    def _parse_tool_args(raw: Any) -> dict[str, Any]:
        """Parse tool call arguments from Mistral response.

        Mistral returns arguments as ``Union[Dict, str]``.  Handle both
        forms with JSON repair for malformed strings from small models.
        """
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            repaired = _repair_json(raw)
            return repaired if repaired is not None else {"raw": raw}
        return {"raw": str(raw)} if raw is not None else {}

    @staticmethod
    def _parse_usage(usage: Any) -> Usage:
        """Parse Mistral ``UsageInfo`` to internal ``Usage`` type."""
        if usage is None:
            return Usage()
        return Usage(
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        )

    @staticmethod
    def _map_stop_reason(finish_reason: Any) -> str:
        """Map Mistral finish_reason to internal stop reason string."""
        fr = str(finish_reason) if finish_reason else "stop"
        if fr == "tool_calls":
            return "tool_use"
        if fr in ("length", "model_length"):
            return "max_tokens"
        return "end_turn"

    # ── Completion ──────────────────────────────────────────────────

    async def complete(
        self, system: SystemPrompt, messages: list[dict[str, Any]], tools: list[dict[str, Any]], **kwargs: Any,
    ) -> LLMResponse:
        """Call Mistral chat completions API."""
        api_messages: list[dict[str, Any]] = []
        if system:
            api_messages.append({"role": "system", "content": system})
        api_messages.extend(messages)

        params: dict[str, Any] = {
            "model": self.model,
            "messages": api_messages,
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
        }
        if tools:
            params["tools"] = tools

        response: Any = await run_blocking(self.client.chat.complete, **params)

        if not response or not response.choices:
            return LLMResponse(
                text=None, tool_calls=[], stop_reason="end_turn",
                usage=Usage(), raw=response,
            )

        choice: Any = response.choices[0]
        message: Any = choice.message

        # Extract text content — string or list of content chunks
        text: str | None = None
        if isinstance(message.content, str):
            text = message.content or None
        elif isinstance(message.content, list):
            parts = []
            for chunk in message.content:
                if hasattr(chunk, "text"):
                    parts.append(chunk.text)
            text = "\n".join(parts) if parts else None

        # Extract tool calls
        tool_calls: list[ToolCall] = []
        for tc in message.tool_calls or []:
            tool_calls.append(ToolCall(
                id=tc.id or "",
                name=tc.function.name,
                arguments=self._parse_tool_args(tc.function.arguments),
            ))

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=self._map_stop_reason(choice.finish_reason),
            usage=self._parse_usage(response.usage),
            raw=response,
        )

    # ── Streaming ───────────────────────────────────────────────────

    async def stream(
        self, system: SystemPrompt, messages: list[dict[str, Any]], tools: list[dict[str, Any]], **kwargs: Any,
    ) -> AsyncIterator[StreamDelta]:
        """Stream response deltas from Mistral chat completions API.

        Uses the SDK's ``chat.stream()`` context manager via
        ``threaded_stream``.  Falls back to ``complete()`` if
        streaming is not supported by the configured model.
        """
        if not self._capabilities.supports_streaming:
            async for delta in stream_fallback(self, system, messages, tools, **kwargs):
                yield delta
            return

        api_messages: list[dict[str, Any]] = []
        if system:
            api_messages.append({"role": "system", "content": system})
        api_messages.extend(messages)

        params: dict[str, Any] = {
            "model": self.model,
            "messages": api_messages,
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
        }
        if tools:
            params["tools"] = tools

        client = self.client  # bind for closure

        def _stream_events() -> Any:
            """Sync generator wrapping the SDK EventStream context manager."""
            with client.chat.stream(**params) as stream:
                yield from stream

        tool_index = -1
        async for event in threaded_stream(_stream_events):
            chunk: Any = event.data

            if not chunk.choices:
                # Usage-only chunk (final)
                if chunk.usage:
                    yield StreamDelta(usage=self._parse_usage(chunk.usage))
                continue

            choice: Any = chunk.choices[0]
            msg_delta: Any = choice.delta

            # Text content
            if msg_delta and msg_delta.content and isinstance(msg_delta.content, str):
                yield StreamDelta(text=msg_delta.content)

            # Tool call deltas (follows OpenAI streaming convention)
            if msg_delta and msg_delta.tool_calls:
                for tc_delta in msg_delta.tool_calls:
                    idx: int = int(tc_delta.index) if tc_delta.index is not None else 0
                    tc_id = str(tc_delta.id) if tc_delta.id else ""
                    if tc_id and tc_id != "null":
                        tool_index = max(tool_index, idx)
                        yield StreamDelta(
                            tool_call_index=idx,
                            tool_call_id=tc_id,
                            tool_name=str(tc_delta.function.name) if tc_delta.function and tc_delta.function.name else "",
                        )
                    if tc_delta.function and tc_delta.function.arguments:
                        args: Any = tc_delta.function.arguments
                        args_str = json.dumps(args) if isinstance(args, dict) else str(args)
                        yield StreamDelta(
                            tool_call_index=idx,
                            tool_args_delta=args_str,
                        )

            # Finish reason
            if choice.finish_reason:
                yield StreamDelta(stop_reason=self._map_stop_reason(choice.finish_reason))

            # Usage on final chunk
            if chunk.usage:
                yield StreamDelta(usage=self._parse_usage(chunk.usage))
