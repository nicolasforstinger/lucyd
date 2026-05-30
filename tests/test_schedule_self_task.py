"""schedule_self_task — schedule a future agent:self work turn via at."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


def _future_when(hours: int = 5) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours)).strftime(
        "%Y-%m-%d %H:%M",
    )


@pytest.mark.asyncio
async def test_schedule_self_task_rejects_past_time():
    from tools.reminder import configure, tool_schedule_self_task
    configure(http_auth_token="t", http_port=8100, user_timezone="UTC")
    past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    result = await tool_schedule_self_task(instruction="X", when=past)
    assert "Error" in result
    assert "future" in result.lower()


@pytest.mark.asyncio
async def test_schedule_self_task_rejects_beyond_one_year():
    from tools.reminder import configure, tool_schedule_self_task
    configure(http_auth_token="t", http_port=8100, user_timezone="UTC")
    far = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=400)).strftime("%Y-%m-%d %H:%M")
    result = await tool_schedule_self_task(instruction="X", when=far)
    assert "Error" in result
    assert "1 year" in result.lower()


@pytest.mark.asyncio
async def test_schedule_self_task_validates_instruction_non_empty():
    from tools.reminder import configure, tool_schedule_self_task
    configure(http_auth_token="t", http_port=8100, user_timezone="UTC")
    result = await tool_schedule_self_task(instruction="   ", when=_future_when())
    assert "Error" in result


@pytest.mark.asyncio
async def test_schedule_self_task_at_job_targets_agent_action():
    """Posts to /api/v1/agent/action with sender=self, scheduled absolutely."""
    from tools.reminder import configure, tool_schedule_self_task
    configure(http_auth_token="t", http_port=8100, user_timezone="UTC")

    captured_scripts: list[str] = []
    captured_cmds: list[str] = []

    async def fake_subprocess(cmd: str, **_: object):
        captured_cmds.append(cmd)
        if " -f " in cmd:
            script_path = cmd.split(" -f ", 1)[1].strip().strip("'\"")
            if Path(script_path).exists():
                captured_scripts.append(Path(script_path).read_text())
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b"job 1 at ..."))
        proc.returncode = 0
        return proc

    with patch("shutil.which", return_value="/usr/bin/at"), \
         patch("asyncio.create_subprocess_shell", side_effect=fake_subprocess):
        result = await tool_schedule_self_task(
            "Read /data/workspace/X.md, summarize, send_message the result.",
            when=_future_when(),
        )

    assert "Scheduled" in result
    assert any("at -t " in c for c in captured_cmds)
    assert len(captured_scripts) == 1
    script = captured_scripts[0]
    assert "/api/v1/agent/action" in script
    assert '"sender": "self"' in script
    assert "[Scheduled task]" in script
    assert "Read /data/workspace/X.md" in script
