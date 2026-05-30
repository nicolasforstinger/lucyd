"""Tests for the self-maintenance heartbeat.

Covers operations.handle_maintain (interval gate, dispatch, brief assembly,
marker advance) and maintain_state (state round-trip, workspace diff, fact
diff, idle query). Boundaries mocked: process_message (the LLM turn), the
asyncpg pool (facts + idle queries), and memory.run_maintenance (mechanical
maintenance, exercised in its own suite). The filesystem is real (tmp_path).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import maintain_state
import operations as ops
from config import Config


# ─── Fixtures ────────────────────────────────────────────────────


def _config(workspace: Path, data_dir: Path, *, enabled: bool = True) -> Config:
    """Minimal Config with the [maintain] section and tmp paths."""
    return Config({
        "agent": {"name": "Lucy", "workspace": str(workspace)},
        "user": {"name": "Nicolas", "timezone": "Europe/Vienna"},
        "models": {"primary": {
            "provider": "anthropic", "model": "m",
            "cost_per_mtok": [1.0, 1.0, 1.0, 1.0],
        }},
        "paths": {"data_dir": str(data_dir)},
        "maintain": {
            "enabled": enabled,
            "interval_min_minutes": 240,
            "interval_max_minutes": 480,
            "idle_minutes": 360,
        },
    })


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    (ws / "notes").mkdir(parents=True)
    (ws / "memory").mkdir(parents=True)
    (ws / "MAINTAIN.md").write_text("# protocol body\nRun your pass.")
    (ws / "MEMORY.md").write_text("memory")
    return ws


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _lock_factory() -> Any:
    """get_session_lock stand-in: returns a real asyncio.Lock per key."""
    locks: dict[str, asyncio.Lock] = {}

    def factory(key: str) -> asyncio.Lock:
        return locks.setdefault(key, asyncio.Lock())

    return factory


def _pool(*, facts: list[dict[str, str]] | None = None,
          last_user_epoch: float | None = None) -> AsyncMock:
    """Pool stub: fetch → facts rows, fetchval → last-user-message epoch."""
    pool = AsyncMock()
    pool.fetch = AsyncMock(return_value=facts or [])
    pool.fetchval = AsyncMock(return_value=last_user_epoch)
    return pool


# ─── Interval gate ───────────────────────────────────────────────


class TestIntervalGate:
    @pytest.mark.asyncio
    async def test_first_pass_dispatches(self, workspace, data_dir):
        """No prior marker → the pass dispatches (treated as first pass)."""
        pm = AsyncMock()
        with patch("memory.run_maintenance", AsyncMock(return_value={})):
            result = await ops.handle_maintain(
                _config(workspace, data_dir), _pool(), None,
                pm, _lock_factory(),
            )
        assert result["outcome"] == "ran"
        pm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_too_soon_skips_dispatch(self, workspace, data_dir):
        """Recent marker (< interval) → too_soon, no LLM dispatch."""
        path = maintain_state.state_path(data_dir)
        recent = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=5)
        maintain_state.save_last_pass(path, recent)

        pm = AsyncMock()
        with patch("memory.run_maintenance", AsyncMock(return_value={})):
            result = await ops.handle_maintain(
                _config(workspace, data_dir), _pool(), None,
                pm, _lock_factory(),
            )
        assert result["outcome"] == "too_soon"
        pm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_elapsed_past_interval_dispatches(self, workspace, data_dir):
        """Marker older than the max interval → always dispatches."""
        path = maintain_state.state_path(data_dir)
        old = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=12)
        maintain_state.save_last_pass(path, old)

        pm = AsyncMock()
        with patch("memory.run_maintenance", AsyncMock(return_value={})):
            result = await ops.handle_maintain(
                _config(workspace, data_dir), _pool(), None,
                pm, _lock_factory(),
            )
        assert result["outcome"] == "ran"
        pm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disabled_runs_mechanical_only(self, workspace, data_dir):
        """maintain_enabled=False → mechanical maintenance only, no dispatch."""
        pm = AsyncMock()
        with patch("memory.run_maintenance",
                   AsyncMock(return_value={"stale": 3})) as rm:
            result = await ops.handle_maintain(
                _config(workspace, data_dir, enabled=False), _pool(), None,
                pm, _lock_factory(),
            )
        assert result["outcome"] == "disabled"
        assert result["maintenance"]["stale"] == 3
        rm.assert_awaited_once()
        pm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mechanical_maintenance_runs_even_when_too_soon(
        self, workspace, data_dir,
    ):
        """Mechanical maintenance runs on every call, including too_soon."""
        path = maintain_state.state_path(data_dir)
        maintain_state.save_last_pass(
            path, _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=1),
        )
        with patch("memory.run_maintenance",
                   AsyncMock(return_value={"stale": 1})) as rm:
            result = await ops.handle_maintain(
                _config(workspace, data_dir), _pool(), None,
                AsyncMock(), _lock_factory(),
            )
        assert result["outcome"] == "too_soon"
        rm.assert_awaited_once()


# ─── Dispatch shape ──────────────────────────────────────────────


class TestDispatch:
    @pytest.mark.asyncio
    async def test_dispatch_envelope(self, workspace, data_dir):
        """The pass is a silent system:maintenance turn in its own session."""
        pm = AsyncMock()
        with patch("memory.run_maintenance", AsyncMock(return_value={})):
            await ops.handle_maintain(
                _config(workspace, data_dir), _pool(), None,
                pm, _lock_factory(),
            )
        kwargs = pm.await_args.kwargs
        assert kwargs["sender"] == "maintenance"
        assert kwargs["talker"] == "system"
        assert kwargs["reply_to"] == "silent"
        assert kwargs["session_key"] == "system:maintenance"
        assert kwargs["trace_id"]

    @pytest.mark.asyncio
    async def test_brief_includes_protocol_body(self, workspace, data_dir):
        """MAINTAIN.md contents are carried in the brief text."""
        pm = AsyncMock()
        with patch("memory.run_maintenance", AsyncMock(return_value={})):
            await ops.handle_maintain(
                _config(workspace, data_dir), _pool(), None,
                pm, _lock_factory(),
            )
        text = pm.await_args.kwargs["text"]
        assert "# protocol body" in text
        assert "Run your pass." in text

    @pytest.mark.asyncio
    async def test_brief_header_fields(self, workspace, data_dir):
        """Header carries last-pass marker, ledger path, and idle line."""
        pm = AsyncMock()
        with patch("memory.run_maintenance", AsyncMock(return_value={})):
            await ops.handle_maintain(
                _config(workspace, data_dir),
                _pool(last_user_epoch=None), None,
                pm, _lock_factory(),
            )
        text = pm.await_args.kwargs["text"]
        assert "Last pass: never (first pass)" in text
        assert "notes/maintenance-log.md" in text
        assert "Nicolas has no messages on record yet." in text

    @pytest.mark.asyncio
    async def test_idle_line_minutes_and_hours(self, workspace, data_dir):
        """Idle line renders minutes under an hour, hours above."""
        import time as _t
        now = _t.time()
        pm = AsyncMock()
        with patch("memory.run_maintenance", AsyncMock(return_value={})):
            await ops.handle_maintain(
                _config(workspace, data_dir),
                _pool(last_user_epoch=now - 1800), None,  # 30 min ago
                pm, _lock_factory(),
            )
        assert "minutes ago" in pm.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_missing_protocol_skips_pass(self, workspace, data_dir):
        """MAINTAIN.md absent → skip LLM pass, mechanical still ran."""
        (workspace / "MAINTAIN.md").unlink()
        pm = AsyncMock()
        with patch("memory.run_maintenance",
                   AsyncMock(return_value={"stale": 0})) as rm:
            result = await ops.handle_maintain(
                _config(workspace, data_dir), _pool(), None,
                pm, _lock_factory(),
            )
        assert result["outcome"] == "skipped"
        assert result["reason"] == "MAINTAIN.md missing"
        rm.assert_awaited_once()
        pm.assert_not_awaited()


# ─── Marker advance ──────────────────────────────────────────────


class TestMarkerAdvance:
    @pytest.mark.asyncio
    async def test_marker_written_after_dispatch(self, workspace, data_dir):
        path = maintain_state.state_path(data_dir)
        assert not path.exists()
        with patch("memory.run_maintenance", AsyncMock(return_value={})):
            await ops.handle_maintain(
                _config(workspace, data_dir), _pool(), None,
                AsyncMock(), _lock_factory(),
            )
        assert path.exists()
        assert maintain_state.load_state(path).last_pass_at is not None

    @pytest.mark.asyncio
    async def test_marker_not_advanced_on_too_soon(self, workspace, data_dir):
        path = maintain_state.state_path(data_dir)
        marker = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=1)
        maintain_state.save_last_pass(path, marker)
        before = path.read_text()
        with patch("memory.run_maintenance", AsyncMock(return_value={})):
            await ops.handle_maintain(
                _config(workspace, data_dir), _pool(), None,
                AsyncMock(), _lock_factory(),
            )
        assert path.read_text() == before


# ─── State round-trip ────────────────────────────────────────────


class TestState:
    def test_round_trip(self, tmp_path):
        path = tmp_path / "maintain" / "state.json"
        when = _dt.datetime(2026, 5, 24, 12, 30, 0, tzinfo=_dt.timezone.utc)
        maintain_state.save_last_pass(path, when)
        loaded = maintain_state.load_state(path)
        assert loaded.last_pass_at == when

    def test_missing_file_is_first_run(self, tmp_path):
        loaded = maintain_state.load_state(tmp_path / "absent.json")
        assert loaded.last_pass_at is None

    def test_invalid_json_is_first_run(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("{ not json")
        assert maintain_state.load_state(path).last_pass_at is None

    def test_missing_key_is_first_run(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"other": "x"}))
        assert maintain_state.load_state(path).last_pass_at is None

    def test_unparseable_timestamp_is_first_run(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"last_pass_at": "not-a-date"}))
        assert maintain_state.load_state(path).last_pass_at is None

    def test_state_path_under_data_dir(self):
        assert maintain_state.state_path(Path("/data")) == \
            Path("/data/maintain/state.json")


# ─── Workspace diff ──────────────────────────────────────────────


class TestWorkspaceDiff:
    def test_first_pass_returns_all_md(self, tmp_path):
        ws = tmp_path / "ws"
        (ws / "notes").mkdir(parents=True)
        (ws / "MEMORY.md").write_text("m")
        (ws / "USER.md").write_text("u")
        (ws / "notes" / "n1.md").write_text("n")
        (ws / "avatar.png").write_bytes(b"x")  # non-md, ignored
        changed = maintain_state.changed_workspace_files(ws, None)
        assert changed == ["MEMORY.md", "USER.md", "notes/n1.md"]

    def test_only_files_newer_than_marker(self, tmp_path):
        ws = tmp_path / "ws"
        (ws / "memory").mkdir(parents=True)
        old = ws / "MEMORY.md"
        old.write_text("old")
        marker_epoch = old.stat().st_mtime + 1
        marker = _dt.datetime.fromtimestamp(marker_epoch, _dt.timezone.utc)
        # New file written after the marker.
        new = ws / "memory" / "2026-05-24.md"
        new.write_text("new")
        import os
        os.utime(new, (marker_epoch + 10, marker_epoch + 10))
        changed = maintain_state.changed_workspace_files(ws, marker)
        assert changed == ["memory/2026-05-24.md"]

    def test_missing_workspace_empty(self, tmp_path):
        assert maintain_state.changed_workspace_files(tmp_path / "nope", None) == []


# ─── Fact diff ───────────────────────────────────────────────────


class TestFactDiff:
    @pytest.mark.asyncio
    async def test_facts_since_formats_rows(self):
        pool = _pool(facts=[
            {"entity": "Nicolas", "attribute": "likes", "value": "tea"},
            {"entity": "Lucy", "attribute": "role", "value": "agent"},
        ])
        when = _dt.datetime(2026, 5, 1, tzinfo=_dt.timezone.utc)
        out = await maintain_state.facts_created_since(pool, when)
        assert out == ["Nicolas · likes · tea", "Lucy · role · agent"]
        pool.fetch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_first_pass_skips_fact_query(self):
        pool = _pool()
        out = await maintain_state.facts_created_since(pool, None)
        assert out == []
        pool.fetch.assert_not_awaited()


# ─── Idle query ──────────────────────────────────────────────────


class TestIdle:
    @pytest.mark.asyncio
    async def test_none_when_no_messages(self):
        assert await maintain_state.idle_minutes_since_user(
            _pool(last_user_epoch=None), "user:Nicolas") is None

    @pytest.mark.asyncio
    async def test_minutes_since_last(self):
        import time as _t
        pool = _pool(last_user_epoch=_t.time() - 3600)  # 1 hour ago
        idle = await maintain_state.idle_minutes_since_user(pool, "user:Nicolas")
        assert idle is not None
        assert 59.0 <= idle <= 61.0


# ─── Brief assembly diff content ─────────────────────────────────


class TestBriefDiffContent:
    @pytest.mark.asyncio
    async def test_changed_files_and_facts_in_brief(self, workspace, data_dir):
        """A real diff (changed file + new fact) lands in the dispatched brief."""
        path = maintain_state.state_path(data_dir)
        old = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=12)
        maintain_state.save_last_pass(path, old)
        # MEMORY.md mtime is "now" → newer than the 12h-old marker.
        pool = _pool(facts=[
            {"entity": "Nicolas", "attribute": "city", "value": "Vienna"},
        ])
        pm = AsyncMock()
        with patch("memory.run_maintenance", AsyncMock(return_value={})):
            await ops.handle_maintain(
                _config(workspace, data_dir), pool, None, pm, _lock_factory(),
            )
        text = pm.await_args.kwargs["text"]
        assert "MEMORY.md" in text
        assert "Nicolas · city · Vienna" in text
