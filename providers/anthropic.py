"""Anthropic-compatible provider.

Works with any model accessible through the Anthropic Messages API.
Supports prompt caching (cache_control) and extended thinking (adaptive/budgeted).
Uses the Anthropic SDK when available and falls back to direct HTTP requests
when it is not.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx

from async_utils import run_blocking, threaded_stream
from messages import Message

from . import (
    LLMResponse,
    ModelCapabilities,
    StreamDelta,
    ToolCall,
    Usage,
    stream_fallback,
)

log = logging.getLogger(__name__)

try:
    import anthropic
    from anthropic._exceptions import OverloadedError as _OverloadedError
except ImportError:
    anthropic = None  # type: ignore[assignment]  # conditional import — module when installed, None otherwise
    _OverloadedError = None  # type: ignore[assignment,misc]  # conditional import fallback


_DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"


class APIStatusError(Exception):
    """SDK-free compatibility error with Anthropic-like shape."""

    def __init__(self, message: str, *, status_code: int, body: Any = None, response: httpx.Response | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.response = response


class APIConnectionError(Exception):
    """Raised when the direct HTTP fallback cannot reach the API."""


class APITimeoutError(Exception):
    """Raised when the direct HTTP fallback times out."""


def _safe_parse_args(raw: Any) -> dict[str, Any]:
    """Parse tool input, handling both dict and string forms.

    Anthropic usually returns a dict, but may return a string in edge cases.
    Matches the OpenAI provider's fallback pattern ({"raw": ...}).
    """
    if isinstance(raw, dict):
        return raw
    try:
        result: Any = json.loads(raw)
        return result if isinstance(result, dict) else {"raw": raw}
    except (json.JSONDecodeError, TypeError):
        return {"raw": raw}


class AnthropicProvider:
    provider_name: str = ""

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
        capabilities: ModelCapabilities | None = None,
    ):
        self.api_key = api_key or "not-needed"
        self.base_url = base_url.rstrip("/")
        self.client: Any = None  # Anthropic client when SDK installed, None otherwise
        if anthropic is not None:
            if self.base_url:
                self.client = anthropic.Anthropic(api_key=self.api_key, base_url=self.base_url)
            else:
                self.client = anthropic.Anthropic(api_key=self.api_key)
        self.model = model
        self.max_tokens = max_tokens
        self.cache_control = cache_control
        self.thinking_enabled = thinking_enabled
        self.thinking_budget = thinking_budget
        self.thinking_effort = thinking_effort  # "high", "medium", "low" or ""
        self._capabilities = capabilities or ModelCapabilities()
        # "adaptive" | "budgeted" | "disabled" — overrides thinking_enabled if set
        self.thinking_mode = thinking_mode

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    def format_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        formatted = []
        for t in tools:
            formatted.append({
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["input_schema"],
            })
        return formatted

    def format_system(self, blocks: list[dict[str, str]]) -> list[dict[str, Any]]:
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

    def format_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Convert internal format to Anthropic API format.

        Preserves thinking blocks with signatures for tool-use continuity.
        Converts neutral image blocks to Anthropic's nested source format.
        """
        result: list[dict[str, Any]] = []
        for msg in messages:
            if msg["role"] == "user":
                content: Any = msg["content"]
                image_blocks: list[dict[str, Any]] | None = msg.get("_image_blocks")  # type: ignore[assignment]  # transient key, stripped before persistence
                if image_blocks:
                    content = image_blocks + [{"type": "text", "text": content if isinstance(content, str) else ""}]
                result.append({"role": "user", "content": self._convert_content_blocks(content)})

            elif msg["role"] == "assistant":
                content_blocks: list[dict[str, Any]] = []
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

            elif msg["role"] == "tool_results":
                tool_content: list[dict[str, Any]] = []
                for r in msg["results"]:
                    tool_content.append({
                        "type": "tool_result",
                        "tool_use_id": r["tool_call_id"],
                        "content": r["content"] if isinstance(r["content"], str)
                                   else str(r.get("content", "")),
                    })
                if tool_content:
                    result.append({"role": "user", "content": tool_content})

        return result

    def _build_thinking_param(self) -> dict[str, Any] | None:
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

    def _headers(self) -> dict[str, str]:
        headers = {
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        if self.api_key and self.api_key != "not-needed":
            headers["x-api-key"] = self.api_key
        return headers

    def _messages_url(self) -> str:
        base = self.base_url or _DEFAULT_ANTHROPIC_BASE_URL
        base = base.rstrip("/")
        if base.endswith("/messages"):
            return base
        if base.endswith("/v1"):
            return f"{base}/messages"
        return f"{base}/v1/messages"

    @staticmethod
    def _sdk_response_to_dict(response: Any) -> dict[str, Any]:
        """Convert an Anthropic SDK Message object to a plain dict
        compatible with _response_to_llm()."""
        content = []
        for block in response.content:
            if block.type == "text":
                content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                content.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
            elif block.type == "thinking":
                entry: dict[str, Any] = {
                    "type": "thinking",
                    "thinking": block.thinking,
                }
                if hasattr(block, "signature") and block.signature:
                    entry["signature"] = block.signature
                content.append(entry)
            elif block.type == "redacted_thinking":
                content.append({"type": "redacted_thinking", "data": block.data})
        u = response.usage
        usage = {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        }
        return {
            "content": content,
            "stop_reason": response.stop_reason,
            "usage": usage,
        }

    @staticmethod
    def _parse_usage_dict(usage: dict[str, Any] | None) -> Usage:
        usage = usage or {}
        return Usage(
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cache_read_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
            cache_write_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
        )

    @classmethod
    def _response_to_llm(cls, response_data: dict[str, Any]) -> LLMResponse:
        text_parts = []
        tool_calls = []
        thinking_text = None
        thinking_block = None

        for block in response_data.get("content", []) or []:
            btype = block.get("type", "")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    arguments=_safe_parse_args(block.get("input")),
                ))
            elif btype == "thinking":
                thinking_text = block.get("thinking")
                thinking_block = {
                    "type": "thinking",
                    "thinking": block.get("thinking", ""),
                }
                if block.get("signature"):
                    thinking_block["signature"] = block["signature"]
            elif btype == "redacted_thinking" and thinking_block is None:
                thinking_block = {"type": "redacted_thinking", "data": block.get("data")}

        stop = "end_turn"
        if response_data.get("stop_reason") == "tool_use":
            stop = "tool_use"
        elif response_data.get("stop_reason") == "max_tokens":
            stop = "max_tokens"

        return LLMResponse(
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            stop_reason=stop,
            usage=cls._parse_usage_dict(response_data.get("usage")),
            thinking=thinking_text,
            raw=response_data,
            _thinking_block=thinking_block,
        )

    async def _complete_via_httpx(self, params: dict[str, Any]) -> LLMResponse:
        timeout = max(float(params.get("timeout", 0) or 0), 600.0 if "thinking" in params else 60.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    self._messages_url(),
                    headers=self._headers(),
                    json=params,
                )
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise APITimeoutError(str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            body = None
            try:
                body = exc.response.json()
            except ValueError:
                body = None
            message = str(exc)
            if isinstance(body, dict):
                err = body.get("error", body)
                if isinstance(err, dict) and err.get("message"):
                    message = err["message"]
            raise APIStatusError(
                message,
                status_code=exc.response.status_code,
                body=body,
                response=exc.response,
            ) from exc
        except httpx.RequestError as exc:
            raise APIConnectionError(str(exc)) from exc

        return self._response_to_llm(response.json())

    async def complete(
        self, system: Any, messages: list[dict[str, Any]], tools: list[dict[str, Any]], **kwargs: Any,
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

        if self.client is None:
            return await self._complete_via_httpx(params)

        if thinking_param:
            # Streaming required when thinking is enabled — SDK enforces this
            # to avoid HTTP timeouts on long reasoning chains.
            def _stream_to_message() -> Any:
                assert self.client is not None
                with self.client.messages.stream(**params) as stream:
                    return stream.get_final_message()

            try:
                response: Any = await run_blocking(_stream_to_message)
            except anthropic.APIStatusError as e:
                # HOTFIX(2026-02-27): Anthropic SDK mid-stream SSE error misclassification.
                # SDK bug (v0.81.0, github.com/anthropics/anthropic-sdk-python#688):
                # Stream.__stream__() catches SSE "error" events and calls
                # _make_status_error(response=self.response) — but self.response is
                # the original HTTP 200, not the error. So overloaded_error (529) arrives
                # as APIStatusError(status_code=200). Our retry system sees 200 < 429
                # and skips retry. Fix: inspect body, re-raise as correct exception class
                # with synthesized response carrying the correct status code.
                # Remove when test_sdk_bug_still_exists (test_providers.py) fails.
                if e.status_code < 429:
                    body = getattr(e, "body", None)
                    if isinstance(body, dict):
                        err = body.get("error", body)
                        if isinstance(err, dict):
                            etype = err.get("type", "")
                            # Synthesize the correct HTTP response the SDK
                            # should have used, so the re-raised exception
                            # carries the right status_code end-to-end.
                            if etype == "overloaded_error":
                                resp529 = httpx.Response(529, request=e.response.request)
                                raise _OverloadedError(
                                    str(e), response=resp529, body=body,
                                ) from e
                            if etype == "api_error":
                                resp500 = httpx.Response(500, request=e.response.request)
                                raise anthropic.InternalServerError(
                                    str(e), response=resp500, body=body,
                                ) from e
                raise
        else:
            response = await run_blocking(
                self.client.messages.create, **params,
            )

        # Convert SDK response object to dict for shared parsing
        response_data = self._sdk_response_to_dict(response)
        result = self._response_to_llm(response_data)
        result.raw = response  # preserve SDK object
        return result

    async def stream(
        self, system: Any, messages: list[dict[str, Any]], tools: list[dict[str, Any]], **kwargs: Any,
    ) -> AsyncIterator[StreamDelta]:
        """Stream response deltas from Anthropic Messages API.

        Uses the SDK's messages.stream() context manager and yields
        StreamDelta chunks as events arrive.
        """
        if self.client is None:
            async for delta in stream_fallback(self, system, messages, tools, **kwargs):
                yield delta
            return

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

        def _stream_factory() -> Iterator[Any]:
            assert self.client is not None
            with self.client.messages.stream(**params) as stream:
                yield from stream
                # Final message for usage
                yield stream.get_final_message()

        tool_index = -1
        async for item in threaded_stream(_stream_factory):
            # Final message object (has .usage)
            if hasattr(item, "usage") and hasattr(item, "stop_reason"):
                data = self._sdk_response_to_dict(item)
                usage = self._parse_usage_dict(data.get("usage"))
                stop = "end_turn"
                if item.stop_reason == "tool_use":
                    stop = "tool_use"
                elif item.stop_reason == "max_tokens":
                    stop = "max_tokens"
                yield StreamDelta(stop_reason=stop, usage=usage)
                break

            # SDK event objects
            etype = getattr(item, "type", "")
            if etype == "content_block_start":
                block = item.content_block
                if block.type == "tool_use":
                    tool_index += 1
                    yield StreamDelta(
                        tool_call_index=tool_index,
                        tool_call_id=block.id,
                        tool_name=block.name,
                    )
            elif etype == "content_block_delta":
                delta = item.delta
                if delta.type == "text_delta":
                    yield StreamDelta(text=delta.text)
                elif delta.type == "thinking_delta":
                    yield StreamDelta(thinking=delta.thinking)
                elif delta.type == "input_json_delta":
                    yield StreamDelta(
                        tool_call_index=tool_index,
                        tool_args_delta=delta.partial_json,
                    )
