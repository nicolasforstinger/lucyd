"""Tests for evolution.py — state tracking and pre-check logic."""

import sqlite3
from pathlib import Path

import pytest

from evolution import (
    check_new_logs_exist,
    get_evolution_state,
)
from memory_schema import ensure_schema


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def mem_conn():
    """In-memory SQLite DB with full schema."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    yield conn
    conn.close()


@pytest.fixture
def workspace(tmp_path):
    """Workspace with MEMORY.md, USER.md, IDENTITY.md, and daily log files."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "MEMORY.md").write_text("# Memory\nI know things about the world.\n")
    (ws / "USER.md").write_text("# User\nNicolas lives in Austria.\n")
    (ws / "IDENTITY.md").write_text("# Identity\nI am Lucy, a goth AI familiar.\n")

    mem_dir = ws / "memory"
    mem_dir.mkdir()
    (mem_dir / "2026-02-20.md").write_text("Day 20 log content.\n")
    (mem_dir / "2026-02-21.md").write_text("Day 21 log content.\n")
    (mem_dir / "2026-02-22.md").write_text("Day 22 log content.\n")

    # Subdirectory that should be ignored
    cache_dir = mem_dir / "cache"
    cache_dir.mkdir()
    (cache_dir / "NOTES.md").write_text("Cached notes — should be ignored.\n")

    return ws


def _insert_evolution_state(conn, file_path, content_hash, logs_through):
    """Insert evolution state directly (helper for tests)."""
    conn.execute(
        "INSERT OR REPLACE INTO evolution_state "
        "(file_path, last_evolved_at, content_hash, logs_through) "
        "VALUES (?, datetime('now'), ?, ?)",
        (file_path, content_hash, logs_through),
    )
    conn.commit()


# ── TestEvolutionState ───────────────────────────────────────────


class TestEvolutionState:
    def test_get_state_returns_none_on_first_run(self, mem_conn):
        """No row exists — get_evolution_state returns None."""
        result = get_evolution_state("MEMORY.md", mem_conn)
        assert result is None

    def test_get_state_returns_stored_values(self, mem_conn):
        """Insert state then read it back."""
        _insert_evolution_state(mem_conn, "MEMORY.md", "abc123hash", "2026-02-22")

        state = get_evolution_state("MEMORY.md", mem_conn)
        assert state is not None
        assert state["content_hash"] == "abc123hash"
        assert state["logs_through"] == "2026-02-22"
        assert state["last_evolved_at"] is not None


# ── TestCheckNewLogsExist ───────────────────────────────────────


class TestCheckNewLogsExist:
    def test_returns_true_when_no_prior_state(self, workspace, mem_conn):
        """First run — no state, all logs are 'new'."""
        has_new, since = check_new_logs_exist(workspace, mem_conn)
        assert has_new is True
        assert since == ""

    def test_returns_false_when_no_new_logs(self, workspace, mem_conn):
        """All logs are older than logs_through — skip."""
        _insert_evolution_state(mem_conn, "MEMORY.md", "abc", "2026-02-22")
        has_new, since = check_new_logs_exist(workspace, mem_conn)
        assert has_new is False
        assert since == "2026-02-22"

    def test_returns_true_when_new_logs_exist(self, workspace, mem_conn):
        """New logs after logs_through — trigger."""
        _insert_evolution_state(mem_conn, "MEMORY.md", "abc", "2026-02-20")
        has_new, since = check_new_logs_exist(workspace, mem_conn)
        assert has_new is True
        assert since == "2026-02-20"

    def test_returns_false_when_no_memory_dir(self, tmp_path, mem_conn):
        """No memory directory at all — nothing to evolve."""
        ws = tmp_path / "empty-workspace"
        ws.mkdir()
        has_new, since = check_new_logs_exist(ws, mem_conn)
        assert has_new is False

    def test_uses_reference_file_for_state(self, workspace, mem_conn):
        """Custom reference file is used for state lookup."""
        _insert_evolution_state(mem_conn, "USER.md", "abc", "2026-02-22")
        # Default ref is MEMORY.md — no state for it, so has_new=True
        has_new, _ = check_new_logs_exist(workspace, mem_conn)
        assert has_new is True
        # With USER.md as ref — state exists, all logs ≤ 2026-02-22
        has_new, _ = check_new_logs_exist(workspace, mem_conn, reference_file="USER.md")
        assert has_new is False
