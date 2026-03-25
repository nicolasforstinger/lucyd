"""Tests for tools/__init__.py — ToolRegistry."""

import json

import pytest

from tools import ToolRegistry, _smart_truncate, _truncate_json

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
        assert result["text"] == "sync:hello"

    @pytest.mark.asyncio
    async def test_async_function(self, tool_registry):
        result = await tool_registry.execute("async_echo", {"text": "world"})
        assert result["text"] == "async:world"

    @pytest.mark.asyncio
    async def test_unknown_tool_error(self, tool_registry):
        result = await tool_registry.execute("nonexistent", {})
        assert "not available" in result["text"]
        assert "Available tools:" in result["text"]

    @pytest.mark.asyncio
    async def test_wrong_args_type_error(self, tool_registry):
        result = await tool_registry.execute("sync_echo", {"bad_arg": 1})
        assert "Error:" in result["text"]

    @pytest.mark.asyncio
    async def test_exception_isolated(self):
        def explode():
            raise RuntimeError("boom")
        reg = ToolRegistry()
        reg.register("bomb", "explodes", {"type": "object"}, explode)
        result = await reg.execute("bomb", {})
        assert "Error:" in result["text"]
        assert "Tool 'bomb' failed (RuntimeError)" in result["text"]

    @pytest.mark.asyncio
    async def test_truncation_at_limit(self):
        reg = ToolRegistry(truncation_limit=50)
        reg.register("big", "returns big output", {}, lambda: "a" * 200)
        result = await reg.execute("big", {})
        assert len(result["text"]) < 200
        assert "[truncated" in result["text"]

    @pytest.mark.asyncio
    async def test_truncation_marker_text(self):
        reg = ToolRegistry(truncation_limit=10)
        reg.register("big", "big", {}, lambda: "x" * 100)
        result = await reg.execute("big", {})
        assert "[truncated" in result["text"]

    @pytest.mark.asyncio
    async def test_non_string_result_converted(self):
        reg = ToolRegistry()
        reg.register("num", "returns int", {}, lambda: 42)
        result = await reg.execute("num", {})
        assert result["text"] == "42"

    @pytest.mark.asyncio
    async def test_per_tool_max_output(self):
        reg = ToolRegistry(truncation_limit=1000)
        reg.register("small", "small limit", {}, lambda: "x" * 200, max_output=50)
        result = await reg.execute("small", {})
        assert "[truncated" in result["text"]
        # Should use per-tool limit (50), not registry default (1000)
        assert len(result["text"]) < 200


# ─── Smart Truncation ────────────────────────────────────────────


