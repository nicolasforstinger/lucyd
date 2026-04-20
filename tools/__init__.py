"""Tool registry — registration, dispatch, error isolation, output truncation."""

from __future__ import annotations

import inspect
import json
import logging
import re
import time
import types
import typing
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any  # Any justified in this module: tool schemas are JSON, tool functions have heterogeneous signatures

import metrics
from plugins import PluginError

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


# ─── @function_tool decorator ────────────────────────────────────
#
# Build a ToolSpec from a Python function's signature + docstring. Replaces
# the hand-written `input_schema={...}` dicts scattered across tools/*.py.
# Type hints map to JSON Schema types; docstrings become descriptions.
#
# Usage:
#
#     @function_tool(name="greet", description="Greet someone by name.")
#     async def greet(name: str, loud: bool = False) -> str:
#         '''
#         Args:
#             name: Person to greet.
#             loud: If True, shout it.
#         '''
#         return "HI" if loud else "hi"
#
# The decorator returns a ToolSpec with input_schema generated from the
# annotations. Default values become the `default:` field. Missing defaults
# mark the param required. `Args:` entries in the docstring supply
# per-parameter descriptions.


_PRIM_TO_JSON: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


def _type_to_schema(tp: Any) -> dict[str, Any]:
    """Map a Python type annotation to a JSON Schema fragment."""
    # Strip Optional[X] → X
    origin = typing.get_origin(tp)
    if origin is typing.Union or origin is types.UnionType:
        union_args: list[Any] = [a for a in typing.get_args(tp) if a is not type(None)]
        if len(union_args) == 1:
            return _type_to_schema(union_args[0])
        # Multiple non-None types: use oneOf
        return {"oneOf": [_type_to_schema(a) for a in union_args]}

    # list[X] / List[X]
    if origin in (list, typing.List):  # noqa: UP006 — intentional cover for older aliases
        list_args = typing.get_args(tp)
        item_schema = _type_to_schema(list_args[0]) if list_args else {}
        return {"type": "array", "items": item_schema}

    # dict[K, V] / Dict[K, V]
    if origin in (dict, typing.Dict):  # noqa: UP006
        return {"type": "object"}

    # Literal["a", "b"] → enum
    if origin is typing.Literal:
        return {"type": "string", "enum": list(typing.get_args(tp))}

    # Primitive
    if tp in _PRIM_TO_JSON:
        return {"type": _PRIM_TO_JSON[tp]}

    # Fallback
    return {"type": "string"}


_DOCSTRING_ARG_RE = re.compile(
    r"^\s+(\w+)\s*:\s*(.+?)(?=\n\s+\w+\s*:|\n\s*$|$)",
    re.MULTILINE | re.DOTALL,
)


def _parse_docstring(doc: str | None) -> tuple[str, dict[str, str]]:
    """Split docstring into (summary, per-arg-description map).

    Recognises an ``Args:`` / ``Arguments:`` / ``Parameters:`` block and pulls
    ``name: description`` pairs out of it. Anything above the block is the
    tool-level summary.
    """
    if not doc:
        return "", {}
    text = inspect.cleandoc(doc)
    parts = re.split(r"\n\s*(?:Args|Arguments|Parameters)\s*:\s*\n", text, maxsplit=1)
    summary = parts[0].strip()
    arg_docs: dict[str, str] = {}
    if len(parts) == 2:
        body = parts[1]
        for m in _DOCSTRING_ARG_RE.finditer("\n" + body):
            name = m.group(1)
            desc = re.sub(r"\s+", " ", m.group(2)).strip()
            arg_docs[name] = desc
    return summary, arg_docs


def function_tool(
    *,
    name: str | None = None,
    description: str | None = None,
    max_output: int = 0,
) -> Callable[[Callable[..., Any]], ToolSpec]:
    """Decorator: turn a typed Python function into a :class:`ToolSpec`.

    Introspects the signature + docstring so tools don't need to duplicate
    their schema as a hand-written ``input_schema`` dict.
    """
    def wrap(fn: Callable[..., Any]) -> ToolSpec:
        sig = inspect.signature(fn)
        hints = typing.get_type_hints(fn, include_extras=False)
        summary, arg_docs = _parse_docstring(fn.__doc__)

        properties: dict[str, Any] = {}
        required: list[str] = []
        for p_name, param in sig.parameters.items():
            if p_name in ("self", "cls"):
                continue
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                continue
            annotation = hints.get(p_name, str)
            schema = _type_to_schema(annotation)
            if p_name in arg_docs:
                schema["description"] = arg_docs[p_name]
            if param.default is inspect.Parameter.empty:
                required.append(p_name)
            else:
                schema["default"] = param.default
            properties[p_name] = schema

        input_schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
        }
        if required:
            input_schema["required"] = required

        return ToolSpec(
            name=name or fn.__name__,
            description=description or summary or fn.__name__,
            input_schema=input_schema,
            function=fn,
            max_output=max_output,
        )
    return wrap


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
        except PluginError as e:
            log.warning("Tool %s plugin error (%s): %s", name, e.code, e)
            if metrics.ENABLED:
                metrics.TOOL_CALLS_TOTAL.labels(tool_name=name, status="error").inc()
            if e.user_safe:
                return {"text": f"Error: {e}", "attachments": []}
            return {
                "text": f"Error: Tool '{name}' is unavailable right now.",
                "attachments": [],
            }
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
