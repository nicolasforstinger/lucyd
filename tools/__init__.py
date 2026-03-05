"""Tool registry — registration, dispatch, error isolation, output truncation."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)


def _smart_truncate(text: str, limit: int) -> str:
    """Truncate tool output, preserving structure when possible.

    Strategy:
    1. If text is valid JSON array: keep first N items, append count marker.
    2. If text is valid JSON object: use compact formatting, then character-cut.
    3. Otherwise: cut at last newline before limit to avoid mid-line breaks.
    Always appends a clear truncation marker so the model knows data is missing.
    """
    if len(text) <= limit:
        return text

    # Try JSON-aware truncation
    stripped = text.strip()
    if stripped.startswith(("[", "{")):
        try:
            data = json.loads(stripped)
            return _truncate_json(data, limit)
        except (json.JSONDecodeError, ValueError):
            pass

    # Line-boundary truncation: cut at last newline within budget
    cut = text[:limit]
    last_nl = cut.rfind("\n")
    if last_nl > limit * 0.8:
        cut = cut[:last_nl]

    total = len(text)
    return cut + f"\n[truncated — showing {len(cut):,} of {total:,} chars]"


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

    def __init__(self, truncation_limit: int = 30000):
        self._tools: dict[str, dict] = {}
        self.truncation_limit = truncation_limit

    def register(self, name: str, description: str, input_schema: dict,
                 func: Callable[..., Any], max_output: int = 0) -> None:
        """Register a tool function.

        max_output: per-tool truncation limit (0 = use registry default).
        """
        self._tools[name] = {
            "name": name,
            "description": description,
            "input_schema": input_schema,
            "function": func,
            "max_output": max_output,
        }

    def register_many(self, tools: list[dict]) -> None:
        """Register multiple tools from a TOOLS list."""
        for t in tools:
            self.register(
                name=t["name"],
                description=t["description"],
                input_schema=t["input_schema"],
                func=t["function"],
                max_output=t.get("max_output", 0),
            )

    def get_schemas(self) -> list[dict]:
        """Return tool schemas for LLM (without function references)."""
        return [
            {
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["input_schema"],
            }
            for t in self._tools.values()
        ]

    def get_brief_descriptions(self) -> list[tuple[str, str]]:
        """Return (name, description) pairs for context builder."""
        return [(t["name"], t["description"]) for t in self._tools.values()]

    async def execute(self, name: str, arguments: dict) -> str:
        """Execute a tool call with error isolation and smart truncation."""
        if name not in self._tools:
            available = ", ".join(sorted(self._tools.keys()))
            return (
                f"Error: Tool '{name}' is not available. "
                f"Available tools: {available}. "
                f"Check your available tools and try a different approach."
            )

        tool = self._tools[name]
        func = tool["function"]
        try:
            import asyncio
            import inspect
            if inspect.iscoroutinefunction(func):
                result = await func(**arguments)
            else:
                result = await asyncio.to_thread(func, **arguments)
        except TypeError as e:
            log.warning("Tool %s argument error: %s", name, e)
            return f"Error: Invalid arguments for '{name}': {e}"
        except Exception as e:
            log.error("Tool %s failed: %s", name, e, exc_info=True)
            return (
                f"Error: Tool '{name}' failed ({type(e).__name__}). "
                f"Try a different approach or check your arguments."
            )

        result_str = str(result) if not isinstance(result, str) else result
        limit = tool.get("max_output") or self.truncation_limit
        return _smart_truncate(result_str, limit)

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())
