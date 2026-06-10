"""Tests for consolidation.py — state tracking, serializer, fact upsert, episode storage."""

import pytest

from memory import _normalize_entity
from memory import resolve_entity as _resolve_entity
from consolidation import (
    get_unprocessed_range,
    serialize_messages,
    store_episode,
    update_consolidation_state,
    upsert_fact,
)


# ─── State Tracking ──────────────────────────────────────────────


class TestGetUnprocessedRange:
    @pytest.mark.asyncio
    async def test_first_run_processes_everything(self, pool):
        messages = [{"role": "user"}, {"role": "agent"}] * 3
        start, end = await get_unprocessed_range("sess1", messages, 0, pool)
        assert start == 0
        assert end == 6

    @pytest.mark.asyncio
    async def test_returns_last_to_n_after_previous(self, pool):
        messages = [{"role": "user"}] * 10
        await update_consolidation_state("sess1", 0, 6, pool)

        start, end = await get_unprocessed_range("sess1", messages, 0, pool)
        assert start == 6
        assert end == 10

    @pytest.mark.asyncio
    async def test_after_compaction_skips_summary(self, pool):
        # compaction_count=0 was processed, now compaction_count=1
        await update_consolidation_state("sess1", 0, 10, pool)
        messages = [{"role": "agent"}] * 5  # index 0 = summary

        start, end = await get_unprocessed_range("sess1", messages, 1, pool)
        assert start == 1
        assert end == 5

    @pytest.mark.asyncio
    async def test_no_new_messages(self, pool):
        messages = [{"role": "user"}] * 5
        await update_consolidation_state("sess1", 0, 5, pool)

        start, end = await get_unprocessed_range("sess1", messages, 0, pool)
        assert start == 0
        assert end == 0

    @pytest.mark.asyncio
    async def test_advance_to_harvested_end_leaves_appended_tail(self, pool):
        """The harvest advances the watermark to end_idx, not a re-read len.

        A harvester reads [0, 4) and advances to 4; if a user turn appended two
        messages meanwhile (len now 6), the tail [4, 6) must remain unprocessed
        for the next harvest — advancing to len would skip it. Guards the
        double-harvest-race fix (operations.harvest_conversation / handle_maintain).
        """
        await update_consolidation_state("sess1", 0, 4, pool)  # harvested [0,4)
        grown = [{"role": "user"}] * 6  # two appended after the harvest read
        start, end = await get_unprocessed_range("sess1", grown, 0, pool)
        assert (start, end) == (4, 6)


class TestUpdateConsolidationState:
    @pytest.mark.asyncio
    async def test_insert_new_state(self, pool):
        await update_consolidation_state("sess1", 0, 10, pool)
        row = await pool.fetchrow(
            "SELECT * FROM knowledge.consolidation_state "
            "WHERE session_id = $1",
            "sess1",
        )
        assert row["last_message_count"] == 10
        assert row["last_compaction_count"] == 0

    @pytest.mark.asyncio
    async def test_replace_existing_state(self, pool):
        await update_consolidation_state("sess1", 0, 5, pool)
        await update_consolidation_state("sess1", 0, 10, pool)
        rows = await pool.fetch(
            "SELECT * FROM knowledge.consolidation_state "
            "WHERE session_id = $1",
            "sess1",
        )
        assert len(rows) == 1
        assert rows[0]["last_message_count"] == 10

    @pytest.mark.asyncio
    async def test_new_compaction_replaces_state(self, pool):
        """Single PK on session_id — new compaction replaces the row."""
        await update_consolidation_state("sess1", 0, 10, pool)
        await update_consolidation_state("sess1", 1, 5, pool)
        row = await pool.fetchrow(
            "SELECT * FROM knowledge.consolidation_state "
            "WHERE session_id = $1",
            "sess1",
        )
        assert row["last_compaction_count"] == 1
        assert row["last_message_count"] == 5


# ─── Serializer ──────────────────────────────────────────────────


class TestSerializeMessages:
    def test_basic_serialization(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "agent", "text": "world"},
        ]
        result = serialize_messages(messages, 0, 2)
        assert "user: hello" in result
        assert "agent: world" in result

    def test_respects_max_chars_truncates_at_budget(self):
        messages = [
            {"role": "user", "content": "A" * 100},
            {"role": "user", "content": "B" * 100},
            {"role": "user", "content": "C" * 100},
        ]
        result = serialize_messages(messages, 0, 3, max_chars=150)
        # Serializer processes front-to-back and stops at budget;
        # first message fits ("user: " + "A"*100 = 106 chars), third is never reached
        assert "A" * 100 in result
        assert "C" * 100 not in result
        assert len(result) <= 150

    def test_skips_tool_results(self):
        messages = [
            {"role": "tool_result", "results": [
                {"content": "X" * 5000}
            ]},
        ]
        result = serialize_messages(messages, 0, 1)
        assert result == ""  # tool_result messages are excluded from serialization

    def test_empty_range_returns_empty(self):
        messages = [{"role": "user", "content": "test"}]
        assert serialize_messages(messages, 0, 0) == ""
        assert serialize_messages(messages, 5, 3) == ""

    def test_handles_user_content_list(self):
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "first"},
                {"type": "image", "source": "base64"},
                {"type": "text", "text": "second"},
            ]},
        ]
        result = serialize_messages(messages, 0, 1)
        assert "first" in result
        assert "second" in result
        assert "base64" not in result

    def test_serializes_assistant_text_field(self):
        messages = [
            {"role": "agent", "text": "thinking about tools",
             "tool_calls": [{"name": "web_search", "arguments": {"query": "test"}}]},
        ]
        result = serialize_messages(messages, 0, 1)
        # Serializer uses the "text" field for assistant messages, not tool_calls
        assert "thinking about tools" in result


