"""OpenAI-compatible provider.

Works with OpenAI cloud, Ollama, vLLM, llama.cpp server, LM Studio, LocalAI,
or any server implementing the OpenAI chat completions API.
Uses the OpenAI SDK when available and falls back to direct HTTP requests
when it is not.

Small-model optimizations:
- Thinking token detection: parses <think>...</think> blocks from reasoning models
- Prompt cache awareness: reads cached_tokens from llama-server extended usage
- JSON repair: attempts to fix malformed tool call arguments
- Thinking budget: passes thinking budget parameters to compatible servers
- Slot affinity: supports id_slot for per-session prompt cache pinning
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
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
    import openai
except ImportError:
    openai = None  # type: ignore[assignment]  # conditional import — module when installed, None otherwise


def _repair_json(raw: str) -> dict[str, Any] | None:
    """Attempt to repair malformed JSON from small models.

    Tries common fixes: trailing commas, single quotes, unquoted keys.
    Returns parsed dict on success, None on failure.
    """
    # Try as-is first (fast path)
    try:
        result: Any = json.loads(raw)
        return result if isinstance(result, dict) else None
    except (json.JSONDecodeError, TypeError):
        pass
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    # Fix trailing commas before } or ]
    s = re.sub(r",\s*([}\]])", r"\1", s)
    # Fix single quotes → double quotes (naive — doesn't handle nested)
    s = s.replace("'", '"')
    try:
        result = json.loads(s)
        return result if isinstance(result, dict) else None
    except json.JSONDecodeError:
        pass
    # Fix unquoted keys: {key: "value"} → {"key": "value"}
    s = re.sub(r"(?<=\{|,)\s*(\w+)\s*:", r' "\1":', s)
    try:
        result = json.loads(s)
        return result if isinstance(result, dict) else None
    except json.JSONDecodeError:
        return None


def _strip_thinking(text: str) -> tuple[str, str]:
    """Extract and remove <think>...</think> blocks from model output.

    Returns (cleaned_text, thinking_content).
    Handles multiple think blocks and partial blocks at the start.
    """
    if not text or "<think>" not in text:
        return text, ""
    thinking_parts: list[str] = []
    cleaned = text
    # Extract complete <think>...</think> blocks
    for match in re.finditer(r"<think>(.*?)</think>", text, re.DOTALL):
        thinking_parts.append(match.group(1).strip())
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL)
    # Handle unclosed <think> at the start (model still thinking)
    if cleaned.strip().startswith("<think>"):
        rest = cleaned.strip()[len("<think>"):]
        thinking_parts.append(rest.strip())
        cleaned = ""
    return cleaned.strip(), "\n".join(thinking_parts)


class OpenAICompatProvider:
    provider_name: str = ""

    def __init__(
        self,
        api_key: str,
        model: str,
        max_tokens: int = 4096,
        base_url: str = "",
        thinking_budget: int = 0,
        slot_id: int = -1,
        capabilities: ModelCapabilities | None = None,
    ):
        self.api_key = api_key or "not-needed"
        self.base_url = base_url.rstrip("/")
        self.client: Any = None  # OpenAI client when SDK installed, None otherwise
        if openai is not None:
            kwargs: dict[str, Any] = {"api_key": self.api_key}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self.client = openai.OpenAI(**kwargs)
        self.model = model
        self.max_tokens = max_tokens
        self.thinking_budget = thinking_budget
        self.slot_id = slot_id  # llama-server slot affinity (-1 = auto)
        self._capabilities = capabilities or ModelCapabilities()

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    def format_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
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

    def format_system(self, blocks: list[dict[str, str]]) -> str:
        """Concatenate system blocks into a single string.

        OpenAI doesn't support cache_control — caching is server-side.
        """
        return "\n\n".join(b["text"] for b in blocks)

    @staticmethod
    def _convert_content_blocks(content: Any) -> Any:
        """Convert neutral image blocks to OpenAI API format.

        Text blocks pass through unchanged. Neutral image blocks
        {"type": "image", "media_type": ..., "data": ...} become
        {"type": "image_url", "image_url": {"url": "data:mime;base64,..."}}.
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
        """Convert internal format to OpenAI API format.

        Converts neutral image blocks to OpenAI's data URI format.
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

    @staticmethod
    def _parse_choice(choice: dict[str, Any]) -> tuple[str | None, list[ToolCall], str, str | None]:
        """Parse an OpenAI choice dict into (text, tool_calls, stop_reason, thinking).

        Shared by complete() and _complete_via_httpx().
        """
        message = choice.get("message") or {}
        content = message.get("content") or ""
        thinking_text = None
        if content and "<think>" in content:
            content, thinking_text = _strip_thinking(content)

        tool_calls = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args: Any = fn.get("arguments", {})
            if isinstance(args, str):
                repaired = _repair_json(args)
                args = repaired if repaired is not None else {"raw": args}
            tool_calls.append(ToolCall(
                id=tc.get("id", ""),
                name=fn.get("name", ""),
                arguments=args if isinstance(args, dict) else {"raw": args},
            ))

        stop = "end_turn"
        finish_reason = choice.get("finish_reason", "stop")
        if finish_reason == "tool_calls":
            stop = "tool_use"
        elif finish_reason == "length":
            stop = "max_tokens"

        return content or None, tool_calls, stop, thinking_text

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key and self.api_key != "not-needed":
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    @staticmethod
    def _parse_usage_dict(usage: dict[str, Any] | None) -> Usage:
        usage = usage or {}
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        cache_read = 0
        details = usage.get("prompt_tokens_details") or {}
        if isinstance(details, dict):
            cache_read = int(details.get("cached_tokens", 0) or 0)
        elif hasattr(details, "cached_tokens"):
            cache_read = int(getattr(details, "cached_tokens", 0) or 0)
        if cache_read == 0 and "cached_tokens" in usage:
            cache_read = int(usage.get("cached_tokens", 0) or 0)
        return Usage(
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
            cache_read_tokens=cache_read,
        )

    async def _complete_via_httpx(self, params: dict[str, Any]) -> LLMResponse:
        if not self.base_url:
            raise RuntimeError(
                "OpenAI-compatible provider requires either the openai package "
                "or a configured base_url for direct HTTP fallback",
            )

        # Flatten extra_body into top-level params for direct HTTP requests.
        # extra_body is an SDK concept (OpenAI Python client); llama.cpp and
        # other servers expect these keys (id_slot, thinking_budget) at the
        # top level of the JSON body.
        body = dict(params)
        extra = body.pop("extra_body", None)
        if extra and isinstance(extra, dict):
            body.update(extra)

        url = f"{self.base_url}/chat/completions"
        timeout = max(float(body.get("timeout", 0) or 0), 60.0)
        body.pop("timeout", None)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=self._headers(), json=body)
            response.raise_for_status()
            data = response.json()

        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("OpenAI-compatible fallback returned no choices")

        text, tool_calls, stop, thinking_text = self._parse_choice(choices[0] or {})

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop,
            usage=self._parse_usage_dict(data.get("usage")),
            thinking=thinking_text,
            raw=data,
        )

    async def complete(
        self, system: Any, messages: list[dict[str, Any]], tools: list[dict[str, Any]], **kwargs: Any,
    ) -> LLMResponse:
        """Call OpenAI-compatible chat completions API.

        Handles:
        - Thinking token detection (strips <think> blocks from reasoning models)
        - Prompt cache awareness (reads cached_tokens from llama-server)
        - JSON repair for malformed tool call arguments
        - Thinking budget / slot affinity for llama-server
        """
        api_messages = []
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

        # llama-server extra params (passed via extra_body)
        extra_body: dict[str, Any] = {}
        if self.slot_id >= 0:
            extra_body["id_slot"] = self.slot_id
        if self.thinking_budget > 0 and not tools:
            # Only apply budget on non-tool turns to save generation time
            extra_body["thinking_budget"] = self.thinking_budget
        if extra_body:
            params["extra_body"] = extra_body

        if self.client is None:
            return await self._complete_via_httpx(params)

        response: Any = await run_blocking(
            self.client.chat.completions.create, **params,
        )

        choice = response.choices[0]
        message = choice.message

        # Convert SDK choice to dict for shared parsing
        choice_dict: dict[str, Any] = {
            "message": {
                "content": message.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in (message.tool_calls or [])
                ],
            },
            "finish_reason": choice.finish_reason,
        }
        text, tool_calls, stop, thinking_text = self._parse_choice(choice_dict)

        if thinking_text:
            log.debug("Thinking detected: ~%d chars", len(thinking_text))

        # Parse usage — with llama-server extended fields
        u = response.usage
        usage_dict: dict[str, Any] = {}
        if u:
            usage_dict["prompt_tokens"] = u.prompt_tokens or 0
            usage_dict["completion_tokens"] = u.completion_tokens or 0
            details = getattr(u, "prompt_tokens_details", None)
            if details and hasattr(details, "cached_tokens"):
                usage_dict["cached_tokens"] = details.cached_tokens or 0
            elif hasattr(u, "cached_tokens"):
                usage_dict["cached_tokens"] = getattr(u, "cached_tokens", 0) or 0

            # Log cache miss warning when no caching detected on large prompts
            prompt_tokens = usage_dict.get("prompt_tokens", 0)
            cache_read = usage_dict.get("cached_tokens", 0)
            if cache_read == 0 and prompt_tokens > 1000:
                log.debug(
                    "Prompt cache miss: %d prompt tokens processed cold "
                    "(no cached_tokens in response). Full re-processing may "
                    "indicate slot mismatch or first request.",
                    prompt_tokens,
                )

        usage = self._parse_usage_dict(usage_dict)

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop,
            usage=usage,
            thinking=thinking_text,
            raw=response,
        )

    async def stream(
        self, system: Any, messages: list[dict[str, Any]], tools: list[dict[str, Any]], **kwargs: Any,
    ) -> AsyncIterator[StreamDelta]:
        """Stream response deltas from OpenAI-compatible API.

        Uses the SDK's stream=True parameter. Yields StreamDelta chunks.
        Handles <think> block detection in streaming mode.
        """
        if self.client is None:
            async for delta in stream_fallback(self, system, messages, tools, **kwargs):
                yield delta
            return

        client = self.client  # bind for type narrowing in nested function

        api_messages = []
        if system:
            api_messages.append({"role": "system", "content": system})
        api_messages.extend(messages)

        params: dict[str, Any] = {
            "model": self.model,
            "messages": api_messages,
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            params["tools"] = tools

        extra_body: dict[str, Any] = {}
        if self.slot_id >= 0:
            extra_body["id_slot"] = self.slot_id
        if self.thinking_budget > 0 and not tools:
            extra_body["thinking_budget"] = self.thinking_budget
        if extra_body:
            params["extra_body"] = extra_body

        def _stream_factory() -> Any:
            return client.chat.completions.create(**params)

        in_think = False
        async for item in threaded_stream(_stream_factory):
            if not item.choices and item.usage:
                # Final usage-only chunk
                u = item.usage
                usage_dict: dict[str, Any] = {
                    "prompt_tokens": u.prompt_tokens or 0,
                    "completion_tokens": u.completion_tokens or 0,
                }
                details = getattr(u, "prompt_tokens_details", None)
                if details and hasattr(details, "cached_tokens"):
                    usage_dict["cached_tokens"] = details.cached_tokens or 0
                yield StreamDelta(usage=self._parse_usage_dict(usage_dict))
                continue

            if not item.choices:
                continue

            choice = item.choices[0]
            delta = choice.delta

            # Text content
            if delta and delta.content:
                text = delta.content
                # Detect <think> blocks in streaming
                if "<think>" in text:
                    in_think = True
                    text = text.split("<think>", 1)[0]
                    think_part = delta.content.split("<think>", 1)[1]
                    if text:
                        yield StreamDelta(text=text)
                    if "</think>" in think_part:
                        thinking, rest = think_part.split("</think>", 1)
                        yield StreamDelta(thinking=thinking)
                        in_think = False
                        if rest:
                            yield StreamDelta(text=rest)
                    else:
                        yield StreamDelta(thinking=think_part)
                elif in_think:
                    if "</think>" in text:
                        thinking, rest = text.split("</think>", 1)
                        yield StreamDelta(thinking=thinking)
                        in_think = False
                        if rest:
                            yield StreamDelta(text=rest)
                    else:
                        yield StreamDelta(thinking=text)
                else:
                    yield StreamDelta(text=text)

            # Tool call deltas
            if delta and delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index if tc_delta.index is not None else 0
                    if tc_delta.id:
                        yield StreamDelta(
                            tool_call_index=idx,
                            tool_call_id=tc_delta.id,
                            tool_name=tc_delta.function.name if tc_delta.function else "",
                        )
                    if tc_delta.function and tc_delta.function.arguments:
                        yield StreamDelta(
                            tool_call_index=idx,
                            tool_args_delta=tc_delta.function.arguments,
                        )

            # Finish reason
            if choice.finish_reason:
                stop = "end_turn"
                if choice.finish_reason == "tool_calls":
                    stop = "tool_use"
                elif choice.finish_reason == "length":
                    stop = "max_tokens"
                yield StreamDelta(stop_reason=stop)