class TestSmartTruncate:
    def test_no_truncation_needed(self):
        text = "short text"
        assert _smart_truncate(text, 100) == text

    def test_returns_exact_input_when_under_limit(self):
        text = "x" * 99
        assert _smart_truncate(text, 100) == text

    def test_returns_exact_input_at_limit(self):
        text = "x" * 100
        assert _smart_truncate(text, 100) == text

    def test_truncates_when_over_limit(self):
        text = "x" * 101
        result = _smart_truncate(text, 100)
        assert result != text
        assert "[truncated" in result

    def test_plain_text_head_tail_split(self):
        text = "HEAD" * 100 + "TAIL" * 100
        result = _smart_truncate(text, 400)
        assert "HEAD" in result
        assert "TAIL" in result
        assert "[...truncated" in result

    def test_head_tail_head_is_70_percent(self):
        text = "a" * 2000
        result = _smart_truncate(text, 500)
        # Head portion should be ~70% of usable space
        parts = result.split("[...truncated")
        head = parts[0]
        assert len(head) > 200  # significantly more than half

    def test_head_tail_skipped_count_accurate(self):
        text = "x" * 1000
        result = _smart_truncate(text, 500)
        # The truncation marker should mention skipped chars
        assert "1,000" in result  # total chars

    def test_tight_limit_falls_back_to_head_only(self):
        text = "x" * 500
        result = _smart_truncate(text, 220)
        # Usable < 200 → head-only mode
        assert "[truncated — showing" in result
        assert "500" in result

    def test_head_only_cuts_at_newline(self):
        lines = "\n".join(f"line-{i:03d}" for i in range(100))
        result = _smart_truncate(lines, 220)
        # Should cut at a newline boundary, not mid-word
        assert not result.split("\n[truncated")[0].endswith("e-")

    def test_json_array_truncation(self):
        data = list(range(100))
        text = json.dumps(data)
        result = _smart_truncate(text, 100)
        assert "[truncated" in result
        assert "100 items" in result
        json_part = result[:result.index("\n[truncated")]
        parsed = json.loads(json_part)
        assert isinstance(parsed, list)
        assert len(parsed) < 100

    def test_json_array_preserves_all_when_fits(self):
        data = [1, 2, 3]
        text = json.dumps(data)
        result = _smart_truncate(text, 1000)
        assert result == text

    def test_json_object_compact(self):
        data = {"key": "x" * 500}
        text = json.dumps(data, indent=2)
        result = _smart_truncate(text, 100)
        assert "[truncated" in result

    def test_truncation_marker_includes_counts(self):
        text = "a" * 500
        result = _smart_truncate(text, 400)
        assert "500" in result

    def test_invalid_json_falls_through(self):
        text = "[this is not json" + "x" * 500
        result = _smart_truncate(text, 100)
        assert "[truncated" in result
        # Should NOT crash on bad JSON

    def test_whitespace_stripped_before_json_detect(self):
        data = [1, 2, 3, 4, 5]
        text = "  " + json.dumps(data) + "  "
        text += "x" * 500
        # starts with whitespace+[ — should try JSON path
        result = _smart_truncate(text, 50)
        assert "[truncated" in result

    def test_tool_name_in_log(self, caplog):
        import logging
        text = "x" * 500
        with caplog.at_level(logging.WARNING):
            _smart_truncate(text, 100, tool_name="my_tool")
        assert "my_tool" in caplog.text

    def test_empty_tool_name_uses_question_mark(self, caplog):
        import logging
        text = "x" * 500
        with caplog.at_level(logging.WARNING):
            _smart_truncate(text, 100)
        assert "?" in caplog.text


class TestTruncateJson:
    def test_list_binary_search_returns_subset(self):
        data = list(range(50))
        result = _truncate_json(data, 80)
        assert "[truncated" in result
        assert "50 items" in result
        json_part = result[:result.index("\n[truncated")]
        parsed = json.loads(json_part)
        assert len(parsed) < 50
        assert len(parsed) > 0

    def test_list_all_fit(self):
        data = [1, 2, 3]
        result = _truncate_json(data, 1000)
        assert json.loads(result) == data
        assert "[truncated" not in result

    def test_list_marker_shows_item_counts(self):
        data = list(range(20))
        result = _truncate_json(data, 50)
        assert "20 items" in result
        json_part = result[:result.index("\n[truncated")]
        shown = json.loads(json_part)
        assert f"showing {len(shown)} of 20 items" in result

    def test_list_single_item_fits(self):
        data = [42]
        result = _truncate_json(data, 1000)
        assert result == "[42]"

    def test_list_empty(self):
        result = _truncate_json([], 100)
        assert result == "[]"

    def test_list_no_items_fit(self):
        data = [{"big": "x" * 200}]
        result = _truncate_json(data, 10)
        # Even first item doesn't fit → should return "[]" with marker
        assert "truncated" in result or result == "[]"

    def test_dict_compact_fits(self):
        data = {"a": 1, "b": 2}
        result = _truncate_json(data, 1000)
        assert json.loads(result) == data

    def test_dict_truncated_includes_limit(self):
        data = {"key": "x" * 500}
        result = _truncate_json(data, 50)
        assert "[truncated" in result
        assert "50" in result  # limit value in marker

    def test_dict_truncated_total_accurate(self):
        data = {"key": "x" * 500}
        compact = json.dumps(data, ensure_ascii=False)
        result = _truncate_json(data, 50)
        assert str(len(compact)) in result  # total chars

    def test_scalar_string(self):
        result = _truncate_json("hello", 1000)
        assert json.loads(result) == "hello"

    def test_scalar_number(self):
        result = _truncate_json(42, 1000)
        assert result == "42"

    def test_scalar_truncated(self):
        long_str = "x" * 500
        result = _truncate_json(long_str, 50)
        assert "[truncated" in result
        assert "502" in result  # total length includes JSON quotes

    def test_scalar_fits(self):
        result = _truncate_json(True, 1000)
        assert result == "true"

    def test_none_value(self):
        result = _truncate_json(None, 1000)
        assert result == "null"