# ─── Helpers ─────────────────────────────────────────────────────


class TestHelpers:
    def test_normalize_entity(self):
        assert _normalize_entity("Uncle Charles") == "uncle_charles"
        assert _normalize_entity("  Lucy  ") == "lucy"
        assert _normalize_entity("NICOLAS") == "nicolas"

    @pytest.mark.asyncio
    async def test_resolve_entity_with_alias(self, pool):
        await pool.execute(
            "INSERT INTO knowledge.entity_aliases (alias, canonical) "
            "VALUES ($1, $2)",
            "alex_johnson", "alex",
        )
        assert await _resolve_entity("alex_johnson", pool) == "alex"

    @pytest.mark.asyncio
    async def test_resolve_entity_no_alias(self, pool):
        assert await _resolve_entity("unknown_entity", pool) == "unknown_entity"


# ─── Fact Upsert ─────────────────────────────────────────────────


class TestUpsertFact:
    @pytest.mark.asyncio
    async def test_new_fact_inserted(self, pool):
        result = await upsert_fact("alex", "role", "engineer", pool)
        assert result == "new"
        row = await pool.fetchrow(
            "SELECT value FROM knowledge.facts "
            "WHERE entity = 'alex' AND attribute = 'role' AND invalidated_at IS NULL",
        )
        assert row["value"] == "engineer"

    @pytest.mark.asyncio
    async def test_same_value_unchanged(self, pool):
        await upsert_fact("alex", "role", "engineer", pool)
        result = await upsert_fact("alex", "role", "engineer", pool)
        assert result == "unchanged"

    @pytest.mark.asyncio
    async def test_changed_value_invalidates_old(self, pool):
        await upsert_fact("alex", "role", "engineer", pool)
        result = await upsert_fact("alex", "role", "manager", pool)
        assert result == "updated"
        live = await pool.fetch(
            "SELECT value FROM knowledge.facts "
            "WHERE entity = 'alex' AND attribute = 'role' AND invalidated_at IS NULL",
        )
        assert len(live) == 1
        assert live[0]["value"] == "manager"


# ─── Episode Storage ─────────────────────────────────────────────


def _episode(**kw):
    base = {"topics": [], "decisions": [],
            "summary": "", "emotional_tone": "neutral"}
    base.update(kw)
    return {"episode": base}


class TestStoreEpisode:
    @pytest.mark.asyncio
    async def test_valid_episode_stored(self, pool):
        eid = await store_episode(
            _episode(topics=["work"], summary="Talked about the launch.",
                     emotional_tone="focused"),
            "user:nicolas", pool,
        )
        assert eid is not None
        row = await pool.fetchrow(
            "SELECT summary, emotional_tone FROM knowledge.episodes WHERE id = $1", eid,
        )
        assert row["summary"] == "Talked about the launch."
        assert row["emotional_tone"] == "focused"

    @pytest.mark.asyncio
    async def test_trivial_episode_returns_none(self, pool):
        eid = await store_episode(
            _episode(summary="Brief mechanical exchange."), "user:nicolas", pool,
        )
        assert eid is None


# ─── record_episode tool ─────────────────────────────────────────


class TestRecordEpisodeTool:
    @pytest.mark.asyncio
    async def test_records_episode(self, pool):
        import tools.memory_write as mw
        mw.configure(pool=pool)
        out = await mw.handle_record_episode(
            summary="Worked through the memory rework.",
            topics=["memory"], emotional_tone="focused",
        )
        assert "recorded" in out
        row = await pool.fetchrow(
            "SELECT summary FROM knowledge.episodes ORDER BY id DESC LIMIT 1",
        )
        assert row["summary"] == "Worked through the memory rework."

    @pytest.mark.asyncio
    async def test_empty_episode_reports_not_recorded(self, pool):
        import tools.memory_write as mw
        mw.configure(pool=pool)
        out = await mw.handle_record_episode(summary="x", emotional_tone="neutral")
        assert "not recorded" in out
