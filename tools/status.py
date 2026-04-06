"""Session status tool — session_status.

Returns current session stats for context-aware agents.
"""

from __future__ import annotations

import time
from typing import Any

from . import ToolSpec

# Set once at daemon startup via configure()
_session_manager: Any = None
_metering: Any = None  # MeteringDB instance
_daemon_start_time: float = 0.0
_session_getter: Any = None  # Callback returning current session

MAX_CONTEXT_TOKENS = 0


def configure(session_manager: Any = None,
              start_time: float = 0.0, max_context_tokens: int = 0,
              session_getter: Any = None,
              config: Any = None, provider: Any = None,
              metering: Any = None, **_: Any) -> None:
    global _session_manager, _metering, _daemon_start_time
    global MAX_CONTEXT_TOKENS, _session_getter
    if session_manager is not None:
        _session_manager = session_manager
    if metering is not None:
        _metering = metering
    # Prefer provider.capabilities for max_context_tokens
    if provider is not None and hasattr(provider, "capabilities"):
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

    # Today's cost from metering DB
    if _metering is not None:
        from config import today_start_ts
        rows = _metering.query(
            "SELECT COALESCE(SUM(cost), 0.0), "
            "COALESCE(SUM(input_tokens), 0), "
            "COALESCE(SUM(output_tokens), 0) "
            "FROM costs WHERE timestamp >= ?",
            (today_start_ts(),),
        )
        if rows and rows[0][0]:
            lines.append(f"Today's cost: {rows[0][0]:.4f} EUR")
            lines.append(f"Today's tokens: {rows[0][1]:,} in / {rows[0][2]:,} out")

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
