"""Tests for tools/__init__.py — ToolRegistry error handling."""

import logging

import pytest

from tools import ToolRegistry


class TestToolErrorHandling:
    """SEC-7: Generic tool error messages — no detail leakage."""

    @pytest.mark.asyncio
    async def test_tool_error_does_not_leak_details(self):
        """Tool error response should not contain exception type or path."""
        reg = ToolRegistry()

        def bad_tool():
            raise FileNotFoundError("/secret/path/to/file.db")

        reg.register("bad", "A bad tool", {"type": "object", "properties": {}}, bad_tool)
        result = await reg.execute("bad", {})
        assert "Error:" in result
        assert "Tool 'bad' execution failed" in result
        assert "FileNotFoundError" not in result
        assert "/secret/path" not in result

    @pytest.mark.asyncio
    async def test_tool_error_is_logged(self, caplog):
        """Detailed error should be logged for debugging."""
        reg = ToolRegistry()

        def exploding_tool():
            raise ValueError("detailed internal error info")

        reg.register("explode", "Exploding tool", {"type": "object", "properties": {}}, exploding_tool)
        with caplog.at_level(logging.ERROR, logger="tools"):
            await reg.execute("explode", {})
        assert "detailed internal error info" in caplog.text
