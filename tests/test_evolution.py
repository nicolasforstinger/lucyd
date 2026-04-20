"""Tests for evolution state tracking and pre-check logic (asyncpg)."""

import hashlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from operations import (
    check_new_logs_exist,
    get_evolution_state,
    update_evolution_state,
)

TEST_CLIENT_ID = "test"
TEST_AGENT_ID = "test_agent"

# ── Helpers ─────────────────────────────────────────────────────


async def _insert_evolution_state(
    pool: object, file_path: str, content_hash: str, logs_through: str,
) -> None:
    """Insert evolution state directly (helper for tests)."""
    await pool.execute(  # type: ignore[union-attr]
        "INSERT INTO knowledge.evolution_state "
        "(file_path, last_evolved_at, content_hash, logs_through) "
        "VALUES ($1, now(), $2, $3) "
        "ON CONFLICT (file_path) DO UPDATE SET "
        "last_evolved_at = now(), content_hash = EXCLUDED.content_hash, "
        "logs_through = EXCLUDED.logs_through",
        file_path, content_hash, logs_through,
    )


# ── Fixtures ────────────────────────────────────────────────────


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


# ── TestEvolutionState ──────────────────────────────────────────


class TestEvolutionState:
    @pytest.mark.asyncio
    async def test_get_state_returns_none_on_first_run(self, pool):
        """No row exists — get_evolution_state returns None."""
        result = await get_evolution_state(
            "MEMORY.md", pool,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_get_state_returns_stored_values(self, pool):
        """Insert state then read it back."""
        await _insert_evolution_state(pool, "MEMORY.md", "abc123hash", "2026-02-22")

        state = await get_evolution_state(
            "MEMORY.md", pool,
        )
        assert state is not None
        assert state["content_hash"] == "abc123hash"
        assert state["logs_through"] == "2026-02-22"
        assert state["last_evolved_at"] is not None


# ── TestCheckNewLogsExist ───────────────────────────────────────


class TestCheckNewLogsExist:
    @pytest.mark.asyncio
    async def test_returns_true_when_no_prior_state(self, workspace, pool):
        """First run — no state, all logs are 'new'."""
        has_new, since = await check_new_logs_exist(
            workspace, pool,
        )
        assert has_new is True
        assert since == ""

    @pytest.mark.asyncio
    async def test_returns_false_when_no_new_logs(self, workspace, pool):
        """All logs are older than logs_through — skip."""
        await _insert_evolution_state(pool, "MEMORY.md", "abc", "2026-02-22")
        has_new, since = await check_new_logs_exist(
            workspace, pool,
        )
        assert has_new is False
        assert since == "2026-02-22"

    @pytest.mark.asyncio
    async def test_returns_true_when_new_logs_exist(self, workspace, pool):
        """New logs after logs_through — trigger."""
        await _insert_evolution_state(pool, "MEMORY.md", "abc", "2026-02-20")
        has_new, since = await check_new_logs_exist(
            workspace, pool,
        )
        assert has_new is True
        assert since == "2026-02-20"

    @pytest.mark.asyncio
    async def test_returns_false_when_no_memory_dir(self, tmp_path, pool):
        """No memory directory at all — nothing to evolve."""
        ws = tmp_path / "empty-workspace"
        ws.mkdir()
        has_new, since = await check_new_logs_exist(
            ws, pool,
        )
        assert has_new is False

    @pytest.mark.asyncio
    async def test_uses_reference_file_for_state(self, workspace, pool):
        """Custom reference file is used for state lookup."""
        await _insert_evolution_state(pool, "USER.md", "abc", "2026-02-22")
        # Default ref is MEMORY.md — no state for it, so has_new=True
        has_new, _ = await check_new_logs_exist(
            workspace, pool,
        )
        assert has_new is True
        # With USER.md as ref — state exists, all logs <= 2026-02-22
        has_new, _ = await check_new_logs_exist(
            workspace, pool,
            reference_file="USER.md",
        )
        assert has_new is False


# ── TestUpdateEvolutionState ───────────────────────────────────


def _make_config(workspace: Path) -> MagicMock:
    """Build a minimal Config mock with the workspace property."""
    cfg = MagicMock()
    cfg.workspace = workspace
    return cfg


class TestUpdateEvolutionState:
    @pytest.mark.asyncio
    async def test_inserts_state_for_both_files(self, workspace, pool):
        """First call inserts rows for MEMORY.md and USER.md."""
        config = _make_config(workspace)
        result = await update_evolution_state(
            config, pool,
        )
        assert "MEMORY.md" in result
        assert "USER.md" in result

        for fname in ("MEMORY.md", "USER.md"):
            state = await get_evolution_state(
                fname, pool,
            )
            assert state is not None
            expected_hash = hashlib.sha256(
                (workspace / fname).read_text().encode(),
            ).hexdigest()
            assert state["content_hash"] == expected_hash
            assert state["logs_through"] == "2026-02-22"

    @pytest.mark.asyncio
    async def test_upserts_on_second_call(self, workspace, pool):
        """Second call updates existing rows rather than failing."""
        config = _make_config(workspace)
        await update_evolution_state(config, pool)

        # Modify MEMORY.md and re-run
        (workspace / "MEMORY.md").write_text("# Updated memory\n")
        result = await update_evolution_state(
            config, pool,
        )

        state = await get_evolution_state(
            "MEMORY.md", pool,
        )
        assert state is not None
        assert state["content_hash"] == result["MEMORY.md"]
        expected = hashlib.sha256(b"# Updated memory\n").hexdigest()
        assert state["content_hash"] == expected

    @pytest.mark.asyncio
    async def test_handles_missing_memory_dir(self, tmp_path, pool):
        """No memory/ dir — logs_through is empty string."""
        ws = tmp_path / "bare"
        ws.mkdir()
        (ws / "MEMORY.md").write_text("x")
        (ws / "USER.md").write_text("y")

        config = _make_config(ws)
        await update_evolution_state(config, pool)

        state = await get_evolution_state(
            "MEMORY.md", pool,
        )
        assert state is not None
        assert state["logs_through"] == ""
