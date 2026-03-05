"""Tests for zero-kill-rate modules: status, memory_tools, skills_tool.

Phase 2: Each test calls the REAL function — no reimplemented logic.
"""

import sqlite3
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory_schema import ensure_schema

# ─── tools/status.py ─────────────────────────────────────────────


class TestToolSessionStatus:
    """Tests calling REAL tool_session_status."""

    def test_no_session_returns_no_data(self):
        """With nothing configured, returns 'No status data available.'"""
        import tools.status as mod
        original = (mod._current_session, mod._daemon_start_time, mod._cost_db_path)
        mod._current_session = None
        mod._daemon_start_time = 0.0
        mod._cost_db_path = ""
        try:
            result = mod.tool_session_status()
            assert "No status data available" in result
        finally:
            mod._current_session, mod._daemon_start_time, mod._cost_db_path = original

    def test_reports_tokens_and_context_pct(self):
        """With a session, reports token count and context percentage."""
        import tools.status as mod
        original = (mod._current_session, mod._daemon_start_time, mod._cost_db_path, mod.MAX_CONTEXT_TOKENS)
        session = MagicMock()
        session.last_input_tokens = 50000
        session.messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "text": "hello"}]
        session.compaction_count = 2
        mod._current_session = session
        mod._daemon_start_time = 0.0
        mod._cost_db_path = ""
        mod.MAX_CONTEXT_TOKENS = 200000
        try:
            result = mod.tool_session_status()
            assert "50,000 tokens" in result
            assert "25%" in result
            assert "Messages: 2" in result
            assert "Compactions: 2" in result
        finally:
            mod._current_session, mod._daemon_start_time, mod._cost_db_path, mod.MAX_CONTEXT_TOKENS = original

    def test_reports_uptime(self):
        """Reports daemon uptime when start_time is set."""
        import tools.status as mod
        original = (mod._current_session, mod._daemon_start_time, mod._cost_db_path)
        mod._current_session = None
        mod._daemon_start_time = time.time() - 3700  # ~1h 1m
        mod._cost_db_path = ""
        try:
            result = mod.tool_session_status()
            assert "Daemon uptime:" in result
            assert "1h" in result
        finally:
            mod._current_session, mod._daemon_start_time, mod._cost_db_path = original

    def test_reports_cost(self, tmp_path):
        """Reports cost from cost DB when configured."""
        import tools.status as mod
        db_path = str(tmp_path / "cost.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE costs (
                timestamp INTEGER, session_id TEXT, model TEXT,
                input_tokens INTEGER, output_tokens INTEGER,
                cache_read_tokens INTEGER, cache_write_tokens INTEGER,
                cost_usd REAL
            )
        """)
        conn.execute(
            "INSERT INTO costs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (int(time.time()), "s1", "opus", 10000, 5000, 0, 0, 1.2345),
        )
        conn.commit()
        conn.close()

        original = (mod._current_session, mod._daemon_start_time, mod._cost_db_path)
        mod._current_session = None
        mod._daemon_start_time = 0.0
        mod._cost_db_path = db_path
        try:
            result = mod.tool_session_status()
            assert "$1.2345" in result
            assert "10,000 in" in result
        finally:
            mod._current_session, mod._daemon_start_time, mod._cost_db_path = original

    def test_configure_sets_max_context_tokens(self):
        """configure() with max_context_tokens updates the module global."""
        import tools.status as mod
        original = mod.MAX_CONTEXT_TOKENS
        mod.configure(max_context_tokens=300000)
        try:
            assert mod.MAX_CONTEXT_TOKENS == 300000
        finally:
            mod.MAX_CONTEXT_TOKENS = original

    def test_set_current_session(self):
        """set_current_session updates module global."""
        import tools.status as mod
        original = mod._current_session
        fake_session = MagicMock()
        mod.set_current_session(fake_session)
        try:
            assert mod._current_session is fake_session
        finally:
            mod._current_session = original


# ─── tools/memory_tools.py ──────────────────────────────────────


class TestToolMemorySearch:
    """Tests calling REAL tool_memory_search."""

    @pytest.mark.asyncio
    async def test_no_memory_configured(self):
        """Returns error when memory not configured."""
        import tools.memory_tools as mod
        original = mod._memory
        mod._memory = None
        try:
            result = await mod.tool_memory_search("test query")
            assert "Memory not configured" in result
        finally:
            mod._memory = original

    @pytest.mark.asyncio
    async def test_search_returns_results(self):
        """Search with results formats them correctly."""
        import tools.memory_tools as mod
        mock_mem = AsyncMock()
        mock_mem.search.return_value = [
            {"source": "SOUL.md", "text": "I am Lucyd", "score": 0.95},
            {"source": "MEMORY.md", "text": "Previous context", "score": 0.8},
        ]
        original = mod._memory
        mod._memory = mock_mem
        try:
            result = await mod.tool_memory_search("who am I")
            assert "SOUL.md" in result
            assert "I am Lucyd" in result
            assert "0.950" in result
            assert "MEMORY.md" in result
            assert "---" in result  # separator between results
        finally:
            mod._memory = original

    @pytest.mark.asyncio
    async def test_search_no_results(self):
        """Search with empty results returns appropriate message."""
        import tools.memory_tools as mod
        mock_mem = AsyncMock()
        mock_mem.search.return_value = []
        original = mod._memory
        mod._memory = mock_mem
        try:
            result = await mod.tool_memory_search("nonexistent query")
            assert "No memory results found" in result
        finally:
            mod._memory = original

    @pytest.mark.asyncio
    async def test_search_error_handled(self):
        """Search exception returns error message."""
        import tools.memory_tools as mod
        mock_mem = AsyncMock()
        mock_mem.search.side_effect = RuntimeError("DB connection lost")
        original = mod._memory
        mod._memory = mock_mem
        try:
            result = await mod.tool_memory_search("query")
            assert "Error searching memory" in result
        finally:
            mod._memory = original

    @pytest.mark.asyncio
    async def test_structured_recall_path_with_vector_results(self):
        """With _conn and _config set, structured recall path is exercised."""
        import tools.memory_tools as mod
        mock_mem = AsyncMock()
        mock_mem.search.return_value = [
            {"text": "vector result about nicolas", "score": 0.9, "days_old": 1},
        ]
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)

        class FakeConfig:
            recall_max_facts = 20
            recall_decay_rate = 0.03
            recall_max_dynamic_tokens = 1000

        original = (mod._memory, mod._conn, mod._config)
        mod._memory = mock_mem
        mod._conn = conn
        mod._config = FakeConfig()
        try:
            result = await mod.tool_memory_search("nicolas")
            # Structured recall returns inject_recall() format
            assert "[Memory search]" in result
            mock_mem.search.assert_awaited_once()
        finally:
            mod._memory, mod._conn, mod._config = original
            conn.close()

    @pytest.mark.asyncio
    async def test_structured_recall_empty_returns_fallback(self):
        """Empty structured recall returns EMPTY_RECALL_FALLBACK."""
        import tools.memory_tools as mod
        mock_mem = AsyncMock()
        mock_mem.search.return_value = []
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)

        class FakeConfig:
            recall_max_facts = 20
            recall_decay_rate = 0.03
            recall_max_dynamic_tokens = 1000

        original = (mod._memory, mod._conn, mod._config)
        mod._memory = mock_mem
        mod._conn = conn
        mod._config = FakeConfig()
        try:
            result = await mod.tool_memory_search("xyznonexistent")
            assert "No results found in structured memory" in result
        finally:
            mod._memory, mod._conn, mod._config = original
            conn.close()

    @pytest.mark.asyncio
    async def test_structured_recall_with_facts(self):
        """Structured recall includes facts from the DB."""
        import tools.memory_tools as mod
        mock_mem = AsyncMock()
        mock_mem.search.return_value = []
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)
        # Seed a fact and alias
        conn.execute(
            "INSERT INTO facts (entity, attribute, value, confidence, source_session, accessed_at) "
            "VALUES ('nicolas', 'lives_in', 'Austria', 1.0, 'test', datetime('now'))"
        )
        conn.execute(
            "INSERT INTO entity_aliases (alias, canonical) VALUES ('nicolas', 'nicolas')"
        )
        conn.commit()

        class FakeConfig:
            recall_max_facts = 20
            recall_decay_rate = 0.03
            recall_max_dynamic_tokens = 1000

        original = (mod._memory, mod._conn, mod._config)
        mod._memory = mock_mem
        mod._conn = conn
        mod._config = FakeConfig()
        try:
            result = await mod.tool_memory_search("nicolas")
            assert "[Known facts]" in result
            assert "Austria" in result
        finally:
            mod._memory, mod._conn, mod._config = original
            conn.close()

    @pytest.mark.asyncio
    async def test_structured_recall_fallback_on_error(self):
        """When structured recall raises, falls back to vector search."""
        import tools.memory_tools as mod
        mock_mem = AsyncMock()
        mock_mem.search.return_value = [
            {"source": "test.md", "text": "fallback result", "score": 0.7},
        ]
        # Use a closed connection to trigger an error in structured recall
        conn = sqlite3.connect(":memory:")
        conn.close()

        class FakeConfig:
            recall_max_facts = 20
            recall_decay_rate = 0.03
            recall_max_dynamic_tokens = 1000

        original = (mod._memory, mod._conn, mod._config)
        mod._memory = mock_mem
        mod._conn = conn
        mod._config = FakeConfig()
        try:
            result = await mod.tool_memory_search("query")
            # Should fall back to vector format
            assert "fallback result" in result
        finally:
            mod._memory, mod._conn, mod._config = original


class TestToolMemoryGet:
    """Tests calling REAL tool_memory_get."""

    @pytest.mark.asyncio
    async def test_no_memory_configured(self):
        import tools.memory_tools as mod
        original = mod._memory
        mod._memory = None
        try:
            result = await mod.tool_memory_get("/some/file.py")
            assert "Memory not configured" in result
        finally:
            mod._memory = original

    @pytest.mark.asyncio
    async def test_get_by_path(self):
        """get_file_snippet returns file content."""
        import tools.memory_tools as mod
        mock_mem = AsyncMock()
        mock_mem.get_file_snippet.return_value = "def main():\n    print('hello')"
        original = mod._memory
        mod._memory = mock_mem
        try:
            result = await mod.tool_memory_get("main.py", start_line=0, end_line=10)
            assert "def main" in result
            mock_mem.get_file_snippet.assert_awaited_once_with("main.py", 0, 10)
        finally:
            mod._memory = original

    @pytest.mark.asyncio
    async def test_get_error_handled(self):
        """get_file_snippet exception returns error."""
        import tools.memory_tools as mod
        mock_mem = AsyncMock()
        mock_mem.get_file_snippet.side_effect = FileNotFoundError("not indexed")
        original = mod._memory
        mod._memory = mock_mem
        try:
            result = await mod.tool_memory_get("/nonexistent.py")
            assert "Error retrieving memory" in result
        finally:
            mod._memory = original

    def test_set_memory(self):
        """set_memory updates module global."""
        import tools.memory_tools as mod
        original = mod._memory
        fake = MagicMock()
        mod.set_memory(fake)
        try:
            assert mod._memory is fake
        finally:
            mod._memory = original


# ─── tools/skills_tool.py ───────────────────────────────────────


class TestToolLoadSkill:
    """Tests calling REAL tool_load_skill."""

    def test_no_loader_configured(self):
        """Returns error when skill loader not initialized."""
        import tools.skills_tool as mod
        original = mod._skill_loader
        mod._skill_loader = None
        try:
            result = mod.tool_load_skill("compute-routing")
            assert "Skill loader not initialized" in result
        finally:
            mod._skill_loader = original

    def test_load_valid_skill(self):
        """Loading a valid skill returns its body."""
        import tools.skills_tool as mod
        mock_loader = MagicMock()
        mock_loader.get_skill.return_value = {
            "name": "compute-routing",
            "body": "# Compute Routing\n\nUse Haiku for routine.",
        }
        original = mod._skill_loader
        mod._skill_loader = mock_loader
        try:
            result = mod.tool_load_skill("compute-routing")
            assert "Compute Routing" in result
            assert "Haiku" in result
            mock_loader.get_skill.assert_called_once_with("compute-routing")
        finally:
            mod._skill_loader = original

    def test_load_missing_skill(self):
        """Loading a missing skill returns error with available list."""
        import tools.skills_tool as mod
        mock_loader = MagicMock()
        mock_loader.get_skill.return_value = None
        mock_loader.list_skill_names.return_value = ["compute-routing", "bare-skill"]
        original = mod._skill_loader
        mod._skill_loader = mock_loader
        try:
            result = mod.tool_load_skill("nonexistent")
            assert "not found" in result
            assert "compute-routing" in result
            assert "bare-skill" in result
        finally:
            mod._skill_loader = original

    def test_load_skill_error_handled(self):
        """Exception during skill loading returns error."""
        import tools.skills_tool as mod
        mock_loader = MagicMock()
        mock_loader.get_skill.side_effect = RuntimeError("disk error")
        original = mod._skill_loader
        mod._skill_loader = mock_loader
        try:
            result = mod.tool_load_skill("compute-routing")
            assert "Error loading skill" in result
        finally:
            mod._skill_loader = original

    def test_set_skill_loader(self):
        """set_skill_loader updates module global."""
        import tools.skills_tool as mod
        original = mod._skill_loader
        fake = MagicMock()
        mod.set_skill_loader(fake)
        try:
            assert mod._skill_loader is fake
        finally:
            mod._skill_loader = original
