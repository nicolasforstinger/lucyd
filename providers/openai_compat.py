"""OpenAI-compatible provider.

Works with OpenAI cloud, Ollama, vLLM, llama.cpp server, LM Studio, LocalAI,
or any server implementing the OpenAI chat completions API.
Conditional import — fails with clear message if openai SDK not installed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from . import LLMResponse, ToolCall, Usage

log = logging.getLogger(__name__)

try:
    import openai
except ImportError:
    openai = None  # type: ignore[assignment]


class OpenAICompatProvider:
    def __init__(
        self,
        api_key: str,
        model: str,
        max_tokens: int = 4096,
        base_url: str = "",
    ):
        if openai is None:
            raise RuntimeError(
                "OpenAI-compatible provider requires: pip install openai"
            )
        kwargs: dict = {"api_key": api_key or "not-needed"}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = openai.OpenAI(**kwargs)
        self.model = model
        self.max_tokens = max_tokens

    def format_tools(self, tools: list[dict]) -> list[dict]:
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

    def format_system(self, blocks: list[dict]) -> str:
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

    def format_messages(self, messages: list[dict]) -> list[dict]:
        """Convert internal format to OpenAI API format.

        Converts neutral image blocks to OpenAI's data URI format.
        """
        result = []
        for msg in messages:
            role = msg.get("role", "")

            if role == "user":
                content = msg.get("content", msg.get("text", ""))
                result.append({
                    "role": "user",
                    "content": self._convert_content_blocks(content),
                })

            elif role == "assistant":
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

            elif role == "tool_results":
                for r in msg.get("results", []):
                    result.append({
                        "role": "tool",
                        "tool_call_id": r["tool_call_id"],
                        "content": r["content"] if isinstance(r["content"], str)
                                   else json.dumps(r["content"]),
                    })

        return result

    async def complete(
        self, system: Any, messages: list[dict], tools: list[dict], **kwargs
    ) -> LLMResponse:
        """Call OpenAI-compatible chat completions API."""
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

        response = await asyncio.to_thread(
            self.client.chat.completions.create, **params
        )

        choice = response.choices[0]
        message = choice.message

        text = message.content
        tool_calls = []
        if message.tool_calls:
            for tc in message.tool_calls:
                args = tc.function.arguments
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"raw": args}
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args,
                ))

        u = response.usage
        usage = Usage(
            input_tokens=u.prompt_tokens if u else 0,
            output_tokens=u.completion_tokens if u else 0,
        )

        stop = "end_turn"
        if choice.finish_reason == "tool_calls":
            stop = "tool_use"
        elif choice.finish_reason == "length":
            stop = "max_tokens"

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop,
            usage=usage,
            raw=response,
        )
