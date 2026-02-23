"""Messaging tools — channel-agnostic message sending and reactions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# Channel reference set at daemon startup
_channel: Any = None
_get_timestamp: Callable | None = None


def set_channel(channel: Any) -> None:
    global _channel
    _channel = channel


def set_timestamp_getter(fn: Callable) -> None:
    """Set callback to retrieve last inbound message timestamp for a sender."""
    global _get_timestamp
    _get_timestamp = fn


def configure(contact_names: list[str] | None = None) -> None:
    """Patch tool descriptions with deployment-specific values."""
    if contact_names:
        names = ", ".join(contact_names)
        target_desc = (
            f"Recipient contact name (case-insensitive). "
            f"Available contacts: {names}. Self-sends are blocked."
        )
    else:
        target_desc = (
            "Recipient contact name. No contacts configured — check deployment config."
        )
    # Patch target description on both message and react tools
    for tool in TOOLS:
        if "target" in tool["input_schema"]["properties"]:
            tool["input_schema"]["properties"]["target"]["description"] = target_desc


async def tool_message(target: str, text: str = "", attachments: list[str] | None = None) -> str:
    """Send a message via the configured channel."""
    if _channel is None:
        return "Error: No channel configured"
    if not text and not attachments:
        return "Error: Must provide text or attachments"
    if attachments:
        from tools.filesystem import _check_path
        for path in attachments:
            err = _check_path(path)
            if err:
                return f"Error: Attachment path not allowed: {path}"
    try:
        await _channel.send(target, text or "", attachments)
        parts = []
        if text:
            parts.append("text")
        if attachments:
            parts.append(f"{len(attachments)} attachment(s)")
        return f"Sent {' + '.join(parts)} to {target}"
    except Exception as e:
        return f"Error: Message delivery failed: {type(e).__name__}"


async def tool_react(target: str, emoji: str, sender: str = "") -> str:
    """Send an emoji reaction to the last message from a sender."""
    if _channel is None:
        return "Error: No channel configured"
    if _get_timestamp is None:
        return "Error: Timestamp tracking not configured"
    ts = _get_timestamp(sender or target)
    if ts is None:
        return f"Error: No recent message timestamp for {sender or target}"
    try:
        await _channel.send_reaction(target, emoji, ts)
        return f"Reacted with {emoji} to {target}'s last message"
    except Exception as e:
        return f"Error: Reaction failed — {e}"


TOOLS = [
    {
        "name": "message",
        "description": (
            "Send a message (text and/or file attachments) to a contact. "
            "In system/HTTP sessions, your text replies are NOT delivered — "
            "this tool is the only way to notify the operator."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Recipient — use a contact name from config. Self-sends are blocked."},
                "text": {"type": "string", "description": "Message text to send"},
                "attachments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of absolute file paths to send as attachments",
                },
            },
            "required": ["target"],
        },
        "function": tool_message,
    },
    {
        "name": "react",
        "description": "Send an emoji reaction to the last message from a contact.",
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Contact to send the reaction to"},
                "emoji": {
                    "type": "string",
                    "description": "Telegram-allowed reaction emoji.",
                    "enum": [
                        "❤", "👍", "👎", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱",
                        "🤬", "😢", "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊", "🤡",
                        "🥱", "🥴", "😍", "🐳", "❤\u200d🔥", "🌚", "🌭", "💯", "🤣", "⚡",
                        "🍌", "🏆", "💔", "🤨", "😐", "🍓", "🍾", "💋", "🖕", "😈",
                        "😴", "😭", "🤓", "👻", "👨\u200d💻", "👀", "🎃", "🙈", "😇", "😨",
                        "🤝", "✍", "🤗", "🫡", "🎅", "🎄", "☃", "💅", "🤪", "🗿",
                        "🆒", "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾",
                        "🤷\u200d♂", "🤷", "🤷\u200d♀", "😡"
                    ],
                },
                "sender": {"type": "string", "description": "Contact who sent the message to react to. Leave empty to react to target's last message (most common)."},
            },
            "required": ["target", "emoji"],
        },
        "function": tool_react,
    },
]
