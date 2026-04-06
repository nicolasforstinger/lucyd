"""Tests for tools/memory_write.py — memory_write, memory_forget, commitment_update."""

import pytest

from tools.memory_write import (
    TOOLS,
    configure,
    handle_commitment_update,
    handle_memory_forget,
    handle_memory_write,
)

# Match conftest.py constants — defined locally to avoid importing conftest as a module.
TEST_CLIENT_ID = "test"
TEST_AGENT_ID = "test_agent"


@pytest.fixture
async def mem_pool(pool):
    """asyncpg pool configured into the memory_write module."""
    configure(pool=pool, client_id=TEST_CLIENT_ID, agent_id=TEST_AGENT_ID)
    yield pool
    configure(pool=None, client_id="", agent_id="")


@pytest.fixture
async def seeded_pool(mem_pool):
    """Pool with pre-existing facts, aliases, and commitments for update/forget tests."""
    await mem_pool.execute(
        "INSERT INTO knowledge.facts (client_id, agent_id, entity, attribute, value, confidence, source_session) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7)",
        TEST_CLIENT_ID, TEST_AGENT_ID, "nicolas", "lives_in", "Austria", 0.9, "test",
    )
    await mem_pool.execute(
        "INSERT INTO knowledge.facts (client_id, agent_id, entity, attribute, value, confidence, source_session) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7)",
        TEST_CLIENT_ID, TEST_AGENT_ID, "lucy", "role", "companion", 1.0, "test",
    )
    # Alias for resolution test
    await mem_pool.execute(
        "INSERT INTO knowledge.entity_aliases (client_id, agent_id, alias, canonical) "
        "VALUES ($1, $2, $3, $4)",
        TEST_CLIENT_ID, TEST_AGENT_ID, "nic", "nicolas",
    )
    # Open commitment
    await mem_pool.execute(
        "INSERT INTO knowledge.commitments (client_id, agent_id, who, what, deadline, status) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        TEST_CLIENT_ID, TEST_AGENT_ID, "nicolas", "review PR", "2026-02-20", "open",
    )
    # Done commitment (should not be re-updatable)
    await mem_pool.execute(
        "INSERT INTO knowledge.commitments (client_id, agent_id, who, what, deadline, status) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        TEST_CLIENT_ID, TEST_AGENT_ID, "nicolas", "old task", "2026-01-01", "done",
    )
    return mem_pool


# --- memory_write --------------------------------------------------------


