"""remind_user + scheduling control — absolute-time scheduling via at."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


def _future_when(hours: int = 5) -> str:
    """An absolute ISO datetime `hours` ahead, in UTC (configure uses UTC)."""
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours)).strftime(
        "%Y-%m-%d %H:%M",
    )


@pytest.mark.asyncio
async def test_remind_user_rejects_past_time():
    from tools.reminder import configure, tool_remind_user
    configure(http_auth_token="t", http_port=8100, user_timezone="UTC")
    past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    result = await tool_remind_user(message="x", when=past)
    assert "Error" in result
    assert "future" in result.lower()


@pytest.mark.asyncio
async def test_remind_user_rejects_beyond_one_year():
    from tools.reminder import configure, tool_remind_user
    configure(http_auth_token="t", http_port=8100, user_timezone="UTC")
    far = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=400)).strftime("%Y-%m-%d %H:%M")
    result = await tool_remind_user(message="x", when=far)
    assert "Error" in result
    assert "1 year" in result.lower()


@pytest.mark.asyncio
async def test_remind_user_rejects_unparseable_when():
    from tools.reminder import configure, tool_remind_user
    configure(http_auth_token="t", http_port=8100, user_timezone="UTC")
    result = await tool_remind_user(message="x", when="next tuesday-ish")
    assert "Error" in result
    assert "ISO" in result


@pytest.mark.asyncio
async def test_remind_user_validates_message_non_empty():
    from tools.reminder import configure, tool_remind_user
    configure(http_auth_token="t", http_port=8100, user_timezone="UTC")
    result = await tool_remind_user(message="   ", when=_future_when())
    assert "Error" in result
    assert "empty" in result.lower()


@pytest.mark.asyncio
async def test_remind_user_at_job_uses_absolute_at_t_and_reminder_marker():
    """Fires as an agent:self [Reminder] turn, scheduled with absolute `at -t`
    (no relative `now + N minutes` math)."""
    from tools.reminder import configure, tool_remind_user
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
        result = await tool_remind_user("don't forget eggs", when=_future_when())

    assert "Scheduled" in result
    # absolute scheduling — at -t, never relative "now +"
    assert any("at -t " in c for c in captured_cmds)
    assert not any("now +" in c for c in captured_cmds)
    assert len(captured_scripts) == 1
    script = captured_scripts[0]
    assert "/api/v1/agent/action" in script
    assert "/api/v1/outbound/send" not in script
    assert "[Reminder]" in script
    assert '"sender": "self"' in script
    assert "forget eggs" in script
    assert "Authorization: Bearer t" in script


@pytest.mark.asyncio
async def test_cancel_scheduled_runs_atrm():
    from tools.reminder import tool_cancel_scheduled
    cmds: list[str] = []

    async def fake_subprocess(cmd: str, **_: object):
        cmds.append(cmd)
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        return proc

    with patch("shutil.which", return_value="/usr/bin/atrm"), \
         patch("asyncio.create_subprocess_shell", side_effect=fake_subprocess):
        result = await tool_cancel_scheduled(job_id=24)
    assert "Cancelled" in result
    assert any("atrm 24" in c for c in cmds)


@pytest.mark.asyncio
async def test_cancel_scheduled_reports_failure():
    from tools.reminder import tool_cancel_scheduled

    async def fake_subprocess(cmd: str, **_: object):
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b"Cannot find jobid 99"))
        proc.returncode = 1
        return proc

    with patch("shutil.which", return_value="/usr/bin/atrm"), \
         patch("asyncio.create_subprocess_shell", side_effect=fake_subprocess):
        result = await tool_cancel_scheduled(job_id=99)
    assert "Error" in result
    assert "99" in result


@pytest.mark.asyncio
async def test_list_scheduled_renders_id_time_and_intent():
    from tools.reminder import tool_list_scheduled
    at_l = "25\tTue May 26 12:01:00 2026 a lucyd\n"
    at_c = (
        '#!/bin/sh\ncurl -s -X POST http://localhost:8100/api/v1/agent/action '
        '-d \'{"message": "[Reminder] water the plants", "sender": "self"}\'\n'
    )

    async def fake_subprocess(cmd: str, **_: object):
        proc = AsyncMock()
        if cmd.strip() == "at -l":
            proc.communicate = AsyncMock(return_value=(at_l.encode(), b""))
        else:  # at -c <id>
            proc.communicate = AsyncMock(return_value=(at_c.encode(), b""))
        proc.returncode = 0
        return proc

    with patch("shutil.which", return_value="/usr/bin/at"), \
         patch("asyncio.create_subprocess_shell", side_effect=fake_subprocess):
        result = await tool_list_scheduled()
    assert "25" in result
    assert "Tue May 26 12:01:00 2026" in result
    assert "water the plants" in result


@pytest.mark.asyncio
async def test_list_scheduled_empty():
    from tools.reminder import tool_list_scheduled

    async def fake_subprocess(cmd: str, **_: object):
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        return proc

    with patch("shutil.which", return_value="/usr/bin/at"), \
         patch("asyncio.create_subprocess_shell", side_effect=fake_subprocess):
        result = await tool_list_scheduled()
    assert "No scheduled jobs" in result


def _within_year_when() -> str:
    """A within-1-year future date at 14:00 (avoids the 1-year-cap rejection)."""
    d = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30)).strftime("%Y-%m-%d")
    return f"{d} 14:00"


def test_resolve_when_does_the_tz_math_correctly():
    """The framework — not the model — converts the user-tz wall-clock to UTC,
    DST-correct, matching a direct zoneinfo computation."""
    from zoneinfo import ZoneInfo

    from tools.reminder import _resolve_when, configure
    configure(http_auth_token="t", http_port=8100, user_timezone="Europe/Vienna")
    when_str = _within_year_when()
    stamp, display, err = _resolve_when(when_str)
    assert err == ""
    expected = (
        dt.datetime.fromisoformat(when_str)
        .replace(tzinfo=ZoneInfo("Europe/Vienna"))
        .astimezone(dt.timezone.utc)
        .strftime("%Y%m%d%H%M")
    )
    assert stamp == expected
    assert "14:00" in display  # echoes the local wall-clock for self-check


def test_resolve_when_naive_defaults_to_user_tz_not_utc():
    from tools.reminder import _resolve_when, configure
    when_str = _within_year_when()
    configure(http_auth_token="t", http_port=8100, user_timezone="Europe/Vienna")
    stamp_vienna, _, e1 = _resolve_when(when_str)
    configure(http_auth_token="t", http_port=8100, user_timezone="UTC")
    stamp_utc, _, e2 = _resolve_when(when_str)
    assert e1 == "" and e2 == ""
    # Same wall-clock string resolves to different UTC stamps per tz.
    assert stamp_vienna != stamp_utc
