"""Tests for tools/__init__.py — ToolRegistry."""

import pytest

from tools import ToolRegistry

# ─── Registration ────────────────────────────────────────────────

class TestRegister:
    def test_register_single(self):
        reg = ToolRegistry()
        reg.register("ping", "Ping tool", {"type": "object"}, lambda: "pong")
        assert "ping" in reg.tool_names

    def test_register_many(self):
        reg = ToolRegistry()
        reg.register_many([
            {"name": "a", "description": "A", "input_schema": {}, "function": lambda: "a"},
            {"name": "b", "description": "B", "input_schema": {}, "function": lambda: "b"},
        ])
        assert set(reg.tool_names) == {"a", "b"}

    def test_overwrite_on_same_name(self):
        reg = ToolRegistry()
        reg.register("x", "first", {}, lambda: "1")
        reg.register("x", "second", {}, lambda: "2")
        assert len(reg.tool_names) == 1
        schemas = reg.get_schemas()
        assert schemas[0]["description"] == "second"


# ─── Schemas ─────────────────────────────────────────────────────

class TestSchemas:
    def test_get_schemas_excludes_function(self, tool_registry):
        schemas = tool_registry.get_schemas()
        for s in schemas:
            assert "function" not in s
            assert "name" in s
            assert "description" in s
            assert "input_schema" in s

    def test_empty_registry_empty_list(self):
        reg = ToolRegistry()
        assert reg.get_schemas() == []

    def test_get_brief_descriptions_returns_tuples(self, tool_registry):
        descs = tool_registry.get_brief_descriptions()
        assert len(descs) == 2
        for name, desc in descs:
            assert isinstance(name, str)
            assert isinstance(desc, str)


# ─── Execute ─────────────────────────────────────────────────────

class TestExecute:
    @pytest.mark.asyncio
    async def test_sync_function(self, tool_registry):
        result = await tool_registry.execute("sync_echo", {"text": "hello"})
        assert result == "sync:hello"

    @pytest.mark.asyncio
    async def test_async_function(self, tool_registry):
        result = await tool_registry.execute("async_echo", {"text": "world"})
        assert result == "async:world"

    @pytest.mark.asyncio
    async def test_unknown_tool_error(self, tool_registry):
        result = await tool_registry.execute("nonexistent", {})
        assert "Unknown tool" in result

    @pytest.mark.asyncio
    async def test_wrong_args_type_error(self, tool_registry):
        result = await tool_registry.execute("sync_echo", {"bad_arg": 1})
        assert "Error:" in result

    @pytest.mark.asyncio
    async def test_exception_isolated(self):
        def explode():
            raise RuntimeError("boom")
        reg = ToolRegistry()
        reg.register("bomb", "explodes", {"type": "object"}, explode)
        result = await reg.execute("bomb", {})
        assert "Error:" in result
        assert "Tool 'bomb' execution failed" in result

    @pytest.mark.asyncio
    async def test_truncation_at_limit(self):
        reg = ToolRegistry(truncation_limit=50)
        reg.register("big", "returns big output", {}, lambda: "a" * 200)
        result = await reg.execute("big", {})
        assert len(result) < 200
        assert result.endswith("[truncated at 50 chars]")

    @pytest.mark.asyncio
    async def test_truncation_marker_text(self):
        reg = ToolRegistry(truncation_limit=10)
        reg.register("big", "big", {}, lambda: "x" * 100)
        result = await reg.execute("big", {})
        assert "[truncated at 10 chars]" in result

    @pytest.mark.asyncio
    async def test_non_string_result_converted(self):
        reg = ToolRegistry()
        reg.register("num", "returns int", {}, lambda: 42)
        result = await reg.execute("num", {})
        assert result == "42"
