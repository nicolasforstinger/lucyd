"""Session status tool — session_status.

Returns current session stats for context-aware agents.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from . import ToolSpec

if TYPE_CHECKING:
    from config import Config
    from providers import LLMProvider
    from session import Session, SessionManager

# Set once at daemon startup via configure()
_session_manager: SessionManager | None = None
_daemon_start_time: float = 0.0
_session_getter: Callable[[], Session | None] | None = None

MAX_CONTEXT_TOKENS = 0


def configure(session_manager: SessionManager | None = None,
              start_time: float = 0.0, max_context_tokens: int = 0,
              session_getter: Callable[[], Session | None] | None = None,
              config: Config | None = None, provider: LLMProvider | None = None,
              **_: object) -> None:
    global _session_manager, _daemon_start_time
    global MAX_CONTEXT_TOKENS, _session_getter
    if session_manager is not None:
        _session_manager = session_manager
    # Prefer provider.capabilities for max_context_tokens
    if provider is not None:
        mct = provider.capabilities.max_context_tokens
        if mct > 0:
            MAX_CONTEXT_TOKENS = mct
    elif config is not None:
        primary_cfg = config.raw("models", "primary", default={})
        mct = primary_cfg.get("max_context_tokens", 0)
        if mct > 0:
            MAX_CONTEXT_TOKENS = mct
    elif max_context_tokens > 0:
        MAX_CONTEXT_TOKENS = max_context_tokens
    if start_time:
        _daemon_start_time = start_time
    if session_getter is not None:
        _session_getter = session_getter


def tool_session_status() -> str:
    """Return current session and daemon statistics."""
    lines = []

    # Context utilization (from current session via callback)
    session = _session_getter() if _session_getter else None
    if session is not None:
        tokens = session.last_input_tokens
        if MAX_CONTEXT_TOKENS > 0:
            pct = tokens * 100 / MAX_CONTEXT_TOKENS if tokens else 0
            lines.append(f"Context: {tokens:,} tokens ({pct:.0f}% of {MAX_CONTEXT_TOKENS:,})")
        else:
            lines.append(f"Context: {tokens:,} tokens")
        lines.append(f"Messages: {len(session.messages)}")
        lines.append(f"Compactions: {session.compaction_count}")

    # Daemon uptime
    if _daemon_start_time:
        uptime_s = time.time() - _daemon_start_time
        hours = int(uptime_s // 3600)
        minutes = int((uptime_s % 3600) // 60)
        lines.append(f"Daemon uptime: {hours}h {minutes}m")

    # Today's cost from metering DB — requires async; skipped in sync tool.
    # Cost is surfaced via /status HTTP endpoint (lucyd.py) instead.

    if not lines:
        lines.append("No status data available.")

    return "\n".join(lines)


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="session_status",
        description="Get current session statistics — context utilization, message count, compaction count, cost, uptime.",
        input_schema={
            "type": "object",
            "properties": {},
        },
        function=tool_session_status,
    ),
]
