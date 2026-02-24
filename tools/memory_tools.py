"""Memory tools — memory_search and memory_get.

Optional: only registered if memory DB is configured.
memory_search uses structured recall (facts, episodes, commitments)
with vector fallback. memory_get reads chunks by file path.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

log = logging.getLogger(__name__)

# Set at daemon startup
_memory: Any = None
_conn: sqlite3.Connection | None = None
_config: Any = None
_synth_provider: Any = None


def set_memory(memory_interface: Any) -> None:
    global _memory
    _memory = memory_interface


def set_structured_memory(conn: sqlite3.Connection, config: Any) -> None:
    """Configure structured recall (memory v2)."""
    global _conn, _config
    _conn = conn
    _config = config


def set_synthesis_provider(provider: Any) -> None:
    """Set the provider used for memory synthesis (subagent model)."""
    global _synth_provider
    _synth_provider = provider


async def tool_memory_search(query: str, top_k: int = 10) -> str:
    """Search long-term memory using structured facts, episodes, and vector similarity."""
    if _memory is None:
        return "Error: Memory not configured in this deployment. This tool is unavailable."

    # Try structured recall if configured
    if _conn is not None and _config is not None:
        try:
            from memory import EMPTY_RECALL_FALLBACK, inject_recall, recall
            max_tokens = getattr(_config, "recall_max_dynamic_tokens", 1000)
            blocks = await recall(query, _conn, _memory, _config, top_k)
            result = inject_recall(blocks, max_tokens)
            if not result:
                return EMPTY_RECALL_FALLBACK
            # Synthesize if style != structured and provider available
            style = getattr(_config, "recall_synthesis_style", "structured")
            if style != "structured" and _synth_provider is not None:
                try:
                    from synthesis import synthesize_recall
                    synth_result = await synthesize_recall(result, style, _synth_provider)
                    result = synth_result.text
                except Exception:
                    log.warning("Tool recall synthesis failed, using raw", exc_info=True)
            return result
        except Exception:
            log.warning("Structured recall failed, falling back to vector", exc_info=True)

    # Fallback to direct vector search
    try:
        results = await _memory.search(query, top_k=top_k)
        if not results:
            return "No memory results found."
        output = []
        for r in results:
            source = r.get("source", "unknown")
            text = r.get("text", "")
            score = r.get("score", 0)
            output.append(f"[{source}] (score: {score:.3f})\n{text}")
        return "\n\n---\n\n".join(output)
    except Exception as e:
        return f"Error searching memory: {e}"


async def tool_memory_get(file_path: str, start_line: int = 0,
                          end_line: int = 50) -> str:
    """Retrieve a specific file snippet from memory by path and line range."""
    if _memory is None:
        return "Error: Memory not configured in this deployment. This tool is unavailable."
    try:
        return await _memory.get_file_snippet(file_path, start_line, end_line)
    except Exception as e:
        return f"Error retrieving memory: {e}"


TOOLS = [
    {
        "name": "memory_search",
        "description": (
            "Search long-term memory. Searches indexed workspace files "
            "(memory/*.md, MEMORY.md) plus structured facts, episodes, "
            "and open commitments extracted from past sessions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (keywords or natural language)"},
                "top_k": {"type": "integer", "description": "Max results to return (default: 10)", "default": 10},
            },
            "required": ["query"],
        },
        "function": tool_memory_search,
    },
    {
        "name": "memory_get",
        "description": (
            "Retrieve a file snippet from indexed memory by workspace-relative path. "
            "Paths are relative to the workspace root (e.g., 'memory/2026-02-23.md', 'MEMORY.md'). "
            "Use memory_search to find available file paths first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Workspace-relative path (e.g., 'memory/2026-02-23.md', 'MEMORY.md'). NOT an absolute path."},
                "start_line": {"type": "integer", "description": "Start line (0-based)", "default": 0},
                "end_line": {"type": "integer", "description": "End line", "default": 50},
            },
            "required": ["file_path"],
        },
        "function": tool_memory_get,
    },
]
