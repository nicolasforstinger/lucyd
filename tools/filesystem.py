"""File operation tools — read, write, edit."""

from __future__ import annotations

import os
from pathlib import Path

# Allowed path prefixes — set at startup via configure()
_PATH_ALLOW: list[str] = []


def configure(allowed_paths: list[str] | None = None) -> None:
    global _PATH_ALLOW
    if allowed_paths is not None:
        _PATH_ALLOW = allowed_paths


def _check_path(file_path: str) -> str | None:
    """Validate file path against allowlist. Returns error or None if OK."""
    try:
        resolved = str(Path(file_path).expanduser().resolve())
    except Exception:
        return f"Error: Invalid path: {file_path}"
    if not _PATH_ALLOW:
        return "Error: No allowed paths configured — filesystem access denied"
    for prefix in _PATH_ALLOW:
        if resolved == prefix or resolved.startswith(prefix + os.sep):
            return None
    return f"Error: Path not allowed: {file_path} (allowed prefixes: {', '.join(_PATH_ALLOW)})"


def tool_read(file_path: str, offset: int = 0, limit: int = 2000) -> str:
    """Read a file with optional offset and line limit."""
    err = _check_path(file_path)
    if err:
        return err
    p = Path(file_path).expanduser()
    if not p.exists():
        return f"Error: File not found: {file_path}"
    if not p.is_file():
        return f"Error: Not a file: {file_path}"
    try:
        with open(p, encoding="utf-8") as f:
            lines = f.readlines()
    except UnicodeDecodeError:
        return f"Error: Cannot read binary file: {file_path}"
    except PermissionError:
        return f"Error: Permission denied: {file_path}"

    total = len(lines)
    selected = lines[offset:offset + limit]
    result = ""
    for i, line in enumerate(selected, start=offset + 1):
        # Truncate very long lines
        if len(line) > 2000:
            line = line[:2000] + "...\n"
        result += f"{i:>6}\t{line}"

    if offset + limit < total:
        result += f"\n[... {total - offset - limit} more lines]"

    return result


def tool_write(file_path: str, content: str) -> str:
    """Write content to a file, creating directories as needed."""
    err = _check_path(file_path)
    if err:
        return err
    p = Path(file_path).expanduser()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Written {len(content)} chars to {file_path}"
    except PermissionError:
        return f"Error: Permission denied: {file_path}"


def tool_edit(file_path: str, old_string: str, new_string: str,
              replace_all: bool = False) -> str:
    """Edit a file by exact string replacement."""
    err = _check_path(file_path)
    if err:
        return err
    p = Path(file_path).expanduser()
    if not p.exists():
        return f"Error: File not found: {file_path}"
    try:
        with open(p, encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        return f"Error: Cannot read binary file: {file_path}"

    if old_string not in content:
        return f"Error: old_string not found in {file_path}"

    if not replace_all:
        count = content.count(old_string)
        if count > 1:
            return f"Error: old_string found {count} times in {file_path}. Use replace_all=true or provide more context."
        content = content.replace(old_string, new_string, 1)
    else:
        count = content.count(old_string)
        content = content.replace(old_string, new_string)

    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
    except PermissionError:
        return f"Error: Permission denied: {file_path}"

    if replace_all:
        return f"Replaced {count} occurrences in {file_path}"
    return f"Edited {file_path}"


TOOLS = [
    {
        "name": "read",
        "description": "Read a file. Returns numbered lines. Use offset/limit for large files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to the file"},
                "offset": {"type": "integer", "description": "Line offset (0-based)", "default": 0},
                "limit": {"type": "integer", "description": "Max lines to read", "default": 2000},
            },
            "required": ["file_path"],
        },
        "function": tool_read,
    },
    {
        "name": "write",
        "description": "Write content to a file. Creates directories as needed. Overwrites existing files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to the file"},
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["file_path", "content"],
        },
        "function": tool_write,
    },
    {
        "name": "edit",
        "description": "Edit a file by exact string replacement. old_string must be unique unless replace_all is true.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to the file"},
                "old_string": {"type": "string", "description": "Exact text to find"},
                "new_string": {"type": "string", "description": "Replacement text"},
                "replace_all": {"type": "boolean", "description": "Replace all occurrences", "default": False},
            },
            "required": ["file_path", "old_string", "new_string"],
        },
        "function": tool_edit,
    },
]
