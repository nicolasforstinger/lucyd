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
        assert "not available" in result
        assert "Available tools:" in result

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
        assert "Tool 'bomb' failed (RuntimeError)" in result

    @pytest.mark.asyncio
    async def test_truncation_at_limit(self):
        reg = ToolRegistry(truncation_limit=50)
        reg.register("big", "returns big output", {}, lambda: "a" * 200)
        result = await reg.execute("big", {})
        assert len(result) < 200
        assert "[truncated" in result

    @pytest.mark.asyncio
    async def test_truncation_marker_text(self):
        reg = ToolRegistry(truncation_limit=10)
        reg.register("big", "big", {}, lambda: "x" * 100)
        result = await reg.execute("big", {})
        assert "[truncated" in result

    @pytest.mark.asyncio
    async def test_non_string_result_converted(self):
        reg = ToolRegistry()
        reg.register("num", "returns int", {}, lambda: 42)
        result = await reg.execute("num", {})
        assert result == "42"

    @pytest.mark.asyncio
    async def test_per_tool_max_output(self):
        reg = ToolRegistry(truncation_limit=1000)
        reg.register("small", "small limit", {}, lambda: "x" * 200, max_output=50)
        result = await reg.execute("small", {})
        assert "[truncated" in result
        # Should use per-tool limit (50), not registry default (1000)
        assert len(result) < 200


# ─── Smart Truncation ────────────────────────────────────────────

import json
from tools import _smart_truncate, _truncate_json


class TestSmartTruncate:
    def test_no_truncation_needed(self):
        text = "short text"
        assert _smart_truncate(text, 100) == text

    def test_plain_text_line_boundary(self):
        lines = "\n".join(f"line {i}" for i in range(100))
        result = _smart_truncate(lines, 50)
        assert "[truncated" in result
        # Should not cut mid-line
        for line in result.split("\n"):
            if "[truncated" in line:
                continue
            assert line.startswith("line ")

    def test_json_array_truncation(self):
        data = list(range(100))
        text = json.dumps(data)
        result = _smart_truncate(text, 100)
        assert "[truncated" in result
        assert "100 items" in result
        # The truncated portion should be valid JSON up to the marker
        json_part = result[:result.index("\n[truncated")]
        parsed = json.loads(json_part)
        assert isinstance(parsed, list)
        assert len(parsed) < 100

    def test_json_array_preserves_all_when_fits(self):
        data = [1, 2, 3]
        text = json.dumps(data)
        result = _smart_truncate(text, 1000)
        assert result == text  # No truncation needed

    def test_json_object_compact(self):
        data = {"key": "x" * 500}
        text = json.dumps(data, indent=2)
        result = _smart_truncate(text, 100)
        assert "[truncated" in result

    def test_truncation_marker_includes_counts(self):
        text = "a" * 500
        result = _smart_truncate(text, 100)
        assert "500" in result  # total chars mentioned


class TestTruncateJson:
    def test_list_binary_search(self):
        data = list(range(50))
        result = _truncate_json(data, 80)
        assert "[truncated" in result
        assert "50 items" in result

    def test_list_all_fit(self):
        data = [1, 2, 3]
        result = _truncate_json(data, 1000)
        assert json.loads(result) == data

    def test_dict_compact_fits(self):
        data = {"a": 1, "b": 2}
        result = _truncate_json(data, 1000)
        assert json.loads(result) == data

    def test_dict_truncated(self):
        data = {"key": "x" * 500}
        result = _truncate_json(data, 50)
        assert "[truncated" in result
