"""Tests for zero-kill-rate modules: status, memory_read, skills.

Phase 2: Each test calls the REAL function — no reimplemented logic.
"""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

TEST_CLIENT_ID = "test"
TEST_AGENT_ID = "test_agent"

# ─── tools/status.py ─────────────────────────────────────────────


class TestToolSessionStatus:
    """Tests calling REAL tool_session_status."""

    def test_no_session_returns_no_data(self):
        """With nothing configured, returns 'No status data available.'"""
        import tools.status as mod
        original = (mod._session_getter, mod._daemon_start_time)
        mod._session_getter = None
        mod._daemon_start_time = 0.0
        try:
            result = mod.tool_session_status()
            assert "No status data available" in result
        finally:
            mod._session_getter, mod._daemon_start_time = original

    def test_reports_tokens_and_context_pct(self):
        """With a session, reports token count and context percentage."""
        import tools.status as mod
        original = (mod._session_getter, mod._daemon_start_time, mod.MAX_CONTEXT_TOKENS)
        session = MagicMock()
        session.last_input_tokens = 50000
        session.messages = [{"role": "user", "content": "hi"}, {"role": "agent", "text": "hello"}]
        session.compaction_count = 2
        mod._session_getter = lambda: session
        mod._daemon_start_time = 0.0
        mod.MAX_CONTEXT_TOKENS = 200000
        try:
            result = mod.tool_session_status()
            assert "50,000 tokens" in result
            assert "25%" in result
            assert "Messages: 2" in result
            assert "Compactions: 2" in result
        finally:
            mod._session_getter, mod._daemon_start_time, mod.MAX_CONTEXT_TOKENS = original

    def test_reports_uptime(self):
        """Reports daemon uptime when start_time is set."""
        import tools.status as mod
        original = (mod._session_getter, mod._daemon_start_time)
        mod._session_getter = None
        mod._daemon_start_time = time.time() - 3700  # ~1h 1m
        try:
            result = mod.tool_session_status()
            assert "Daemon uptime:" in result
            assert "1h" in result
        finally:
            mod._session_getter, mod._daemon_start_time = original

    def test_configure_sets_max_context_tokens(self):
        """configure() with max_context_tokens updates the module global."""
        import tools.status as mod
        original = mod.MAX_CONTEXT_TOKENS
        mod.configure(max_context_tokens=300000)
        try:
            assert mod.MAX_CONTEXT_TOKENS == 300000
        finally:
            mod.MAX_CONTEXT_TOKENS = original

    def test_session_getter_callback(self):
        """session_getter callback replaces set_current_session."""
        import tools.status as mod
        original = mod._session_getter
        fake_session = MagicMock()
        fake_session.last_input_tokens = 1000
        fake_session.messages = []
        fake_session.compaction_count = 0
        mod._session_getter = lambda: fake_session
        try:
            result = mod.tool_session_status()
            assert "1,000 tokens" in result
        finally:
            mod._session_getter = original


# ─── tools/memory_read.py ───────────────────────────────────────


