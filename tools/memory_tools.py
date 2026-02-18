"""Memory tools — memory_search and memory_get.

Optional: only registered if memory DB is configured.
Delegates to memory.py for actual search logic.
"""

from __future__ import annotations

from typing import Any

# Set at daemon startup
_memory: Any = None


def set_memory(memory_interface: Any) -> None:
    global _memory
    _memory = memory_interface


async def tool_memory_search(query: str, top_k: int = 10) -> str:
    """Search long-term memory using keywords and/or semantic similarity."""
    if _memory is None:
        return "Error: Memory not configured"
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
        return "Error: Memory not configured"
    try:
        return await _memory.get_file_snippet(file_path, start_line, end_line)
    except Exception as e:
        return f"Error retrieving memory: {e}"


TOOLS = [
    {
        "name": "memory_search",
        "description": "Search long-term memory for relevant information. Uses keyword matching first, falls back to semantic similarity.",
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
        "description": "Retrieve a specific file snippet from memory by path and line range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path of the file in memory"},
                "start_line": {"type": "integer", "description": "Start line (0-based)", "default": 0},
                "end_line": {"type": "integer", "description": "End line", "default": 50},
            },
            "required": ["file_path"],
        },
        "function": tool_memory_get,
    },
]
