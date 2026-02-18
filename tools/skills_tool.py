"""Skill loading tool — load_skill.

Agent calls this to load a skill's full content on demand.
"""

from __future__ import annotations

from typing import Any

# Set at daemon startup
_skill_loader: Any = None


def set_skill_loader(loader: Any) -> None:
    global _skill_loader
    _skill_loader = loader


def tool_load_skill(name: str) -> str:
    """Load a skill's full content by name."""
    if _skill_loader is None:
        return "Error: Skill loader not initialized"
    try:
        skill = _skill_loader.get_skill(name)
        if skill is None:
            available = _skill_loader.list_skill_names()
            return f"Error: Skill '{name}' not found. Available: {', '.join(available)}"
        return skill["body"]
    except Exception as e:
        return f"Error loading skill: {e}"


TOOLS = [
    {
        "name": "load_skill",
        "description": "Load a skill's full instructions by name. Use the skills index to see available skills.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name to load"},
            },
            "required": ["name"],
        },
        "function": tool_load_skill,
    },
]
