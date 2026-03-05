"""Session status tool — session_status.

Returns current session stats for context-aware agents.
"""

from __future__ import annotations

import time
from typing import Any

from agentic import cost_db_query

# Set at daemon startup
_session_manager: Any = None
_cost_db_path: str = ""
_daemon_start_time: float = 0.0
_current_session: Any = None  # Set by LucydDaemon before each _process_message

MAX_CONTEXT_TOKENS = 0


def configure(session_manager: Any = None, cost_db: str = "",
              start_time: float = 0.0, max_context_tokens: int = 0) -> None:
    global _session_manager, _cost_db_path, _daemon_start_time, MAX_CONTEXT_TOKENS
    _session_manager = session_manager
    _cost_db_path = cost_db
    _daemon_start_time = start_time
    if max_context_tokens > 0:
        MAX_CONTEXT_TOKENS = max_context_tokens


def set_current_session(session: Any) -> None:
    global _current_session
    _current_session = session


def tool_session_status() -> str:
    """Return current session and daemon statistics."""
    lines = []

    # Context utilization (from current session)
    if _current_session is not None:
        tokens = _current_session.last_input_tokens
        if MAX_CONTEXT_TOKENS > 0:
            pct = tokens * 100 / MAX_CONTEXT_TOKENS if tokens else 0
            lines.append(f"Context: {tokens:,} tokens ({pct:.0f}% of {MAX_CONTEXT_TOKENS:,})")
        else:
            lines.append(f"Context: {tokens:,} tokens")
        lines.append(f"Messages: {len(_current_session.messages)}")
        lines.append(f"Compactions: {_current_session.compaction_count}")

    # Daemon uptime
    if _daemon_start_time:
        uptime_s = time.time() - _daemon_start_time
        hours = int(uptime_s // 3600)
        minutes = int((uptime_s % 3600) // 60)
        lines.append(f"Daemon uptime: {hours}h {minutes}m")

    # Today's cost
    if _cost_db_path:
        from config import today_start_ts
        rows = cost_db_query(
            _cost_db_path,
            "SELECT SUM(cost_usd), SUM(input_tokens), SUM(output_tokens) "
            "FROM costs WHERE timestamp >= ?",
            (today_start_ts(),),
        )
        if rows and rows[0][0]:
            lines.append(f"Today's cost: ${rows[0][0]:.4f}")
            lines.append(f"Today's tokens: {rows[0][1]:,} in / {rows[0][2]:,} out")

    if not lines:
        lines.append("No status data available.")

    return "\n".join(lines)


TOOLS = [
    {
        "name": "session_status",
        "description": "Get current session statistics — context utilization, message count, compaction count, cost, uptime.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
        "function": tool_session_status,
    },
]
