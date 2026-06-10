"""Tests for the weekly user-session auto-reset.

Covers operations.handle_session_reset: the disabled / no-session /
waiting-for-diary / already-reset-this-week / user-active gates and the
lock-guarded close path. Boundaries mocked: the asyncpg pool (open-session
lookup + idle query) and the SessionManager (close_session). The filesystem is
real (tmp_path) for the diary-presence gate; the session lock is a real
asyncio.Lock per key.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

import operations as ops
from config import Config


def _config(workspace: Path, *, enabled: bool = True,
            idle_minutes: int = 60) -> Config:
    """Minimal Config with the [session.auto_reset] section and a tmp workspace."""
    return Config({
        "agent": {"name": "Lucy", "workspace": str(workspace)},
        "user": {"name": "Nicolas", "timezone": "Europe/Vienna"},
        "models": {"primary": {
            "provider": "anthropic", "model": "m",
            "cost_per_mtok": [1.0, 1.0, 1.0, 1.0],
        }},
        "session": {"auto_reset": {
            "enabled": enabled, "idle_minutes": idle_minutes,
        }},
    })


def _lock_factory() -> Any:
    """get_session_lock stand-in: a real asyncio.Lock per key."""
    locks: dict[str, asyncio.Lock] = {}

    def factory(key: str) -> asyncio.Lock:
        return locks.setdefault(key, asyncio.Lock())

    return factory


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    (ws / "memory").mkdir(parents=True)
    return ws


def _write_today_diary(workspace: Path) -> None:
    """Stand in for the daily diary maintenance having run today."""
    today = time.strftime("%Y-%m-%d")
    (workspace / "memory" / f"{today}.md").write_text("# diary")


def _pool(*, session_row: dict[str, Any] | None,
          last_user_epoch: float | None = None) -> AsyncMock:
    """Pool stub: fetchrow → the open session, fetchval → last-user-message epoch."""
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=session_row)
    pool.fetchval = AsyncMock(return_value=last_user_epoch)
    return pool


def _prior_week_session() -> dict[str, Any]:
    """An open session created well before the current week (9 days ago)."""
    created = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=9)
    return {"id": "sess-old", "created_at": created}


@pytest.mark.asyncio
async def test_disabled_does_not_reset(workspace: Path) -> None:
    mgr = AsyncMock()
    result = await ops.handle_session_reset(
        _config(workspace, enabled=False), mgr,
        _pool(session_row=_prior_week_session()), _lock_factory(),
    )
    assert result["outcome"] == "disabled"
    mgr.close_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_open_session(workspace: Path) -> None:
    mgr = AsyncMock()
    result = await ops.handle_session_reset(
        _config(workspace), mgr, _pool(session_row=None), _lock_factory(),
    )
    assert result["outcome"] == "no_session"
    mgr.close_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_waits_until_diary_written(workspace: Path) -> None:
    """No diary for today yet → hold; continuity must be captured first."""
    mgr = AsyncMock()
    result = await ops.handle_session_reset(
        _config(workspace), mgr,
        _pool(session_row=_prior_week_session()), _lock_factory(),
    )
    assert result["outcome"] == "waiting_for_diary"
    mgr.close_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_opened_this_week_is_left_alone(workspace: Path) -> None:
    """A session already opened this week → idempotent skip across retries."""
    _write_today_diary(workspace)
    mgr = AsyncMock()
    fresh = {"id": "sess-new",
             "created_at": _dt.datetime.now(_dt.timezone.utc)}
    result = await ops.handle_session_reset(
        _config(workspace), mgr, _pool(session_row=fresh), _lock_factory(),
    )
    assert result["outcome"] == "already_reset_this_week"
    mgr.close_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_user_is_never_cut(workspace: Path) -> None:
    """Recent user message → never reset mid-conversation."""
    _write_today_diary(workspace)
    mgr = AsyncMock()
    recent = (_dt.datetime.now(_dt.timezone.utc)
              - _dt.timedelta(minutes=5)).timestamp()
    result = await ops.handle_session_reset(
        _config(workspace), mgr,
        _pool(session_row=_prior_week_session(), last_user_epoch=recent),
        _lock_factory(),
    )
    assert result["outcome"] == "user_active"
    mgr.close_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_resets_when_all_gates_pass(workspace: Path) -> None:
    """Diary done + prior-week session + user idle → close the session."""
    _write_today_diary(workspace)
    mgr = AsyncMock()
    long_idle = (_dt.datetime.now(_dt.timezone.utc)
                 - _dt.timedelta(hours=3)).timestamp()
    result = await ops.handle_session_reset(
        _config(workspace), mgr,
        _pool(session_row=_prior_week_session(), last_user_epoch=long_idle),
        _lock_factory(),
    )
    assert result["outcome"] == "reset"
    assert result["closed_session"] == "sess-old"
    mgr.close_session.assert_awaited_once_with("user:Nicolas")