class TestToolMemorySearch:
    """Tests calling REAL tool_memory_search."""

    @pytest.mark.asyncio
    async def test_no_memory_configured(self):
        """Returns error when memory not configured."""
        import tools.memory_read as mod
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
        import tools.memory_read as mod
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
        import tools.memory_read as mod
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
        import tools.memory_read as mod
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
    async def test_structured_recall_path_with_vector_results(self, pool):
        """With _pool and _config set, structured recall path is exercised."""
        import tools.memory_read as mod
        mock_mem = AsyncMock()
        mock_mem.search.return_value = [
            {"text": "vector result about nicolas", "score": 0.9, "days_old": 1},
        ]

        class FakeConfig:
            recall_max_facts = 20
            recall_decay_rate = 0.03
            recall_max_dynamic_tokens = 1000

        original = (mod._memory, mod._pool, mod._client_id, mod._agent_id, mod._config)
        mod._memory = mock_mem
        mod._pool = pool
        mod._client_id = TEST_CLIENT_ID
        mod._agent_id = TEST_AGENT_ID
        mod._config = FakeConfig()
        try:
            result = await mod.tool_memory_search("nicolas")
            # Structured recall returns inject_recall() format
            assert "[Memory search]" in result
            mock_mem.search.assert_awaited_once()
        finally:
            mod._memory, mod._pool, mod._client_id, mod._agent_id, mod._config = original

    @pytest.mark.asyncio
    async def test_structured_recall_empty_returns_fallback(self, pool):
        """Empty structured recall returns EMPTY_RECALL_FALLBACK."""
        import tools.memory_read as mod
        mock_mem = AsyncMock()
        mock_mem.search.return_value = []

        class FakeConfig:
            recall_max_facts = 20
            recall_decay_rate = 0.03
            recall_max_dynamic_tokens = 1000

        original = (mod._memory, mod._pool, mod._client_id, mod._agent_id, mod._config)
        mod._memory = mock_mem
        mod._pool = pool
        mod._client_id = TEST_CLIENT_ID
        mod._agent_id = TEST_AGENT_ID
        mod._config = FakeConfig()
        try:
            result = await mod.tool_memory_search("xyznonexistent")
            assert "No results found in structured memory" in result
        finally:
            mod._memory, mod._pool, mod._client_id, mod._agent_id, mod._config = original

    @pytest.mark.asyncio
    async def test_structured_recall_with_facts(self, pool):
        """Structured recall includes facts from the DB."""
        import tools.memory_read as mod
        mock_mem = AsyncMock()
        mock_mem.search.return_value = []
        # Seed a fact and alias via asyncpg
        await pool.execute(
            "INSERT INTO knowledge.facts "
            "(client_id, agent_id, entity, attribute, value, confidence, source_session, accessed_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, now())",
            TEST_CLIENT_ID, TEST_AGENT_ID,
            "nicolas", "lives_in", "Austria", 1.0, "test",
        )
        await pool.execute(
            "INSERT INTO knowledge.entity_aliases (client_id, agent_id, alias, canonical) "
            "VALUES ($1, $2, $3, $4)",
            TEST_CLIENT_ID, TEST_AGENT_ID, "nicolas", "nicolas",
        )

        class FakeConfig:
            recall_max_facts = 20
            recall_decay_rate = 0.03
            recall_max_dynamic_tokens = 1000

        original = (mod._memory, mod._pool, mod._client_id, mod._agent_id, mod._config)
        mod._memory = mock_mem
        mod._pool = pool
        mod._client_id = TEST_CLIENT_ID
        mod._agent_id = TEST_AGENT_ID
        mod._config = FakeConfig()
        try:
            result = await mod.tool_memory_search("nicolas")
            assert "[Known facts]" in result
            assert "Austria" in result
        finally:
            mod._memory, mod._pool, mod._client_id, mod._agent_id, mod._config = original

    @pytest.mark.asyncio
    async def test_structured_recall_fallback_on_error(self):
        """When structured recall raises, falls back to vector search."""
        import tools.memory_read as mod
        mock_mem = AsyncMock()
        mock_mem.search.return_value = [
            {"source": "test.md", "text": "fallback result", "score": 0.7},
        ]
        # Mock pool that raises on every async call to trigger recall failure
        bad_pool = MagicMock()
        bad_pool.fetch = AsyncMock(side_effect=Exception("connection error"))
        bad_pool.fetchrow = AsyncMock(side_effect=Exception("connection error"))
        bad_pool.fetchval = AsyncMock(side_effect=Exception("connection error"))

        class FakeConfig:
            recall_max_facts = 20
            recall_decay_rate = 0.03
            recall_max_dynamic_tokens = 1000

        original = (mod._memory, mod._pool, mod._client_id, mod._agent_id, mod._config)
        mod._memory = mock_mem
        mod._pool = bad_pool
        mod._client_id = TEST_CLIENT_ID
        mod._agent_id = TEST_AGENT_ID
        mod._config = FakeConfig()
        try:
            result = await mod.tool_memory_search("query")
            # Should fall back to vector format
            assert "fallback result" in result
        finally:
            mod._memory, mod._pool, mod._client_id, mod._agent_id, mod._config = original


class TestToolMemoryGet:
    """Tests calling REAL tool_memory_get."""

    @pytest.mark.asyncio
    async def test_no_memory_configured(self):
        import tools.memory_read as mod
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
        import tools.memory_read as mod
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
        import tools.memory_read as mod
        mock_mem = AsyncMock()
        mock_mem.get_file_snippet.side_effect = FileNotFoundError("not indexed")
        original = mod._memory
        mod._memory = mock_mem
        try:
            result = await mod.tool_memory_get("/nonexistent.py")
            assert "Error retrieving memory" in result
        finally:
            mod._memory = original

    def test_configure_memory(self):
        """configure(memory=...) updates module global."""
        import tools.memory_read as mod
        original = mod._memory
        fake = MagicMock()
        mod.configure(memory=fake)
        try:
            assert mod._memory is fake
        finally:
            mod._memory = original


# ─── skills.py ──────────────────────────────────────────────────


class TestToolLoadSkill:
    """Tests calling REAL tool_load_skill."""

    def test_no_loader_configured(self):
        """Returns error when skill loader not initialized."""
        import skills as mod
        original = mod._skill_loader
        mod._skill_loader = None
        try:
            result = mod.tool_load_skill("compute-routing")
            assert "Skill loader not initialized" in result
        finally:
            mod._skill_loader = original

    def test_load_valid_skill(self):
        """Loading a valid skill returns its body."""
        import skills as mod
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
        import skills as mod
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

    def test_configure_skill_loader(self):
        """configure(skill_loader=...) updates module global."""
        import skills as mod
        original = mod._skill_loader
        fake = MagicMock()
        mod.configure(skill_loader=fake)
        try:
            assert mod._skill_loader is fake
        finally:
            mod._skill_loader = original
