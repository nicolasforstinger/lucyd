"""Tests for tools/memory_write.py — memory_write, memory_forget, record_episode."""

import pytest

from tools.memory_write import (
    TOOLS,
    configure,
    handle_memory_forget,
    handle_memory_write,
)

# Match conftest.py constants — defined locally to avoid importing conftest as a module.


@pytest.fixture
async def mem_pool(pool):
    """asyncpg pool configured into the memory_write module."""
    configure(pool=pool,)
    yield pool
    configure(pool=None,)


@pytest.fixture
async def seeded_pool(mem_pool):
    """Pool with pre-existing facts and aliases for update/forget tests."""
    await mem_pool.execute(
        "INSERT INTO knowledge.facts (entity, attribute, value, confidence, source_session) "
        "VALUES ($1, $2, $3, $4, $5)",
        "nicolas", "lives_in", "Austria", 0.9, "test",
    )
    await mem_pool.execute(
        "INSERT INTO knowledge.facts (entity, attribute, value, confidence, source_session) "
        "VALUES ($1, $2, $3, $4, $5)",
        "lucy", "role", "companion", 1.0, "test",
    )
    # Alias for resolution test
    await mem_pool.execute(
        "INSERT INTO knowledge.entity_aliases (alias, canonical) "
        "VALUES ($1, $2)",
        "nic", "nicolas",
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
            "WHERE TRUE "
            "AND entity = 'nicolas' AND attribute = 'favorite_color' AND invalidated_at IS NULL",
            )
        assert row["confidence"] == 1.0
        assert row["source_session"] == "agent"

    @pytest.mark.asyncio
    async def test_deduplicates_same_value(self, seeded_pool):
        result = await handle_memory_write("nicolas", "lives_in", "Austria")
        assert "Already known" in result

        count = await seeded_pool.fetchval(
            "SELECT COUNT(*) FROM knowledge.facts "
            "WHERE TRUE "
            "AND entity = 'nicolas' AND attribute = 'lives_in' AND invalidated_at IS NULL",
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
            "WHERE value = 'Austria'",
            )
        assert old["invalidated_at"] is not None

        # New fact active
        new = await seeded_pool.fetchrow(
            "SELECT value FROM knowledge.facts "
            "WHERE TRUE "
            "AND entity = 'nicolas' AND attribute = 'lives_in' AND invalidated_at IS NULL",
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
        configure(pool=None,)
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
            "WHERE TRUE "
            "AND entity = 'nicolas' AND attribute = 'lives_in'",
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
        configure(pool=None,)
        result = await handle_memory_forget("test", "attr")
        assert "Error" in result


# --- user-entity pin (entity-alias-cycle-corruption) ---------------------


class _StubConfig:
    """Minimal stand-in: configure() only reads config.user_name."""

    def __init__(self, user_name: str) -> None:
        self.user_name = user_name


@pytest.fixture
async def cyclic_user_pool(mem_pool):
    """A fact under the user entity plus a nicolas <-> nicolas_forstinger
    alias 2-cycle, mirroring the corrupted live state. Without the pin,
    _resolve_entity('nicolas') would hop to nicolas_forstinger (no fact)."""
    await mem_pool.execute(
        "INSERT INTO knowledge.facts (entity, attribute, value, confidence, source_session) "
        "VALUES ($1, $2, $3, $4, $5)",
        "nicolas", "streaming_gear_arrived", "2026-05-28", 0.9, "test",
    )
    await mem_pool.execute(
        "INSERT INTO knowledge.entity_aliases (alias, canonical) VALUES "
        "('nicolas', 'nicolas_forstinger'), ('nicolas_forstinger', 'nicolas')",
    )
    return mem_pool


class TestUserEntityPin:
    @pytest.mark.asyncio
    async def test_forget_user_fact_bypasses_cyclic_alias(self, cyclic_user_pool):
        configure(pool=cyclic_user_pool, config=_StubConfig("nicolas"))
        try:
            result = await handle_memory_forget("nicolas", "streaming_gear_arrived")
            assert "Forgotten: nicolas.streaming_gear_arrived" in result
            row = await cyclic_user_pool.fetchrow(
                "SELECT invalidated_at FROM knowledge.facts "
                "WHERE entity = 'nicolas' AND attribute = 'streaming_gear_arrived'",
            )
            assert row["invalidated_at"] is not None
        finally:
            configure(pool=None, config=None)

    @pytest.mark.asyncio
    async def test_write_user_fact_lands_on_user_entity(self, cyclic_user_pool):
        configure(pool=cyclic_user_pool, config=_StubConfig("nicolas"))
        try:
            result = await handle_memory_write("nicolas", "favorite_color", "black")
            assert "nicolas.favorite_color" in result
            exists = await cyclic_user_pool.fetchval(
                "SELECT 1 FROM knowledge.facts "
                "WHERE entity = 'nicolas' AND attribute = 'favorite_color' "
                "AND invalidated_at IS NULL",
            )
            assert exists == 1
        finally:
            configure(pool=None, config=None)

    @pytest.mark.asyncio
    async def test_non_user_entity_still_alias_resolves(self, cyclic_user_pool):
        # The pin must not interfere with ordinary alias resolution for other
        # entities — 'nic' still resolves through the alias table to 'nicolas'.
        await cyclic_user_pool.execute(
            "INSERT INTO knowledge.entity_aliases (alias, canonical) VALUES ('nic', 'nicolas')",
        )
        configure(pool=cyclic_user_pool, config=_StubConfig("nicolas"))
        try:
            result = await handle_memory_write("nic", "age", "30")
            assert "nicolas.age" in result
        finally:
            configure(pool=None, config=None)


# --- Tool Definitions ----------------------------------------------------


class TestToolDefinitions:
    def test_three_tools_registered(self):
        assert len(TOOLS) == 3

    def test_tool_names(self):
        names = {t.name for t in TOOLS}
        assert names == {"memory_write", "memory_forget", "record_episode"}

    def test_tools_have_functions(self):
        for tool in TOOLS:
            assert callable(tool.function)

    def test_required_fields(self):
        from tools import ToolSpec
        for tool in TOOLS:
            assert isinstance(tool, ToolSpec)
