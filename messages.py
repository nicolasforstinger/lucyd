"""Message type definitions for the internal message format.

Three roles, discriminated by the ``role`` field:

- **UserMessage**: ``{"role": "user", "content": str}``
- **AssistantMessage**: ``{"role": "assistant", "text": ..., "tool_calls": ..., "usage": ...}``
- **ToolResultsMessage**: ``{"role": "tool_results", "results": [...]}``

The union ``Message = UserMessage | AssistantMessage | ToolResultsMessage``
is narrowed by mypy via ``msg["role"] == "user"`` checks.

These are TypedDicts — plain dicts at runtime.  No construction change,
no serialization change, no wire format change.
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict


class UserMessage(TypedDict):
    """User message.  Content is always ``str`` at rest.

    During image processing, a transient ``_image_blocks`` key may be
    added by the daemon and stripped before persistence
    (``save_state`` filters ``_``-prefixed keys).
    """

    role: Literal["user"]
    content: str


class AssistantMessage(TypedDict):
    """LLM response message.

    Created by ``LLMResponse.to_internal_message()``.
    All fields except ``role`` are optional: ``text`` is absent for
    tool-only responses, ``tool_calls`` absent for text-only,
    ``usage`` stripped during compaction.
    """

    role: Literal["assistant"]
    text: NotRequired[str]
    tool_calls: NotRequired[list[dict[str, Any]]]
    thinking: NotRequired[str]
    thinking_block: NotRequired[dict[str, Any]]
    usage: NotRequired[dict[str, Any]]


class ToolResultsMessage(TypedDict):
    """Tool execution results, paired with a preceding assistant's ``tool_calls``."""

    role: Literal["tool_results"]
    results: list[dict[str, Any]]


Message = UserMessage | AssistantMessage | ToolResultsMessage
