"""Pipeline filters tools by ctx.talker so gated tools (e.g. send_message)
only appear in the schema list for the right talker context."""
from __future__ import annotations

from tools import ToolRegistry, ToolSpec


def _make_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(ToolSpec(
        name="universal",
        description="works everywhere",
        input_schema={"type": "object", "properties": {}},
        function=lambda: "ok",
    ))
    reg.register(ToolSpec(
        name="agent_only",
        description="agent self only",
        input_schema={"type": "object", "properties": {}},
        function=lambda: "ok",
        talkers=frozenset({"agent"}),
    ))
    return reg


def test_user_talker_excludes_agent_only_tool() -> None:
    reg = _make_registry()
    schemas = reg.get_schemas_for_talker("user")
    names = {s["name"] for s in schemas}
    assert "universal" in names
    assert "agent_only" not in names


def test_agent_talker_includes_agent_only_tool() -> None:
    reg = _make_registry()
    schemas = reg.get_schemas_for_talker("agent")
    names = {s["name"] for s in schemas}
    assert "universal" in names
    assert "agent_only" in names
