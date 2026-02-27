"""Tests for evolution.py — state tracking, context gathering, prompt building, file evolution."""

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from evolution import (
    build_evolution_prompt,
    check_new_logs_exist,
    evolve_file,
    gather_daily_logs,
    gather_structured_context,
    get_evolution_state,
    run_evolution,
    update_evolution_state,
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


def _make_provider(response_text: str):
    """Create a mock provider returning the given text."""

    @dataclass
    class FakeResponse:
        text: str

    provider = AsyncMock()
    provider.format_system.return_value = "system"
    provider.format_messages.return_value = "messages"
    provider.complete.return_value = FakeResponse(text=response_text)
    return provider


class FakeConfig:
    """Minimal config mock for evolution functions."""

    def __init__(self, workspace: Path, **overrides):
        self.workspace = workspace
        self.evolution_enabled = overrides.get("evolution_enabled", True)
        self.evolution_files = overrides.get("evolution_files", ["MEMORY.md", "USER.md"])
        self.evolution_model = overrides.get("evolution_model", "primary")
        self.evolution_anchor_file = overrides.get("evolution_anchor_file", "IDENTITY.md")
        self.evolution_max_log_chars = overrides.get("evolution_max_log_chars", 80_000)
        self.evolution_max_facts = overrides.get("evolution_max_facts", 50)
        self.evolution_max_episodes = overrides.get("evolution_max_episodes", 20)
        self._models = overrides.get("models", {
            "primary": {
                "provider": "openai-compat",
                "model": "test-model",
                "max_tokens": 4096,
                "api_key_env": "",
            }
        })

    def model_config(self, name: str) -> dict:
        if name not in self._models:
            raise ValueError(f"No model config for '{name}'")
        return self._models[name]


# ── TestEvolutionState ───────────────────────────────────────────


class TestEvolutionState:
    def test_get_state_returns_none_on_first_run(self, mem_conn):
        """No row exists — get_evolution_state returns None."""
        result = get_evolution_state("MEMORY.md", mem_conn)
        assert result is None

    def test_update_and_get_round_trip(self, mem_conn):
        """Insert state then read it back."""
        update_evolution_state("MEMORY.md", "abc123hash", "2026-02-22", mem_conn)
        mem_conn.commit()

        state = get_evolution_state("MEMORY.md", mem_conn)
        assert state is not None
        assert state["content_hash"] == "abc123hash"
        assert state["logs_through"] == "2026-02-22"
        assert state["last_evolved_at"] is not None

    def test_update_replaces_existing(self, mem_conn):
        """Insert twice — second replaces first, single row remains."""
        update_evolution_state("MEMORY.md", "hash1", "2026-02-20", mem_conn)
        mem_conn.commit()
        update_evolution_state("MEMORY.md", "hash2", "2026-02-22", mem_conn)
        mem_conn.commit()

        state = get_evolution_state("MEMORY.md", mem_conn)
        assert state["content_hash"] == "hash2"
        assert state["logs_through"] == "2026-02-22"

        # Verify only one row
        rows = mem_conn.execute(
            "SELECT * FROM evolution_state WHERE file_path = ?",
            ("MEMORY.md",),
        ).fetchall()
        assert len(rows) == 1


# ── TestCheckNewLogsExist ───────────────────────────────────────


class TestCheckNewLogsExist:
    def test_returns_true_when_no_prior_state(self, workspace, mem_conn):
        """First run — no state, all logs are 'new'."""
        has_new, since = check_new_logs_exist(workspace, mem_conn)
        assert has_new is True
        assert since == ""

    def test_returns_false_when_no_new_logs(self, workspace, mem_conn):
        """All logs are older than logs_through — skip."""
        update_evolution_state("MEMORY.md", "abc", "2026-02-22", mem_conn)
        mem_conn.commit()
        has_new, since = check_new_logs_exist(workspace, mem_conn)
        assert has_new is False
        assert since == "2026-02-22"

    def test_returns_true_when_new_logs_exist(self, workspace, mem_conn):
        """New logs after logs_through — trigger."""
        update_evolution_state("MEMORY.md", "abc", "2026-02-20", mem_conn)
        mem_conn.commit()
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
        update_evolution_state("USER.md", "abc", "2026-02-22", mem_conn)
        mem_conn.commit()
        # Default ref is MEMORY.md — no state for it, so has_new=True
        has_new, _ = check_new_logs_exist(workspace, mem_conn)
        assert has_new is True
        # With USER.md as ref — state exists, all logs ≤ 2026-02-22
        has_new, _ = check_new_logs_exist(workspace, mem_conn, reference_file="USER.md")
        assert has_new is False


# ── TestGatherDailyLogs ─────────────────────────────────────────


class TestGatherDailyLogs:
    def test_gathers_all_logs_when_no_since_date(self, workspace):
        """First run — no since_date, all logs included."""
        text, latest = gather_daily_logs(workspace, since_date=None)
        assert "Day 20" in text
        assert "Day 21" in text
        assert "Day 22" in text
        assert latest == "2026-02-22"

    def test_since_date_includes_all_logs_when_new_exist(self, workspace):
        """All logs are included for full reinterpretation when new logs exist."""
        text, latest = gather_daily_logs(workspace, since_date="2026-02-20")
        # New logs exist (02-21 and 02-22 are after 02-20), so all logs included
        assert "Day 20" in text
        assert "Day 21" in text
        assert "Day 22" in text
        assert latest == "2026-02-22"

    def test_since_date_returns_empty_when_no_new_logs(self, workspace):
        """Returns empty when no logs are newer than since_date."""
        text, latest = gather_daily_logs(workspace, since_date="2026-02-22")
        assert text == ""
        assert latest == ""

    def test_respects_max_chars_drops_oldest(self, workspace):
        """When max_chars is tight, newest logs survive, oldest are dropped."""
        # Each log is ~21 chars. Separator is "\n\n---\n\n" = 9 chars.
        # With max_chars=30, only the newest log should survive fully.
        text, latest = gather_daily_logs(workspace, max_chars=30)
        assert "Day 22" in text  # newest survives
        assert latest == "2026-02-22"
        # Oldest should be absent or truncated
        assert "Day 20" not in text

    def test_empty_directory_returns_empty(self, tmp_path):
        """Workspace with empty memory dir returns empty."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        mem_dir = ws / "memory"
        mem_dir.mkdir()

        text, latest = gather_daily_logs(ws)
        assert text == ""
        assert latest == ""

    def test_ignores_subdirectories(self, workspace):
        """Files in memory/cache/ are not picked up."""
        text, latest = gather_daily_logs(workspace)
        assert "Cached notes" not in text
        assert "NOTES" not in text


# ── TestGatherStructuredContext ──────────────────────────────────


class TestGatherStructuredContext:
    def test_includes_facts_episodes_commitments(self, mem_conn):
        """All three structured types present in output."""
        # Insert a fact
        mem_conn.execute(
            "INSERT INTO facts (entity, attribute, value, confidence, source_session, accessed_at) "
            "VALUES ('nicolas', 'lives_in', 'Austria', 0.9, 'sess1', datetime('now'))"
        )
        # Insert an episode
        mem_conn.execute(
            "INSERT INTO episodes (session_id, date, summary, emotional_tone, topics) "
            "VALUES ('sess1', '2026-02-22', 'Discussed the framework.', 'productive', 'framework')"
        )
        # Insert an open commitment
        mem_conn.execute(
            "INSERT INTO commitments (who, what, deadline, status) "
            "VALUES ('nicolas', 'review the PR', '2026-03-01', 'open')"
        )
        mem_conn.commit()

        result = gather_structured_context(mem_conn)
        assert "nicolas.lives_in = Austria" in result
        assert "Discussed the framework." in result
        assert "review the PR" in result

    def test_empty_db_returns_empty_string(self, mem_conn):
        """No facts, episodes, or commitments — returns empty string."""
        result = gather_structured_context(mem_conn)
        assert result == ""


# ── TestBuildEvolutionPrompt ─────────────────────────────────────


class TestBuildEvolutionPrompt:
    def test_memory_md_uses_correct_system_prompt(self):
        """MEMORY.md dispatches to the memory-specific system prompt."""
        system, user_msg = build_evolution_prompt(
            file_name="MEMORY.md",
            current_content="current memory",
            anchor_content="identity anchor",
            daily_logs="some logs",
            structured_context="some facts",
        )
        assert "MEMORY.md is a living knowledge base" in system
        assert "USER.md" not in system

    def test_user_md_uses_correct_system_prompt(self):
        """USER.md dispatches to the user-specific system prompt."""
        system, user_msg = build_evolution_prompt(
            file_name="USER.md",
            current_content="current user file",
            anchor_content="identity anchor",
            daily_logs="some logs",
            structured_context="some facts",
        )
        assert "USER.md is the author's perception" in system
        assert "living knowledge base" not in system

    def test_extra_context_included_for_user_md(self):
        """When extra_context is provided, MEMORY.md content appears in user message."""
        system, user_msg = build_evolution_prompt(
            file_name="USER.md",
            current_content="user file content",
            anchor_content="identity anchor",
            daily_logs="daily logs here",
            structured_context="structured context here",
            extra_context="This is the MEMORY.md content for context.",
        )
        assert "This is the MEMORY.md content for context." in user_msg
        assert "CURRENT MEMORY.md" in user_msg


# ── TestEvolveFile ───────────────────────────────────────────────


class TestEvolveFile:
    @pytest.mark.asyncio
    async def test_successful_evolution(self, mem_conn, workspace):
        """Happy path — file replaced with new content, returns True."""
        original = (workspace / "MEMORY.md").read_text()
        # New content roughly same length as original
        new_content = "# Memory\nI have learned many new things this week.\n"
        provider = _make_provider(new_content)
        config = FakeConfig(workspace)

        result = await evolve_file(
            file_path=workspace / "MEMORY.md",
            file_name="MEMORY.md",
            anchor_path=workspace / "IDENTITY.md",
            workspace=workspace,
            provider=provider,
            conn=mem_conn,
            config=config,
        )

        assert result is True
        written = (workspace / "MEMORY.md").read_text()
        assert "learned many new things" in written
        provider.complete.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_response_preserves_original(self, mem_conn, workspace):
        """LLM returns empty string — original file preserved."""
        original = (workspace / "MEMORY.md").read_text()
        provider = _make_provider("")
        config = FakeConfig(workspace)

        result = await evolve_file(
            file_path=workspace / "MEMORY.md",
            file_name="MEMORY.md",
            anchor_path=workspace / "IDENTITY.md",
            workspace=workspace,
            provider=provider,
            conn=mem_conn,
            config=config,
        )

        assert result is False
        assert (workspace / "MEMORY.md").read_text() == original

    @pytest.mark.asyncio
    async def test_too_short_response_preserves_original(self, mem_conn, workspace):
        """LLM returns content under 50% of original — rejected."""
        original = (workspace / "MEMORY.md").read_text()
        # Return something much shorter than original
        short_content = "x"
        provider = _make_provider(short_content)
        config = FakeConfig(workspace)

        result = await evolve_file(
            file_path=workspace / "MEMORY.md",
            file_name="MEMORY.md",
            anchor_path=workspace / "IDENTITY.md",
            workspace=workspace,
            provider=provider,
            conn=mem_conn,
            config=config,
        )

        assert result is False
        assert (workspace / "MEMORY.md").read_text() == original

    @pytest.mark.asyncio
    async def test_too_long_response_preserves_original(self, mem_conn, workspace):
        """LLM returns content over 200% of original — rejected."""
        original = (workspace / "MEMORY.md").read_text()
        original_len = len(original)
        # Return something more than 2x the original
        long_content = "x" * (original_len * 3)
        provider = _make_provider(long_content)
        config = FakeConfig(workspace)

        result = await evolve_file(
            file_path=workspace / "MEMORY.md",
            file_name="MEMORY.md",
            anchor_path=workspace / "IDENTITY.md",
            workspace=workspace,
            provider=provider,
            conn=mem_conn,
            config=config,
        )

        assert result is False
        assert (workspace / "MEMORY.md").read_text() == original

    @pytest.mark.asyncio
    async def test_skips_when_no_new_logs(self, mem_conn, workspace):
        """No daily logs since last evolution — returns False without LLM call."""
        # Set state as if we already processed through the latest log
        update_evolution_state("MEMORY.md", "somehash", "2026-02-22", mem_conn)
        mem_conn.commit()

        provider = _make_provider("should not be called")
        config = FakeConfig(workspace)

        result = await evolve_file(
            file_path=workspace / "MEMORY.md",
            file_name="MEMORY.md",
            anchor_path=workspace / "IDENTITY.md",
            workspace=workspace,
            provider=provider,
            conn=mem_conn,
            config=config,
        )

        assert result is False
        provider.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_provider_error_preserves_original(self, mem_conn, workspace):
        """LLM raises exception — original file preserved."""
        original = (workspace / "MEMORY.md").read_text()
        provider = _make_provider("")
        provider.complete.side_effect = RuntimeError("API exploded")
        config = FakeConfig(workspace)

        result = await evolve_file(
            file_path=workspace / "MEMORY.md",
            file_name="MEMORY.md",
            anchor_path=workspace / "IDENTITY.md",
            workspace=workspace,
            provider=provider,
            conn=mem_conn,
            config=config,
        )

        assert result is False
        assert (workspace / "MEMORY.md").read_text() == original

    @pytest.mark.asyncio
    async def test_updates_state_after_success(self, mem_conn, workspace):
        """After successful evolution, DB state is updated with hash and date."""
        new_content = "# Memory\nI have learned many new things this week.\n"
        provider = _make_provider(new_content)
        config = FakeConfig(workspace)

        await evolve_file(
            file_path=workspace / "MEMORY.md",
            file_name="MEMORY.md",
            anchor_path=workspace / "IDENTITY.md",
            workspace=workspace,
            provider=provider,
            conn=mem_conn,
            config=config,
        )

        state = get_evolution_state("MEMORY.md", mem_conn)
        assert state is not None
        assert state["logs_through"] == "2026-02-22"
        assert state["content_hash"] is not None
        assert len(state["content_hash"]) == 64  # SHA-256 hex digest


# ── TestRunEvolution ─────────────────────────────────────────────


class TestRunEvolution:
    @pytest.mark.asyncio
    async def test_processes_configured_files(self, mem_conn, workspace):
        """Both MEMORY.md and USER.md are processed, results reported."""
        # Content must be roughly similar length to originals
        memory_content = "# Memory\nI have learned many new things this week.\n"
        user_content = "# User\nNicolas is doing well in Austria.\n"

        call_count = 0

        @dataclass
        class FakeResponse:
            text: str

        class SequenceProvider:
            """Provider that returns different content per call."""
            def __init__(self):
                self.responses = [memory_content, user_content]
                self.idx = 0

            def format_system(self, blocks):
                return blocks

            def format_messages(self, msgs):
                return msgs

            async def complete(self, system, messages, tools):
                text = self.responses[self.idx] if self.idx < len(self.responses) else ""
                self.idx += 1
                return FakeResponse(text=text)

        fake_provider = SequenceProvider()
        config = FakeConfig(workspace, evolution_files=["MEMORY.md", "USER.md"])

        with patch("providers.create_provider", return_value=fake_provider):
            result = await run_evolution(config, mem_conn)

        assert result["error"] is None
        assert "MEMORY.md" in result["evolved"]
        assert "USER.md" in result["evolved"]
        assert result["skipped"] == []

    @pytest.mark.asyncio
    async def test_disabled_returns_error(self, mem_conn, workspace):
        """evolution_enabled=False returns error without processing."""
        config = FakeConfig(workspace, evolution_enabled=False)

        result = await run_evolution(config, mem_conn)

        assert result["error"] == "evolution disabled"
        assert result["evolved"] == []

    @pytest.mark.asyncio
    async def test_no_files_returns_error(self, mem_conn, workspace):
        """Empty evolution_files list returns error."""
        config = FakeConfig(workspace, evolution_files=[])

        result = await run_evolution(config, mem_conn)

        assert result["error"] == "no files configured"
        assert result["evolved"] == []

    @pytest.mark.asyncio
    async def test_memory_md_content_passed_to_user_md(self, mem_conn, workspace):
        """After MEMORY.md evolves, its new content is passed as extra_context to USER.md."""
        memory_new = "# Memory\nI have evolved knowledge about the world.\n"
        user_new = "# User\nNicolas is doing well in Austria.\n"

        @dataclass
        class FakeResponse:
            text: str

        class TrackingProvider:
            """Provider that tracks what messages were sent."""
            def __init__(self):
                self.responses = [memory_new, user_new]
                self.idx = 0
                self.calls = []

            def format_system(self, blocks):
                return blocks

            def format_messages(self, msgs):
                # Store raw messages for inspection
                self.calls.append(msgs)
                return msgs

            async def complete(self, system, messages, tools):
                text = self.responses[self.idx] if self.idx < len(self.responses) else ""
                self.idx += 1
                return FakeResponse(text=text)

        tracking_provider = TrackingProvider()
        config = FakeConfig(workspace, evolution_files=["MEMORY.md", "USER.md"])

        with patch("providers.create_provider", return_value=tracking_provider):
            result = await run_evolution(config, mem_conn)

        assert "MEMORY.md" in result["evolved"]
        assert "USER.md" in result["evolved"]

        # The second call (USER.md) should have received the evolved MEMORY.md
        # content in its user message. The format_messages call stores the raw
        # messages list — the user message text should contain the new MEMORY.md.
        assert len(tracking_provider.calls) == 2
        user_md_messages = tracking_provider.calls[1]
        # The messages list has one entry with role: user
        user_msg_text = user_md_messages[0]["content"]
        assert "evolved knowledge about the world" in user_msg_text
