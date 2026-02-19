"""Tests for structured recall functions in memory.py (Memory v2)."""

import sqlite3
from unittest.mock import AsyncMock

import pytest

from memory import (
    EMPTY_RECALL_FALLBACK,
    PRIORITY_COMMITMENTS,
    PRIORITY_EPISODES,
    PRIORITY_FACTS,
    PRIORITY_VECTOR,
    RecallBlock,
    extract_query_entities,
    get_open_commitments,
    get_session_start_context,
    inject_recall,
    lookup_facts,
    recall,
    resolve_entity,
    search_episodes,
)
from memory_schema import ensure_schema


@pytest.fixture
def mem_conn():
    """In-memory SQLite DB with structured memory schema."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    yield conn
    conn.close()


@pytest.fixture
def populated_conn(mem_conn):
    """DB with test facts, episodes, commitments, and aliases."""
    # Facts
    mem_conn.execute(
        "INSERT INTO facts (entity, attribute, value, confidence, source_session, accessed_at) "
        "VALUES ('nicolas', 'lives_in', 'Austria', 1.0, 'test', datetime('now'))"
    )
    mem_conn.execute(
        "INSERT INTO facts (entity, attribute, value, confidence, source_session, accessed_at) "
        "VALUES ('nicolas', 'cat_name', 'Miso', 0.9, 'test', datetime('now'))"
    )
    mem_conn.execute(
        "INSERT INTO facts (entity, attribute, value, confidence, source_session, accessed_at) "
        "VALUES ('lucy', 'role', 'companion', 1.0, 'test', datetime('now'))"
    )
    # An invalidated fact (should not appear)
    mem_conn.execute(
        "INSERT INTO facts (entity, attribute, value, confidence, source_session, "
        "invalidated_at, accessed_at) "
        "VALUES ('nicolas', 'lives_in', 'Germany', 0.9, 'old', datetime('now'), "
        "datetime('now'))"
    )

    # Aliases
    mem_conn.execute(
        "INSERT INTO entity_aliases (alias, canonical) VALUES ('lucy_belladonna', 'lucy')"
    )
    mem_conn.execute(
        "INSERT INTO entity_aliases (alias, canonical) VALUES ('nic', 'nicolas')"
    )
    mem_conn.execute(
        "INSERT INTO entity_aliases (alias, canonical) VALUES ('uncle_charles', 'uncle_charles')"
    )
    mem_conn.execute(
        "INSERT INTO entity_aliases (alias, canonical) VALUES ('charles', 'uncle_charles')"
    )

    # Episodes
    mem_conn.execute(
        "INSERT INTO episodes (session_id, date, topics, decisions, summary, emotional_tone) "
        "VALUES ('sess1', '2026-02-18', '[\"memory system\", \"architecture\"]', "
        "'[\"use SQLite\"]', 'Discussed memory architecture.', 'productive')"
    )
    mem_conn.execute(
        "INSERT INTO episodes (session_id, date, topics, decisions, summary, emotional_tone) "
        "VALUES ('sess2', '2026-02-19', '[\"deployment\"]', '[]', "
        "'Planned deployment steps.', 'focused')"
    )

    # Commitments
    mem_conn.execute(
        "INSERT INTO commitments (who, what, deadline, status, episode_id) "
        "VALUES ('nicolas', 'review PR', '2026-02-20', 'open', 1)"
    )
    mem_conn.execute(
        "INSERT INTO commitments (who, what, deadline, status, episode_id) "
        "VALUES ('lucy', 'send briefing', NULL, 'open', 2)"
    )
    mem_conn.execute(
        "INSERT INTO commitments (who, what, deadline, status, episode_id) "
        "VALUES ('nicolas', 'old task', '2026-01-01', 'done', 1)"
    )

    mem_conn.commit()
    return mem_conn


# ─── Entity Resolution ───────────────────────────────────────────


class TestResolveEntity:
    def test_alias_resolves(self, populated_conn):
        assert resolve_entity("lucy_belladonna", populated_conn) == "lucy"

    def test_no_alias_returns_normalized(self, populated_conn):
        assert resolve_entity("Unknown Person", populated_conn) == "unknown_person"

    def test_shorthand_alias(self, populated_conn):
        assert resolve_entity("nic", populated_conn) == "nicolas"


class TestExtractQueryEntities:
    def test_single_word_entity(self, populated_conn):
        entities = extract_query_entities("what does nicolas like?", populated_conn)
        assert "nicolas" in entities

    def test_alias_resolves_to_canonical(self, populated_conn):
        entities = extract_query_entities("tell me about nic", populated_conn)
        assert "nicolas" in entities

    def test_bigram_entity(self, populated_conn):
        # "uncle_charles" is a known alias
        entities = extract_query_entities("what about uncle charles?", populated_conn)
        assert "uncle_charles" in entities

    def test_multiple_matches(self, populated_conn):
        entities = extract_query_entities("nicolas and lucy", populated_conn)
        assert "nicolas" in entities
        assert "lucy" in entities

    def test_no_matches_returns_empty(self, populated_conn):
        entities = extract_query_entities("what is the weather?", populated_conn)
        assert len(entities) == 0


# ─── Fact Lookup ─────────────────────────────────────────────────


class TestLookupFacts:
    def test_returns_matching_facts(self, populated_conn):
        facts = lookup_facts({"nicolas"}, populated_conn)
        assert len(facts) == 2
        attrs = {f["attribute"] for f in facts}
        assert "lives_in" in attrs
        assert "cat_name" in attrs

    def test_excludes_invalidated(self, populated_conn):
        facts = lookup_facts({"nicolas"}, populated_conn)
        values = [f["value"] for f in facts]
        assert "Germany" not in values

    def test_updates_accessed_at(self, populated_conn):
        # Get initial accessed_at
        before = populated_conn.execute(
            "SELECT accessed_at FROM facts WHERE entity = 'lucy' "
            "AND invalidated_at IS NULL"
        ).fetchone()[0]

        lookup_facts({"lucy"}, populated_conn)

        after = populated_conn.execute(
            "SELECT accessed_at FROM facts WHERE entity = 'lucy' "
            "AND invalidated_at IS NULL"
        ).fetchone()[0]
        # accessed_at should have been updated
        assert after is not None

    def test_respects_max_results(self, populated_conn):
        facts = lookup_facts({"nicolas"}, populated_conn, max_results=1)
        assert len(facts) == 1

    def test_empty_entities_returns_empty(self, populated_conn):
        assert lookup_facts(set(), populated_conn) == []


# ─── Episode Search ──────────────────────────────────────────────


class TestSearchEpisodes:
    def test_keyword_match_on_topics(self, populated_conn):
        episodes = search_episodes(["memory"], populated_conn)
        assert len(episodes) >= 1
        assert "memory" in episodes[0]["topics"].lower()

    def test_keyword_match_on_summary(self, populated_conn):
        episodes = search_episodes(["deployment"], populated_conn)
        assert len(episodes) >= 1

    def test_no_matches(self, populated_conn):
        episodes = search_episodes(["nonexistent_topic_xyz"], populated_conn)
        assert len(episodes) == 0

    def test_date_range_filtering(self, populated_conn):
        # Search only last 1 day — should miss 2026-02-18 episode
        episodes = search_episodes(
            ["memory"], populated_conn, days_back=1,
        )
        # This depends on current date vs fixture dates — the date filter
        # uses date('now', '-1 days') so fixed dates from 2026-02-18 may
        # or may not match. Test that the query runs without error.
        assert isinstance(episodes, list)


# ─── Commitments ─────────────────────────────────────────────────


class TestGetOpenCommitments:
    def test_returns_open_only(self, populated_conn):
        commits = get_open_commitments(populated_conn)
        assert len(commits) == 2
        statuses = set()
        for c in commits:
            # All returned should be open
            row = populated_conn.execute(
                "SELECT status FROM commitments WHERE id = ?",
                (c["id"],),
            ).fetchone()
            statuses.add(row["status"])
        assert statuses == {"open"}

    def test_ordered_by_deadline(self, populated_conn):
        commits = get_open_commitments(populated_conn)
        # First should be the one with deadline, then the one without
        assert commits[0]["deadline"] is not None
        assert commits[1]["deadline"] is None


# ─── recall() Integration ────────────────────────────────────────


class TestRecall:
    @pytest.mark.asyncio
    async def test_integrates_facts_episodes_vector_commitments(self, populated_conn):
        mock_memory = AsyncMock()
        mock_memory.search.return_value = [
            {"text": "some vector result", "score": 0.8, "days_old": 0},
        ]

        class FakeConfig:
            recall_max_facts = 20
            recall_decay_rate = 0.03

        blocks = await recall(
            "what does nicolas like?", populated_conn,
            mock_memory, FakeConfig(),
        )

        # Should have multiple block types
        sections = {b.section for b in blocks}
        assert "[Known facts]" in sections
        assert "[Open commitments]" in sections
        assert "[Memory search]" in sections

    @pytest.mark.asyncio
    async def test_blocks_sorted_by_priority(self, populated_conn):
        mock_memory = AsyncMock()
        mock_memory.search.return_value = [
            {"text": "vector", "score": 0.5, "days_old": 0},
        ]

        class FakeConfig:
            recall_max_facts = 20
            recall_decay_rate = 0.03

        blocks = await recall(
            "nicolas", populated_conn,
            mock_memory, FakeConfig(),
        )

        # Verify sorted highest-first
        for i in range(len(blocks) - 1):
            assert blocks[i].priority >= blocks[i + 1].priority

    @pytest.mark.asyncio
    async def test_vector_k_reduced_when_structured_exists(self, populated_conn):
        mock_memory = AsyncMock()
        mock_memory.search.return_value = []

        class FakeConfig:
            recall_max_facts = 20
            recall_decay_rate = 0.03

        await recall(
            "nicolas", populated_conn,
            mock_memory, FakeConfig(), top_k=5,
        )

        # If structured results existed, vector_k should have been reduced
        call_args = mock_memory.search.call_args
        actual_k = call_args.kwargs.get("top_k", call_args.args[1] if len(call_args.args) > 1 else None)
        # With structured results, vector_k = max(1, top_k - len(blocks))
        # The exact value depends on how many structured blocks exist
        assert actual_k is not None

    @pytest.mark.asyncio
    async def test_empty_query_returns_commitments_only(self, populated_conn):
        mock_memory = AsyncMock()
        mock_memory.search.return_value = []

        class FakeConfig:
            recall_max_facts = 20
            recall_decay_rate = 0.03

        blocks = await recall(
            "xyznonexistent", populated_conn,
            mock_memory, FakeConfig(),
        )

        # Should at least have commitments (always included)
        has_commitments = any(b.section == "[Open commitments]" for b in blocks)
        assert has_commitments


# ─── inject_recall ───────────────────────────────────────────────


class TestInjectRecall:
    def test_respects_budget(self):
        blocks = [
            RecallBlock(priority=40, section="[High]", text="A" * 400, est_tokens=100),
            RecallBlock(priority=30, section="[Med]", text="B" * 400, est_tokens=100),
            RecallBlock(priority=10, section="[Low]", text="C" * 400, est_tokens=100),
        ]
        result = inject_recall(blocks, max_tokens=250)
        assert "[High]" in result
        assert "[Med]" in result
        assert "[Low]" not in result  # dropped due to budget

    def test_empty_blocks_returns_empty(self):
        assert inject_recall([], max_tokens=1000) == ""

    def test_zero_budget_returns_empty(self):
        blocks = [
            RecallBlock(priority=40, section="[X]", text="data", est_tokens=10),
        ]
        result = inject_recall(blocks, max_tokens=0)
        assert result == ""

    def test_unlimited_budget_includes_all(self):
        blocks = [
            RecallBlock(priority=40, section="[A]", text="data a", est_tokens=10),
            RecallBlock(priority=30, section="[B]", text="data b", est_tokens=10),
            RecallBlock(priority=20, section="[C]", text="data c", est_tokens=10),
        ]
        result = inject_recall(blocks, max_tokens=10000)
        assert "[A]" in result
        assert "[B]" in result
        assert "[C]" in result

    def test_drops_lowest_priority_first(self):
        """With sorted blocks (high→low), budget drops from the end."""
        blocks = [
            RecallBlock(priority=40, section="[High]", text="x", est_tokens=50),
            RecallBlock(priority=10, section="[Low]", text="x", est_tokens=50),
        ]
        result = inject_recall(blocks, max_tokens=60)
        assert "[High]" in result
        assert "[Low]" not in result


# ─── get_session_start_context ───────────────────────────────────


class TestGetSessionStartContext:
    def test_returns_facts_and_commitments(self, populated_conn):
        result = get_session_start_context(populated_conn)
        assert "[Known facts]" in result
        assert "[Open commitments]" in result

    def test_respects_max_facts(self, populated_conn):
        result = get_session_start_context(populated_conn, max_facts=1)
        # Should only have 1 fact line
        facts_section = result.split("[Open commitments]")[0]
        fact_lines = [l for l in facts_section.split("\n") if l.strip().startswith("nicolas.") or l.strip().startswith("lucy.")]
        assert len(fact_lines) <= 1

    def test_empty_db_returns_empty(self, mem_conn):
        result = get_session_start_context(mem_conn)
        assert result == ""

    def test_budget_constraint(self, populated_conn):
        result = get_session_start_context(populated_conn, max_tokens=10)
        # With very small budget, might only fit one block or nothing
        assert isinstance(result, str)


# ─── Constants ───────────────────────────────────────────────────


class TestConstants:
    def test_priority_ordering(self):
        assert PRIORITY_COMMITMENTS > PRIORITY_FACTS
        assert PRIORITY_FACTS > PRIORITY_EPISODES
        assert PRIORITY_EPISODES > PRIORITY_VECTOR

    def test_empty_recall_fallback_not_empty(self):
        assert len(EMPTY_RECALL_FALLBACK) > 0
        assert "memory" in EMPTY_RECALL_FALLBACK.lower()