class TestMemoryWrite:
    @pytest.mark.asyncio
    async def test_creates_new_fact(self, mem_pool):
        result = await handle_memory_write("nicolas", "favorite_color", "blue")
        assert "Stored:" in result
        assert "nicolas.favorite_color" in result

        row = await mem_pool.fetchrow(
            "SELECT confidence, source_session FROM knowledge.facts "
            "WHERE client_id = $1 AND agent_id = $2 "
            "AND entity = 'nicolas' AND attribute = 'favorite_color' AND invalidated_at IS NULL",
            TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert row["confidence"] == 1.0
        assert row["source_session"] == "agent"

    @pytest.mark.asyncio
    async def test_deduplicates_same_value(self, seeded_pool):
        result = await handle_memory_write("nicolas", "lives_in", "Austria")
        assert "Already known" in result

        count = await seeded_pool.fetchval(
            "SELECT COUNT(*) FROM knowledge.facts "
            "WHERE client_id = $1 AND agent_id = $2 "
            "AND entity = 'nicolas' AND attribute = 'lives_in' AND invalidated_at IS NULL",
            TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert count == 1

    @pytest.mark.asyncio
    async def test_updates_changed_value(self, seeded_pool):
        result = await handle_memory_write("nicolas", "lives_in", "Germany")
        assert "Updated:" in result
        assert "was: Austria" in result

        # Old fact invalidated
        old = await seeded_pool.fetchrow(
            "SELECT invalidated_at FROM knowledge.facts "
            "WHERE client_id = $1 AND agent_id = $2 AND value = 'Austria'",
            TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert old["invalidated_at"] is not None

        # New fact active
        new = await seeded_pool.fetchrow(
            "SELECT value FROM knowledge.facts "
            "WHERE client_id = $1 AND agent_id = $2 "
            "AND entity = 'nicolas' AND attribute = 'lives_in' AND invalidated_at IS NULL",
            TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert new["value"] == "Germany"

    @pytest.mark.asyncio
    async def test_resolves_alias(self, seeded_pool):
        result = await handle_memory_write("nic", "age", "30")
        assert "nicolas.age" in result  # resolved from "nic" alias

    @pytest.mark.asyncio
    async def test_normalizes_entity(self, mem_pool):
        result = await handle_memory_write("Uncle Charles", "relation", "uncle")
        assert "uncle_charles" in result

    @pytest.mark.asyncio
    async def test_not_configured_returns_error(self):
        configure(pool=None, client_id="", agent_id="")
        result = await handle_memory_write("test", "attr", "val")
        assert "Error" in result


# --- memory_forget -------------------------------------------------------


class TestMemoryForget:
    @pytest.mark.asyncio
    async def test_sets_invalidated_at(self, seeded_pool):
        result = await handle_memory_forget("nicolas", "lives_in")
        assert "Forgotten:" in result

        row = await seeded_pool.fetchrow(
            "SELECT invalidated_at FROM knowledge.facts "
            "WHERE client_id = $1 AND agent_id = $2 "
            "AND entity = 'nicolas' AND attribute = 'lives_in'",
            TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert row["invalidated_at"] is not None

    @pytest.mark.asyncio
    async def test_no_match_returns_not_found(self, seeded_pool):
        result = await handle_memory_forget("nicolas", "nonexistent_attr")
        assert "No current fact found" in result

    @pytest.mark.asyncio
    async def test_resolves_alias(self, seeded_pool):
        result = await handle_memory_forget("nic", "lives_in")
        assert "Forgotten:" in result
        assert "nicolas" in result

    @pytest.mark.asyncio
    async def test_not_configured_returns_error(self):
        configure(pool=None, client_id="", agent_id="")
        result = await handle_memory_forget("test", "attr")
        assert "Error" in result


# --- commitment_update ---------------------------------------------------


class TestCommitmentUpdate:
    @pytest.mark.asyncio
    async def test_changes_status(self, seeded_pool):
        row = await seeded_pool.fetchrow(
            "SELECT id FROM knowledge.commitments "
            "WHERE client_id = $1 AND agent_id = $2 AND status = 'open'",
            TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        cid = row["id"]

        result = await handle_commitment_update(cid, "done")
        assert f"#{cid}" in result
        assert "done" in result

        updated = await seeded_pool.fetchrow(
            "SELECT status FROM knowledge.commitments "
            "WHERE client_id = $1 AND agent_id = $2 AND id = $3",
            TEST_CLIENT_ID, TEST_AGENT_ID, cid,
        )
        assert updated["status"] == "done"

    @pytest.mark.asyncio
    async def test_no_match_returns_not_found(self, seeded_pool):
        result = await handle_commitment_update(9999, "done")
        assert "No open commitment" in result

    @pytest.mark.asyncio
    async def test_only_affects_open(self, seeded_pool):
        """Cannot re-update an already-closed commitment."""
        row = await seeded_pool.fetchrow(
            "SELECT id FROM knowledge.commitments "
            "WHERE client_id = $1 AND agent_id = $2 AND status = 'done'",
            TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        cid = row["id"]

        result = await handle_commitment_update(cid, "cancelled")
        assert "No open commitment" in result

        # Status unchanged
        check = await seeded_pool.fetchrow(
            "SELECT status FROM knowledge.commitments "
            "WHERE client_id = $1 AND agent_id = $2 AND id = $3",
            TEST_CLIENT_ID, TEST_AGENT_ID, cid,
        )
        assert check["status"] == "done"

    @pytest.mark.asyncio
    async def test_not_configured_returns_error(self):
        configure(pool=None, client_id="", agent_id="")
        result = await handle_commitment_update(1, "done")
        assert "Error" in result


# --- Tool Definitions ----------------------------------------------------


class TestToolDefinitions:
    def test_three_tools_registered(self):
        assert len(TOOLS) == 3

    def test_tool_names(self):
        names = {t.name for t in TOOLS}
        assert names == {"memory_write", "memory_forget", "commitment_update"}

    def test_tools_have_functions(self):
        for tool in TOOLS:
            assert callable(tool.function)

    def test_required_fields(self):
        from tools import ToolSpec
        for tool in TOOLS:
            assert isinstance(tool, ToolSpec)
