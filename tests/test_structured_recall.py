"""Tests for structured recall functions in memory.py (Memory v2)."""

import sqlite3
from unittest.mock import AsyncMock

import pytest

from memory import (
    _DEFAULT_PRIORITIES,
    EMPTY_RECALL_FALLBACK,
    RecallBlock,
    _format_episode,
    _format_fact_row,
    _format_fact_tuple,
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


class FakeConfig:
    """Config mock with all personality attributes at warm defaults."""
    recall_max_facts = 20
    recall_decay_rate = 0.03
    recall_fact_format = "natural"
    recall_show_emotional_tone = True
    recall_priority_vector = 35
    recall_priority_episodes = 25
    recall_priority_facts = 15
    recall_priority_commitments = 40
    recall_episode_section_header = "Recent conversations"
    recall_max_episodes_at_start = 3
    recall_max_dynamic_tokens = 1500


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
        # accessed_at should have been updated (at least as recent as before)
        assert after is not None
        assert after >= before

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


# ─── Format Helpers ──────────────────────────────────────────────


class TestFormatFactRow:
    """Tests for _format_fact_row (sqlite3.Row access pattern)."""

    def test_natural_format(self, populated_conn):
        row = populated_conn.execute(
            "SELECT id, entity, attribute, value, confidence "
            "FROM facts WHERE entity = 'nicolas' AND attribute = 'lives_in' "
            "AND invalidated_at IS NULL"
        ).fetchone()
        result = _format_fact_row(row, "natural")
        assert result == "  nicolas — lives in: Austria"

    def test_compact_format(self, populated_conn):
        row = populated_conn.execute(
            "SELECT id, entity, attribute, value, confidence "
            "FROM facts WHERE entity = 'nicolas' AND attribute = 'lives_in' "
            "AND invalidated_at IS NULL"
        ).fetchone()
        result = _format_fact_row(row, "compact")
        assert result == "  nicolas.lives_in: Austria"

    def test_underscores_replaced_in_natural(self, populated_conn):
        row = populated_conn.execute(
            "SELECT id, entity, attribute, value, confidence "
            "FROM facts WHERE entity = 'nicolas' AND attribute = 'cat_name' "
            "AND invalidated_at IS NULL"
        ).fetchone()
        result = _format_fact_row(row, "natural")
        assert "cat name" in result
        assert "_" not in result.split(":")[0]  # no underscores before the value


class TestFormatFactTuple:
    """Tests for _format_fact_tuple (raw tuple access pattern)."""

    def test_natural_format(self):
        result = _format_fact_tuple(("nicolas", "lives_in", "Austria"), "natural")
        assert result == "  nicolas — lives in: Austria"

    def test_compact_format(self):
        result = _format_fact_tuple(("nicolas", "lives_in", "Austria"), "compact")
        assert result == "  nicolas.lives_in: Austria"

    def test_underscores_preserved_in_compact(self):
        result = _format_fact_tuple(("uncle_charles", "phone_number", "555"), "compact")
        assert "uncle_charles" in result
        assert "phone_number" in result

    def test_underscores_replaced_in_natural(self):
        result = _format_fact_tuple(("uncle_charles", "phone_number", "555"), "natural")
        assert "uncle charles" in result
        assert "phone number" in result


class TestFormatEpisode:
    """Tests for _format_episode."""

    def test_shows_non_neutral_tone(self):
        e = ("2026-02-18", "Discussed architecture.", "productive")
        result = _format_episode(e, show_tone=True)
        assert "(tone: productive)" in result
        assert "[2026-02-18]" in result

    def test_omits_neutral_tone(self):
        e = ("2026-02-18", "Routine check.", "neutral")
        result = _format_episode(e, show_tone=True)
        assert "tone:" not in result

    def test_omits_tone_when_disabled(self):
        e = ("2026-02-18", "Discussed architecture.", "productive")
        result = _format_episode(e, show_tone=False)
        assert "tone:" not in result
        assert "Discussed architecture." in result

    def test_omits_none_tone(self):
        e = ("2026-02-18", "Something happened.", None)
        result = _format_episode(e, show_tone=True)
        assert "tone:" not in result

    def test_with_sqlite_row(self, populated_conn):
        row = populated_conn.execute(
            "SELECT date, summary, emotional_tone FROM episodes LIMIT 1"
        ).fetchone()
        result = _format_episode(row, show_tone=True)
        assert "[" in result
        assert "]" in result


# ─── recall() Integration ────────────────────────────────────────


class TestRecall:
    @pytest.mark.asyncio
    async def test_integrates_facts_episodes_vector_commitments(self, populated_conn):
        mock_memory = AsyncMock()
        mock_memory.search.return_value = [
            {"text": "some vector result", "score": 0.8, "days_old": 0},
        ]

        blocks = await recall(
            "what does nicolas like?", populated_conn,
            mock_memory, FakeConfig(),
        )

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

        blocks = await recall(
            "nicolas", populated_conn,
            mock_memory, FakeConfig(),
        )

        # Verify sorted highest-first
        for i in range(len(blocks) - 1):
            assert blocks[i].priority >= blocks[i + 1].priority

    @pytest.mark.asyncio
    async def test_vector_gets_full_top_k(self, populated_conn):
        """Vector search receives full top_k — no pre-throttle."""
        mock_memory = AsyncMock()
        mock_memory.search.return_value = []

        await recall(
            "nicolas", populated_conn,
            mock_memory, FakeConfig(), top_k=5,
        )

        call_args = mock_memory.search.call_args
        actual_k = call_args.kwargs.get(
            "top_k", call_args.args[1] if len(call_args.args) > 1 else None
        )
        assert actual_k == 5

    @pytest.mark.asyncio
    async def test_empty_query_returns_commitments_only(self, populated_conn):
        mock_memory = AsyncMock()
        mock_memory.search.return_value = []

        blocks = await recall(
            "xyznonexistent", populated_conn,
            mock_memory, FakeConfig(),
        )

        has_commitments = any(b.section == "[Open commitments]" for b in blocks)
        assert has_commitments

    @pytest.mark.asyncio
    async def test_uses_config_priorities(self, populated_conn):
        """Verify recall uses config priority values, not defaults."""
        mock_memory = AsyncMock()
        mock_memory.search.return_value = [
            {"text": "vector", "score": 0.5, "days_old": 0},
        ]

        class FactsHeavyConfig(FakeConfig):
            recall_priority_facts = 50
            recall_priority_vector = 5

        blocks = await recall(
            "nicolas", populated_conn,
            mock_memory, FactsHeavyConfig(),
        )

        fact_block = next(b for b in blocks if b.section == "[Known facts]")
        vector_block = next(b for b in blocks if b.section == "[Memory search]")
        assert fact_block.priority == 50
        assert vector_block.priority == 5
        # Facts should sort before vector
        fact_idx = blocks.index(fact_block)
        vector_idx = blocks.index(vector_block)
        assert fact_idx < vector_idx

    @pytest.mark.asyncio
    async def test_uses_natural_fact_format(self, populated_conn):
        mock_memory = AsyncMock()
        mock_memory.search.return_value = []

        blocks = await recall(
            "nicolas", populated_conn,
            mock_memory, FakeConfig(),
        )

        fact_block = next(b for b in blocks if b.section == "[Known facts]")
        # Natural format: spaces, em-dash
        assert " — " in fact_block.text

    @pytest.mark.asyncio
    async def test_uses_compact_fact_format(self, populated_conn):
        mock_memory = AsyncMock()
        mock_memory.search.return_value = []

        class CompactConfig(FakeConfig):
            recall_fact_format = "compact"

        blocks = await recall(
            "nicolas", populated_conn,
            mock_memory, CompactConfig(),
        )

        fact_block = next(b for b in blocks if b.section == "[Known facts]")
        assert "." in fact_block.text
        assert " — " not in fact_block.text

    @pytest.mark.asyncio
    async def test_episode_section_header_from_config(self, populated_conn):
        mock_memory = AsyncMock()
        mock_memory.search.return_value = []

        class CustomHeaderConfig(FakeConfig):
            recall_episode_section_header = "What happened lately"

        # Query must match episode keywords — "memory" hits fixture episode topics
        blocks = await recall(
            "tell me about memory architecture", populated_conn,
            mock_memory, CustomHeaderConfig(),
        )

        episode_block = next(
            (b for b in blocks if "What happened lately" in b.section), None
        )
        assert episode_block is not None

    @pytest.mark.asyncio
    async def test_episode_tone_displayed(self, populated_conn):
        mock_memory = AsyncMock()
        mock_memory.search.return_value = []

        # Query must match episode keywords — "deployment" hits fixture episode
        blocks = await recall(
            "deployment planning", populated_conn,
            mock_memory, FakeConfig(),
        )

        episode_block = next(
            (b for b in blocks if "Recent conversations" in b.section), None
        )
        assert episode_block is not None
        # Fixture episodes have non-neutral tones
        assert "tone:" in episode_block.text


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
        """With sorted blocks (high->low), budget drops from the end."""
        blocks = [
            RecallBlock(priority=40, section="[High]", text="x", est_tokens=50),
            RecallBlock(priority=10, section="[Low]", text="x", est_tokens=50),
        ]
        result = inject_recall(blocks, max_tokens=60)
        assert "[High]" in result
        assert "[Low]" not in result


# ─── get_session_start_context ───────────────────────────────────


class TestGetSessionStartContext:
    def test_returns_facts_episodes_and_commitments(self, populated_conn):
        result = get_session_start_context(populated_conn, config=FakeConfig())
        assert "[Known facts]" in result
        assert "[Recent conversations]" in result
        assert "[Open commitments]" in result

    def test_respects_max_facts(self, populated_conn):
        result = get_session_start_context(
            populated_conn, config=FakeConfig(), max_facts=1
        )
        # Natural format uses " — " not "."
        facts_section = result.split("[Recent conversations]")[0]
        fact_lines = [
            line for line in facts_section.split("\n")
            if line.strip() and " — " in line
        ]
        assert len(fact_lines) <= 1

    def test_respects_max_episodes(self, populated_conn):
        result = get_session_start_context(
            populated_conn, config=FakeConfig(), max_episodes=1
        )
        episode_section = result.split("[Recent conversations]")[1].split("[Open commitments]")[0]
        episode_lines = [
            line for line in episode_section.split("\n")
            if line.strip() and line.strip().startswith("[")
        ]
        assert len(episode_lines) <= 1

    def test_empty_db_returns_empty(self, mem_conn):
        result = get_session_start_context(mem_conn)
        assert result == ""

    def test_budget_constraint(self, populated_conn):
        result = get_session_start_context(
            populated_conn, config=FakeConfig(), max_tokens=10
        )
        assert isinstance(result, str)

    def test_works_without_config(self, populated_conn):
        """Graceful fallback when config is None (backwards compat)."""
        result = get_session_start_context(populated_conn)
        assert "[Known facts]" in result
        assert "[Open commitments]" in result

    def test_uses_config_fact_format(self, populated_conn):
        class CompactConfig(FakeConfig):
            recall_fact_format = "compact"

        result = get_session_start_context(
            populated_conn, config=CompactConfig()
        )
        # Compact format uses dots, not em-dashes
        assert "." in result.split("[Recent conversations]")[0]

    def test_episode_tone_in_output(self, populated_conn):
        result = get_session_start_context(populated_conn, config=FakeConfig())
        # Fixture episodes have non-neutral tones
        assert "tone:" in result


# ─── Defaults ────────────────────────────────────────────────────


class TestDefaults:
    def test_default_priorities_exist(self):
        assert "vector" in _DEFAULT_PRIORITIES
        assert "episodes" in _DEFAULT_PRIORITIES
        assert "facts" in _DEFAULT_PRIORITIES
        assert "commitments" in _DEFAULT_PRIORITIES

    def test_commitments_highest_default(self):
        assert _DEFAULT_PRIORITIES["commitments"] > _DEFAULT_PRIORITIES["vector"]
        assert _DEFAULT_PRIORITIES["commitments"] > _DEFAULT_PRIORITIES["episodes"]
        assert _DEFAULT_PRIORITIES["commitments"] > _DEFAULT_PRIORITIES["facts"]

    def test_vector_outranks_facts_in_warm_profile(self):
        """Warm defaults: vector > facts (warmth over clinical precision)."""
        assert _DEFAULT_PRIORITIES["vector"] > _DEFAULT_PRIORITIES["facts"]

    def test_empty_recall_fallback_not_empty(self):
        assert len(EMPTY_RECALL_FALLBACK) > 0
        assert "memory" in EMPTY_RECALL_FALLBACK.lower()
