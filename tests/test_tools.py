"""Tests for tools/__init__.py — ToolRegistry error handling and tool contracts."""

import logging

import pytest

from tools import ToolRegistry, ToolSpec


class TestToolErrorHandling:
    """SEC-7: Generic tool error messages — no detail leakage."""

    @pytest.mark.asyncio
    async def test_tool_error_does_not_leak_details(self):
        """Tool error response includes exception type but not message content (paths, secrets)."""
        reg = ToolRegistry()

        def bad_tool():
            raise FileNotFoundError("/secret/path/to/file.db")

        reg.register(ToolSpec(name="bad", description="A bad tool", input_schema={"type": "object", "properties": {}}, function=bad_tool))
        result = await reg.execute("bad", {})
        assert "Error:" in result["text"]
        assert "Tool 'bad' failed (FileNotFoundError)" in result["text"]
        # Exception message must NOT leak — could contain paths, credentials
        assert "/secret/path" not in result["text"]

    @pytest.mark.asyncio
    async def test_tool_error_is_logged(self, caplog):
        """Detailed error should be logged for debugging."""
        reg = ToolRegistry()

        def exploding_tool():
            raise ValueError("detailed internal error info")

        reg.register(ToolSpec(name="explode", description="Exploding tool", input_schema={"type": "object", "properties": {}}, function=exploding_tool))
        with caplog.at_level(logging.ERROR, logger="tools"):
            await reg.execute("explode", {})
        assert "detailed internal error info" in caplog.text


# ─── ToolSpec talkers + ToolRegistry filter ─────────────────────


class TestToolSpecTalkers:
    """ToolSpec.talkers gates a tool to specific talker contexts."""

    def test_talkers_default_is_none(self):
        """A tool with no talkers field is available everywhere (None)."""
        spec = ToolSpec(
            name="any",
            description="test",
            input_schema={"type": "object", "properties": {}},
            function=lambda: "ok",
        )
        assert spec.talkers is None

    def test_talkers_explicit_frozenset(self):
        """A tool can declare talkers as a frozenset of Talker literals."""
        spec = ToolSpec(
            name="agent_only",
            description="test",
            input_schema={"type": "object", "properties": {}},
            function=lambda: "ok",
            talkers=frozenset({"agent"}),
        )
        assert spec.talkers == frozenset({"agent"})

    def test_get_schemas_for_talker_includes_unscoped_tools(self):
        """A tool with talkers=None appears in every talker's schema list."""
        reg = ToolRegistry()
        reg.register(ToolSpec(
            name="any",
            description="test",
            input_schema={"type": "object", "properties": {}},
            function=lambda: "ok",
            talkers=None,
        ))
        for talker in ("user", "operator", "system", "agent"):
            assert any(s["name"] == "any" for s in reg.get_schemas_for_talker(talker))

    def test_get_schemas_for_talker_filters_scoped_tools(self):
        """A tool with talkers={agent} only appears for talker='agent'."""
        reg = ToolRegistry()
        reg.register(ToolSpec(
            name="agent_only",
            description="test",
            input_schema={"type": "object", "properties": {}},
            function=lambda: "ok",
            talkers=frozenset({"agent"}),
        ))
        assert any(s["name"] == "agent_only" for s in reg.get_schemas_for_talker("agent"))
        for other in ("user", "operator", "system"):
            assert not any(s["name"] == "agent_only" for s in reg.get_schemas_for_talker(other))


class TestArgumentCoercion:
    """execute() coerces stringified scalars to their declared schema type.

    Weaker models emit integer/number/boolean params as JSON strings; the
    framework coerces at the boundary so handlers trusting their type
    annotations don't crash downstream (the commitment_update asyncpg int4
    DataError, dev-2026-05-25-001).
    """

    @staticmethod
    def _int_param_tool(received: dict[str, object]):
        """A tool whose schema declares an integer param; records what it got."""
        def fn(commitment_id, status):
            received["commitment_id"] = commitment_id
            received["status"] = status
            return "ok"
        return ToolSpec(
            name="commit",
            description="x",
            input_schema={
                "type": "object",
                "properties": {
                    "commitment_id": {"type": "integer"},
                    "status": {"type": "string"},
                },
                "required": ["commitment_id", "status"],
            },
            function=fn,
        )

    @pytest.mark.asyncio
    async def test_stringified_int_is_coerced_before_dispatch(self):
        """A JSON-string integer ('92') reaches the handler as int 92, not str."""
        reg = ToolRegistry()
        received: dict[str, object] = {}
        reg.register(self._int_param_tool(received))
        result = await reg.execute("commit", {"commitment_id": "92", "status": "done"})
        assert "Error:" not in result["text"]
        assert received["commitment_id"] == 92
        assert isinstance(received["commitment_id"], int)
        assert received["status"] == "done"

    @pytest.mark.asyncio
    async def test_already_int_passes_through_untouched(self):
        """An int that is already an int is left alone."""
        reg = ToolRegistry()
        received: dict[str, object] = {}
        reg.register(self._int_param_tool(received))
        await reg.execute("commit", {"commitment_id": 7, "status": "done"})
        assert received["commitment_id"] == 7
        assert isinstance(received["commitment_id"], int)

    @pytest.mark.asyncio
    async def test_uncoercible_int_returns_clean_error_not_crash(self):
        """A non-numeric string for an integer param fails closed with a clear message."""
        reg = ToolRegistry()
        received: dict[str, object] = {}
        reg.register(self._int_param_tool(received))
        result = await reg.execute("commit", {"commitment_id": "abc", "status": "done"})
        assert "Error: Invalid arguments for 'commit'" in result["text"]
        assert "commitment_id must be an integer" in result["text"]
        # handler must NOT have been called with the bad value
        assert "commitment_id" not in received

    @pytest.mark.asyncio
    async def test_stringified_number_is_coerced(self):
        """A JSON-string number ('1.5') reaches the handler as float."""
        reg = ToolRegistry()
        received: dict[str, object] = {}

        def fn(amount):
            received["amount"] = amount
            return "ok"

        reg.register(ToolSpec(
            name="num", description="x",
            input_schema={"type": "object", "properties": {"amount": {"type": "number"}}},
            function=fn,
        ))
        await reg.execute("num", {"amount": "1.5"})
        assert received["amount"] == 1.5
        assert isinstance(received["amount"], float)

    @pytest.mark.asyncio
    async def test_stringified_bool_is_coerced(self):
        """A JSON-string boolean ('true'/'false') reaches the handler as bool."""
        reg = ToolRegistry()
        received: dict[str, object] = {}

        def fn(flag):
            received["flag"] = flag
            return "ok"

        reg.register(ToolSpec(
            name="flag", description="x",
            input_schema={"type": "object", "properties": {"flag": {"type": "boolean"}}},
            function=fn,
        ))
        await reg.execute("flag", {"flag": "false"})
        assert received["flag"] is False

    @pytest.mark.asyncio
    async def test_string_param_is_not_coerced(self):
        """A string-typed param keeps its string value even when numeric-looking."""
        reg = ToolRegistry()
        received: dict[str, object] = {}

        def fn(code):
            received["code"] = code
            return "ok"

        reg.register(ToolSpec(
            name="str_tool", description="x",
            input_schema={"type": "object", "properties": {"code": {"type": "string"}}},
            function=fn,
        ))
        await reg.execute("str_tool", {"code": "007"})
        assert received["code"] == "007"
        assert isinstance(received["code"], str)
