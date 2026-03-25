"""Monitor writer — tracks turn state for the monitor.json file.

Extracted from lucyd.py. Owns mutable turn state so the daemon can
report progress to external watchers (HTTP /api/v1/monitor).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)


class MonitorWriter:
    """Owns turn state for the monitor.json file."""

    __slots__ = ("_path", "_contact", "_session_id", "_trace_id", "_model",
                 "_turn", "_turn_started_at", "_message_started_at", "_turns")

    def __init__(self, path: Path, contact: str, session_id: str,
                 trace_id: str, model: str):
        self._path = path
        self._contact = contact
        self._session_id = session_id
        self._trace_id = trace_id
        self._model = model
        self._turn = 1
        self._turn_started_at = time.time()
        self._message_started_at = self._turn_started_at
        self._turns: list[dict] = []

    def write(self, state: str, tools_in_flight: list[str] | None = None) -> None:
        data = {
            "state": state,
            "contact": self._contact,
            "session_id": self._session_id,
            "trace_id": self._trace_id,
            "model": self._model,
            "turn": self._turn,
            "message_started_at": self._message_started_at,
            "turn_started_at": self._turn_started_at,
            "tools_in_flight": tools_in_flight or [],
            "turns": self._turns,
            "updated_at": time.time(),
        }
        try:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.rename(self._path)
        except Exception as exc:
            log.warning("Monitor write failed: %s", exc)

    def on_response(self, response) -> None:
        duration_ms = int((time.time() - self._turn_started_at) * 1000)
        tool_names = [tc.name for tc in response.tool_calls] if response.tool_calls else []
        self._turns.append({
            "duration_ms": duration_ms,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cache_read_tokens": response.usage.cache_read_tokens,
            "cache_write_tokens": response.usage.cache_write_tokens,
            "stop_reason": response.stop_reason,
            "tools": tool_names,
        })
        if response.stop_reason == "tool_use" and response.tool_calls:
            self.write("tools", tools_in_flight=tool_names)
        else:
            self.write("idle")

    def on_tool_results(self, results_msg) -> None:
        self._turn += 1
        self._turn_started_at = time.time()
        self.write("thinking")
