"""Tool registry — registration, dispatch, error isolation, output truncation."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)


class ToolRegistry:
    """Registers tool functions and dispatches calls from the agentic loop."""

    def __init__(self, truncation_limit: int = 30000):
        self._tools: dict[str, dict] = {}
        self.truncation_limit = truncation_limit

    def register(self, name: str, description: str, input_schema: dict,
                 func: Callable[..., Any]) -> None:
        """Register a tool function."""
        self._tools[name] = {
            "name": name,
            "description": description,
            "input_schema": input_schema,
            "function": func,
        }

    def register_many(self, tools: list[dict]) -> None:
        """Register multiple tools from a TOOLS list."""
        for t in tools:
            self.register(
                name=t["name"],
                description=t["description"],
                input_schema=t["input_schema"],
                func=t["function"],
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
        """Execute a tool call with error isolation and truncation."""
        if name not in self._tools:
            return f"Error: Unknown tool '{name}'"

        func = self._tools[name]["function"]
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
            return f"Error: Tool '{name}' execution failed"

        result_str = str(result) if not isinstance(result, str) else result
        if len(result_str) > self.truncation_limit:
            result_str = result_str[:self.truncation_limit] + \
                f"\n[truncated at {self.truncation_limit} chars]"
        return result_str

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())
