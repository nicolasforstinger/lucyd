"""Tests for tools/__init__.py — ToolRegistry error handling and tool contracts."""

import json
import logging
from unittest.mock import AsyncMock, patch

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


# ─── Reminder tool ──────────────────────────────────────────────


class TestReminderPayload:
    """Reminder curl payload targets /agent/action with sender=self."""

    @pytest.mark.asyncio
    async def test_reminder_payload_targets_agent_action(self) -> None:
        """Reminder posts to /api/v1/agent/action with sender 'self'."""
        from tools.reminder import tool_reminder

        captured_scripts: list[str] = []

        async def fake_subprocess(cmd: str, **_: object) -> AsyncMock:
            if "at -f" in cmd:
                script_path = cmd.split("at -f ")[1].split(" now")[0].strip("'\"")
                from pathlib import Path
                if Path(script_path).exists():
                    captured_scripts.append(Path(script_path).read_text())
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"", b"job 1 at ..."))
            proc.returncode = 0
            return proc

        with patch("shutil.which", return_value="/usr/bin/at"), \
             patch("asyncio.create_subprocess_shell", side_effect=fake_subprocess):
            result = await tool_reminder("check logs", minutes=10)

        assert "Reminder set" in result
        assert len(captured_scripts) == 1
        script = captured_scripts[0]
        assert "/api/v1/agent/action" in script
        assert '"sender": "self"' in script
        assert "check logs" in script

    @pytest.mark.asyncio
    async def test_reminder_handles_single_quotes_in_message(self) -> None:
        """Single quotes in reminder message don't break shell quoting."""
        from tools.reminder import tool_reminder

        captured_scripts: list[str] = []

        async def fake_subprocess(cmd: str, **_: object) -> AsyncMock:
            if "at -f" in cmd:
                script_path = cmd.split("at -f ")[1].split(" now")[0].strip("'\"")
                from pathlib import Path
                if Path(script_path).exists():
                    captured_scripts.append(Path(script_path).read_text())
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"", b"job 1 at ..."))
            proc.returncode = 0
            return proc

        with patch("shutil.which", return_value="/usr/bin/at"), \
             patch("asyncio.create_subprocess_shell", side_effect=fake_subprocess):
            result = await tool_reminder("it's time to check", minutes=5)

        assert "Reminder set" in result
        assert len(captured_scripts) == 1
        script = captured_scripts[0]
        assert "time to check" in script
        assert '"sender": "self"' in script
