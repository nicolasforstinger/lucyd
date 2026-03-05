"""Scheduled message delivery — asyncio timer-based.

Non-persistent: timers are lost on daemon restart.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

_channel: Any = None
_scheduled: dict[str, dict] = {}  # id → {target, text, fire_at, task}
_counter = 0
_max_scheduled: int = 50
_max_delay: int = 86400


def configure(channel: Any, contact_names: list[str] | None = None,
              max_scheduled: int = 50, max_delay: int = 86400) -> None:
    global _channel, _max_scheduled, _max_delay
    _channel = channel
    _max_scheduled = max_scheduled
    _max_delay = max_delay
    if contact_names:
        names = ", ".join(contact_names)
        TOOLS[0]["input_schema"]["properties"]["target"]["description"] = (
            f"Recipient contact name. Available contacts: {names}."
        )


async def tool_schedule_message(target: str, text: str, delay_seconds: int) -> str:
    """Schedule a message for future delivery."""
    if _channel is None:
        return "Error: No channel configured"
    if len(_scheduled) >= _max_scheduled:
        return f"Error: Maximum {_max_scheduled} scheduled messages reached"
    if delay_seconds <= 0:
        return "Error: delay_seconds must be positive"
    if delay_seconds > _max_delay:
        return f"Error: Maximum delay is {_max_delay} seconds"
    if not text:
        return "Error: Message text is required"

    global _counter
    _counter += 1
    sched_id = f"sched-{_counter}"
    fire_at = time.time() + delay_seconds

    async def _fire():
        await asyncio.sleep(delay_seconds)
        try:
            await _channel.send(target, text, None)
        finally:
            _scheduled.pop(sched_id, None)

    task = asyncio.create_task(_fire())
    _scheduled[sched_id] = {
        "id": sched_id,
        "target": target,
        "text": text,
        "fire_at": fire_at,
        "task": task,
    }

    minutes = delay_seconds // 60
    if minutes > 0:
        return f"Scheduled message to {target} in {minutes}m ({sched_id})"
    return f"Scheduled message to {target} in {delay_seconds}s ({sched_id})"


async def tool_list_scheduled() -> str:
    """List all pending scheduled messages."""
    active = {k: v for k, v in _scheduled.items() if not v["task"].done()}
    if not active:
        return "No scheduled messages pending."
    now = time.time()
    lines = []
    for sid, info in active.items():
        remaining = max(0, int(info["fire_at"] - now))
        m, s = divmod(remaining, 60)
        lines.append(f"- {sid}: to {info['target']} in {m}m{s}s — \"{info['text'][:50]}\"")
    return "\n".join(lines)


TOOLS = [
    {
        "name": "schedule_message",
        "description": (
            "Schedule a message to be sent after a delay. Non-persistent — lost on daemon restart. "
            "Maximum delay: 24 hours. Maximum 50 pending scheduled messages."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Recipient contact name"},
                "text": {"type": "string", "description": "Message text to send"},
                "delay_seconds": {"type": "integer", "description": "Seconds to wait before sending"},
            },
            "required": ["target", "text", "delay_seconds"],
        },
        "function": tool_schedule_message,
    },
    {
        "name": "list_scheduled",
        "description": "List all pending scheduled messages with their delivery times.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
        "function": tool_list_scheduled,
    },
]
