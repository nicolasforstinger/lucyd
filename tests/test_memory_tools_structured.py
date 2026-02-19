"""Tests for tools/structured_memory.py — memory_write, memory_forget, commitment_update."""

import sqlite3

import pytest

from memory_schema import ensure_schema
from tools.structured_memory import (
    TOOLS,
    configure,
    handle_commitment_update,
    handle_memory_forget,
    handle_memory_write,
)


@pytest.fixture
def mem_conn(tmp_path):
    """SQLite connection with structured memory schema, configured into the module."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    configure(conn)
    yield conn
    # Reset module state
    configure(None)
    conn.close()


@pytest.fixture
def seeded_conn(mem_conn):
    """DB with pre-existing facts and commitments for update/forget tests."""
    mem_conn.execute(
        "INSERT INTO facts (entity, attribute, value, confidence, source_session) "
        "VALUES ('nicolas', 'lives_in', 'Austria', 0.9, 'test')"
    )
    mem_conn.execute(
        "INSERT INTO facts (entity, attribute, value, confidence, source_session) "
        "VALUES ('lucy', 'role', 'companion', 1.0, 'test')"
    )
    # Alias for resolution test
    mem_conn.execute(
        "INSERT INTO entity_aliases (alias, canonical) VALUES ('nic', 'nicolas')"
    )
    # Open commitment
    mem_conn.execute(
        "INSERT INTO commitments (who, what, deadline, status) "
        "VALUES ('nicolas', 'review PR', '2026-02-20', 'open')"
    )
    # Done commitment (should not be re-updatable)
    mem_conn.execute(
        "INSERT INTO commitments (who, what, deadline, status) "
        "VALUES ('nicolas', 'old task', '2026-01-01', 'done')"
    )
    mem_conn.commit()
    return mem_conn


# ─── memory_write ────────────────────────────────────────────────


class TestMemoryWrite:
    @pytest.mark.asyncio
    async def test_creates_new_fact(self, mem_conn):
        result = await handle_memory_write("nicolas", "favorite_color", "blue")
        assert "Stored:" in result
        assert "nicolas.favorite_color" in result

        row = mem_conn.execute(
            "SELECT confidence, source_session FROM facts WHERE entity = 'nicolas' "
            "AND attribute = 'favorite_color' AND invalidated_at IS NULL"
        ).fetchone()
        assert row["confidence"] == 1.0
        assert row["source_session"] == "agent"

    @pytest.mark.asyncio
    async def test_deduplicates_same_value(self, seeded_conn):
        result = await handle_memory_write("nicolas", "lives_in", "Austria")
        assert "Already known" in result

        # Only one current fact for this entity+attribute
        count = seeded_conn.execute(
            "SELECT COUNT(*) FROM facts WHERE entity = 'nicolas' "
            "AND attribute = 'lives_in' AND invalidated_at IS NULL"
        ).fetchone()[0]
        assert count == 1

    @pytest.mark.asyncio
    async def test_updates_changed_value(self, seeded_conn):
        result = await handle_memory_write("nicolas", "lives_in", "Germany")
        assert "Updated:" in result
        assert "was: Austria" in result

        # Old fact invalidated
        old = seeded_conn.execute(
            "SELECT invalidated_at FROM facts WHERE value = 'Austria'"
        ).fetchone()
        assert old["invalidated_at"] is not None

        # New fact active
        new = seeded_conn.execute(
            "SELECT value FROM facts WHERE entity = 'nicolas' "
            "AND attribute = 'lives_in' AND invalidated_at IS NULL"
        ).fetchone()
        assert new["value"] == "Germany"

    @pytest.mark.asyncio
    async def test_resolves_alias(self, seeded_conn):
        result = await handle_memory_write("nic", "age", "30")
        assert "nicolas.age" in result  # resolved from "nic" alias

    @pytest.mark.asyncio
    async def test_normalizes_entity(self, mem_conn):
        result = await handle_memory_write("Uncle Charles", "relation", "uncle")
        assert "uncle_charles" in result

    @pytest.mark.asyncio
    async def test_not_configured_returns_error(self):
        configure(None)
        result = await handle_memory_write("test", "attr", "val")
        assert "Error" in result


# ─── memory_forget ───────────────────────────────────────────────


class TestMemoryForget:
    @pytest.mark.asyncio
    async def test_sets_invalidated_at(self, seeded_conn):
        result = await handle_memory_forget("nicolas", "lives_in")
        assert "Forgotten:" in result

        row = seeded_conn.execute(
            "SELECT invalidated_at FROM facts WHERE entity = 'nicolas' "
            "AND attribute = 'lives_in'"
        ).fetchone()
        assert row["invalidated_at"] is not None

    @pytest.mark.asyncio
    async def test_no_match_returns_not_found(self, seeded_conn):
        result = await handle_memory_forget("nicolas", "nonexistent_attr")
        assert "No current fact found" in result

    @pytest.mark.asyncio
    async def test_resolves_alias(self, seeded_conn):
        result = await handle_memory_forget("nic", "lives_in")
        assert "Forgotten:" in result
        assert "nicolas" in result

    @pytest.mark.asyncio
    async def test_not_configured_returns_error(self):
        configure(None)
        result = await handle_memory_forget("test", "attr")
        assert "Error" in result


# ─── commitment_update ───────────────────────────────────────────


class TestCommitmentUpdate:
    @pytest.mark.asyncio
    async def test_changes_status(self, seeded_conn):
        # Get the open commitment's ID
        row = seeded_conn.execute(
            "SELECT id FROM commitments WHERE status = 'open'"
        ).fetchone()
        cid = row["id"]

        result = await handle_commitment_update(cid, "done")
        assert f"#{cid}" in result
        assert "done" in result

        updated = seeded_conn.execute(
            "SELECT status FROM commitments WHERE id = ?", (cid,)
        ).fetchone()
        assert updated["status"] == "done"

    @pytest.mark.asyncio
    async def test_no_match_returns_not_found(self, seeded_conn):
        result = await handle_commitment_update(9999, "done")
        assert "No open commitment" in result

    @pytest.mark.asyncio
    async def test_only_affects_open(self, seeded_conn):
        """Cannot re-update an already-closed commitment."""
        row = seeded_conn.execute(
            "SELECT id FROM commitments WHERE status = 'done'"
        ).fetchone()
        cid = row["id"]

        result = await handle_commitment_update(cid, "cancelled")
        assert "No open commitment" in result

        # Status unchanged
        check = seeded_conn.execute(
            "SELECT status FROM commitments WHERE id = ?", (cid,)
        ).fetchone()
        assert check["status"] == "done"

    @pytest.mark.asyncio
    async def test_not_configured_returns_error(self):
        configure(None)
        result = await handle_commitment_update(1, "done")
        assert "Error" in result


# ─── Tool Definitions ────────────────────────────────────────────


class TestToolDefinitions:
    def test_three_tools_registered(self):
        assert len(TOOLS) == 3

    def test_tool_names(self):
        names = {t["name"] for t in TOOLS}
        assert names == {"memory_write", "memory_forget", "commitment_update"}

    def test_tools_have_functions(self):
        for tool in TOOLS:
            assert callable(tool["function"])

    def test_required_fields(self):
        for tool in TOOLS:
            assert "name" in tool
            assert "description" in tool
            assert "input_schema" in tool
            assert "function" in tool
