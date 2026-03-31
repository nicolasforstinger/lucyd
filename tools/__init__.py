"""Tool registry — registration, dispatch, error isolation, output truncation."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import metrics

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolSpec:
    """Typed definition of a tool available to the agentic loop.

    Replaces the raw dict convention (``{"name": ..., "description": ..., ...}``).
    Frozen because tool definitions don't change after registration.
    """
    name: str
    description: str
    input_schema: dict[str, Any]
    function: Callable[..., Any]
    max_output: int = 0  # per-tool truncation limit (0 = use registry default)


def _smart_truncate(text: str, limit: int, tool_name: str = "") -> str:
    """Truncate tool output, preserving structure when possible.

    Strategy:
    1. If text is valid JSON array: keep first N items, append count marker.
    2. If text is valid JSON object: use compact formatting, then character-cut.
    3. Otherwise: cut at last newline before limit to avoid mid-line breaks.
    Always appends a clear truncation marker so the model knows data is missing.
    """
    if len(text) <= limit:
        return text

    log.warning("Tool %s output truncated: %d → %d chars",
                tool_name or "?", len(text), limit)

    # Try JSON-aware truncation
    stripped = text.strip()
    if stripped.startswith(("[", "{")):
        try:
            data = json.loads(stripped)
            return _truncate_json(data, limit)
        except (json.JSONDecodeError, ValueError):
            pass

    # Head+tail truncation: keep beginning and end for context
    total = len(text)
    marker_template = "\n[...truncated {:,} of {:,} chars...]\n"
    marker_len = len(marker_template.format(total, total)) + 10  # padding
    usable = limit - marker_len
    if usable < 200:
        # Too tight for head+tail — just do head
        cut = text[:limit]
        last_nl = cut.rfind("\n")
        if last_nl > limit * 0.8:
            cut = cut[:last_nl]
        return cut + f"\n[truncated — showing {len(cut):,} of {total:,} chars]"

    head_size = int(usable * 0.7)
    tail_size = usable - head_size
    head = text[:head_size]
    tail = text[-tail_size:]
    skipped = total - head_size - tail_size
    return f"{head}\n[...truncated {skipped:,} of {total:,} chars...]\n{tail}"


def _truncate_json(data: Any, limit: int) -> str:
    """Truncate parsed JSON data to fit within character limit."""
    if isinstance(data, list):
        total_items = len(data)
        # Binary search: find largest N items that fit
        lo, hi = 0, total_items
        best = "[]"
        while lo <= hi:
            mid = (lo + hi) // 2
            subset = data[:mid]
            candidate = json.dumps(subset, ensure_ascii=False)
            marker = f'\n[truncated — showing {mid} of {total_items} items]'
            if len(candidate) + len(marker) <= limit:
                best = candidate + (marker if mid < total_items else "")
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    if isinstance(data, dict):
        # Try compact then fall back to character truncation
        compact = json.dumps(data, ensure_ascii=False)
        if len(compact) <= limit:
            return compact
        total = len(compact)
        return compact[:limit] + f"\n[truncated — showing {limit:,} of {total:,} chars]"

    # Scalar or other — stringify and truncate
    text = json.dumps(data, ensure_ascii=False)
    if len(text) <= limit:
        return text
    total = len(text)
    return text[:limit] + f"\n[truncated — showing {limit:,} of {total:,} chars]"


class ToolRegistry:
    """Registers tool functions and dispatches calls from the agentic loop."""

    def __init__(self, truncation_limit: int = 30000, max_result_tokens: int = 0):
        self._tools: dict[str, ToolSpec] = {}
        self.truncation_limit = truncation_limit
        self.max_result_tokens = max_result_tokens  # 0 = use char limit only

    def register(self, spec: ToolSpec) -> None:
        """Register a tool from a ToolSpec."""
        self._tools[spec.name] = spec

    def register_many(self, tools: list[ToolSpec]) -> None:
        """Register multiple tools from a TOOLS list."""
        for spec in tools:
            self.register(spec)

    def get_schemas(self) -> list[dict[str, Any]]:
        """Return tool schemas for LLM (without function references)."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in self._tools.values()
        ]

    def get_brief_descriptions(self) -> list[tuple[str, str]]:
        """Return (name, description) pairs for context builder."""
        return [(t.name, t.description) for t in self._tools.values()]

    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a tool call with error isolation and smart truncation.

        Returns {"text": str, "attachments": list[str]}.
        ``text`` is the truncated string shown to the LLM.
        ``attachments`` lists file paths the tool produced (empty for most tools).
        """
        if name not in self._tools:
            available = ", ".join(sorted(self._tools.keys()))
            return {
                "text": (
                    f"Error: Tool '{name}' is not available. "
                    f"Available tools: {available}. "
                    f"Check your available tools and try a different approach."
                ),
                "attachments": [],
            }

        spec = self._tools[name]
        _tool_start = time.time()
        try:
            import inspect
            if inspect.iscoroutinefunction(spec.function):
                result = await spec.function(**arguments)
            else:
                result = spec.function(**arguments)
            if metrics.ENABLED:
                metrics.TOOL_CALLS_TOTAL.labels(tool_name=name, status="success").inc()
                metrics.TOOL_DURATION.labels(tool_name=name).observe(time.time() - _tool_start)
        except TypeError as e:
            log.warning("Tool %s argument error: %s", name, e)
            if metrics.ENABLED:
                metrics.TOOL_CALLS_TOTAL.labels(tool_name=name, status="error").inc()
            return {"text": f"Error: Invalid arguments for '{name}': {e}", "attachments": []}
        except Exception as e:
            log.error("Tool %s failed: %s", name, e, exc_info=True)
            if metrics.ENABLED:
                metrics.TOOL_CALLS_TOTAL.labels(tool_name=name, status="error").inc()
            return {
                "text": (
                    f"Error: Tool '{name}' failed ({type(e).__name__}). "
                    f"Try a different approach or check your arguments."
                ),
                "attachments": [],
            }

        # Structured result: tool returned {"text": ..., "attachments": [...]}
        attachments: list[str] = []
        if isinstance(result, dict) and "text" in result:
            attachments = result.get("attachments", [])
            result_str = str(result["text"])
        else:
            result_str = str(result) if not isinstance(result, str) else result

        limit = spec.max_output or self.truncation_limit
        if self.max_result_tokens > 0:
            from context import _estimate_tokens
            est = _estimate_tokens(result_str)
            if est > self.max_result_tokens:
                ratio = self.max_result_tokens / max(est, 1)
                token_derived_limit = int(len(result_str) * ratio)
                limit = min(limit, token_derived_limit)
        return {
            "text": _smart_truncate(result_str, limit, tool_name=name),
            "attachments": attachments,
        }

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())
