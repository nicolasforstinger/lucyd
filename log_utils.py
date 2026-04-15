"""Log sanitization and structured logging utilities.

Structured context: agent_id, session_id, and trace_id are stored in
a contextvars.ContextVar so any module can log with these fields without
passing them explicitly. The StructuredJSONFormatter merges them into
every JSON log entry.
"""

from __future__ import annotations

import contextvars
import json
import logging
from typing import Any  # Any justified: JSON log entries have mixed value types

# ─── Log Context ──────────────────────────────────────────────────
# Set once per message processing cycle via set_log_context().
# The JSON formatter reads this on every log call.

_log_context: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "log_context", default=None,
)


def set_log_context(
    agent_id: str = "",
    session_id: str = "",
    trace_id: str = "",
) -> None:
    """Set structured log context for the current async task."""
    ctx: dict[str, str] = {}
    if agent_id:
        ctx["agent_id"] = agent_id
    if session_id:
        ctx["session_id"] = session_id
    if trace_id:
        ctx["trace_id"] = trace_id
    _log_context.set(ctx)



def _log_safe(s: str | None) -> str:
    """Sanitize a string for log output — strip control chars that could forge log entries."""
    if s is None:
        return ""
    return str(s).replace("\n", "\\n").replace("\r", "\\r")


# ─── Structured JSON Formatter ───────────────────────────────────


class StructuredJSONFormatter(logging.Formatter):
    """JSON log formatter that includes agent_id, session_id, trace_id.

    Context fields come from the contextvars set by set_log_context().
    Always emits: ts, level, logger, msg. Context fields are added
    when present.
    """

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
        }
        # Merge context fields
        ctx = _log_context.get() or {}
        if ctx:
            entry.update(ctx)
        entry["msg"] = record.getMessage()
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)
