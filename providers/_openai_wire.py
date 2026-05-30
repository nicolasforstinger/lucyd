"""Shared OpenAI-compatible wire formatting.

OpenAIProvider and MistralProvider both speak the OpenAI chat-completions wire
format, so their tool/system/message formatting is identical. It lives here
once and both providers inherit it. (Anthropic has its own wire format and does
not use this.)
"""

from __future__ import annotations

import json
from typing import Any

from messages import Message

from . import SystemPrompt


class OpenAIWireMixin:
    """Tool/system/message formatting for OpenAI-compatible chat APIs."""

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

    def format_system(self, blocks: list[dict[str, str]]) -> SystemPrompt:
        """Concatenate system blocks into a single string.

        OpenAI-compatible APIs have no per-block cache control (caching is
        server-side), so the tier tags are flattened away here.
        """
        return "\n\n".join(b["text"] for b in blocks)

    @staticmethod
    def _convert_content_blocks(content: Any) -> Any:
        """Convert neutral image blocks to the OpenAI data-URI image format.

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
        """Convert internal message format to OpenAI chat-completions format."""
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

            elif msg["role"] == "agent":
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

            elif msg["role"] == "tool_result":
                for r in msg["results"]:
                    result.append({
                        "role": "tool",
                        "tool_call_id": r["tool_call_id"],
                        "content": r["content"] if isinstance(r["content"], str)
                                   else json.dumps(r["content"]),
                    })

        return result
