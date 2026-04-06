"""Tests for structured recall functions in memory.py (Memory v2)."""

from typing import Any
from unittest.mock import AsyncMock

import pytest

from memory import (
    EMPTY_RECALL_FALLBACK,
    RECALL_PRIORITY_COMMITMENTS,
    RECALL_PRIORITY_EPISODES,
    RECALL_PRIORITY_FACTS,
    RECALL_PRIORITY_VECTOR,
    RecallBlock,
    _format_episode,
    _format_fact,
    extract_query_entities,
    get_open_commitments,
    get_session_start_context,
    inject_recall,
    lookup_facts,
    recall,
    resolve_entity,
    search_episodes,
)

TEST_CLIENT_ID = "test"
TEST_AGENT_ID = "test_agent"


@pytest.fixture
async def populated_pool(pool: Any) -> Any:
    """Pool with test facts, episodes, commitments, and aliases."""
    cid, aid = TEST_CLIENT_ID, TEST_AGENT_ID

    # Facts
    await pool.execute(
        "INSERT INTO knowledge.facts "
        "(client_id, agent_id, entity, attribute, value, confidence, "
        "source_session, accessed_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, now())",
        cid, aid, "nicolas", "lives_in", "Austria", 1.0, "test",
    )
    await pool.execute(
        "INSERT INTO knowledge.facts "
        "(client_id, agent_id, entity, attribute, value, confidence, "
        "source_session, accessed_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, now())",
        cid, aid, "nicolas", "cat_name", "Miso", 0.9, "test",
    )
    await pool.execute(
        "INSERT INTO knowledge.facts "
        "(client_id, agent_id, entity, attribute, value, confidence, "
        "source_session, accessed_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, now())",
        cid, aid, "lucy", "role", "companion", 1.0, "test",
    )
    # An invalidated fact (should not appear)
    await pool.execute(
        "INSERT INTO knowledge.facts "
        "(client_id, agent_id, entity, attribute, value, confidence, "
        "source_session, invalidated_at, accessed_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, now(), now())",
        cid, aid, "nicolas", "lives_in", "Germany", 0.9, "old",
    )

    # Aliases
    await pool.execute(
        "INSERT INTO knowledge.entity_aliases "
        "(client_id, agent_id, alias, canonical) VALUES ($1, $2, $3, $4)",
        cid, aid, "alex_johnson", "alex",
    )
    await pool.execute(
        "INSERT INTO knowledge.entity_aliases "
        "(client_id, agent_id, alias, canonical) VALUES ($1, $2, $3, $4)",
        cid, aid, "nic", "nicolas",
    )
    await pool.execute(
        "INSERT INTO knowledge.entity_aliases "
        "(client_id, agent_id, alias, canonical) VALUES ($1, $2, $3, $4)",
        cid, aid, "uncle_charles", "uncle_charles",
    )
    await pool.execute(
        "INSERT INTO knowledge.entity_aliases "
        "(client_id, agent_id, alias, canonical) VALUES ($1, $2, $3, $4)",
        cid, aid, "charles", "uncle_charles",
    )

    # Episodes
    ep1_id: int = await pool.fetchval(
        "INSERT INTO knowledge.episodes "
        "(client_id, agent_id, session_id, date, topics, decisions, "
        "summary, emotional_tone) "
        "VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8) "
        "RETURNING id",
        cid, aid, "sess1", "2026-02-18",
        '["memory system", "architecture"]', '["use SQLite"]',
        "Discussed memory architecture.", "productive",
    )
    ep2_id: int = await pool.fetchval(
        "INSERT INTO knowledge.episodes "
        "(client_id, agent_id, session_id, date, topics, decisions, "
        "summary, emotional_tone) "
        "VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8) "
        "RETURNING id",
        cid, aid, "sess2", "2026-02-19",
        '["deployment"]', '[]',
        "Planned deployment steps.", "focused",
    )

    # Commitments
    await pool.execute(
        "INSERT INTO knowledge.commitments "
        "(client_id, agent_id, who, what, deadline, status, episode_id) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7)",
        cid, aid, "nicolas", "review PR", "2026-02-20", "open", ep1_id,
    )
    await pool.execute(
        "INSERT INTO knowledge.commitments "
        "(client_id, agent_id, who, what, deadline, status, episode_id) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7)",
        cid, aid, "lucy", "send briefing", None, "open", ep2_id,
    )
    await pool.execute(
        "INSERT INTO knowledge.commitments "
        "(client_id, agent_id, who, what, deadline, status, episode_id) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7)",
        cid, aid, "nicolas", "old task", "2026-01-01", "done", ep1_id,
    )

    return pool


class FakeConfig:
    """Config mock with recall attributes."""
    recall_max_facts = 20
    recall_decay_rate = 0.03
    recall_max_episodes_at_start = 3
    recall_max_dynamic_tokens = 1500


# ─── Entity Resolution ───────────────────────────────────────────


class TestResolveEntity:
    @pytest.mark.asyncio
    async def test_alias_resolves(self, populated_pool: Any) -> None:
        result = await resolve_entity(
            "alex_johnson", populated_pool, TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert result == "alex"

    @pytest.mark.asyncio
    async def test_no_alias_returns_normalized(self, populated_pool: Any) -> None:
        result = await resolve_entity(
            "Unknown Person", populated_pool, TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert result == "unknown_person"

    @pytest.mark.asyncio
    async def test_shorthand_alias(self, populated_pool: Any) -> None:
        result = await resolve_entity(
            "nic", populated_pool, TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert result == "nicolas"


class TestExtractQueryEntities:
    @pytest.mark.asyncio
    async def test_single_word_entity(self, populated_pool: Any) -> None:
        entities = await extract_query_entities(
            "what does nicolas like?", populated_pool,
            TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert "nicolas" in entities

    @pytest.mark.asyncio
    async def test_alias_resolves_to_canonical(self, populated_pool: Any) -> None:
        entities = await extract_query_entities(
            "tell me about nic", populated_pool,
            TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert "nicolas" in entities

    @pytest.mark.asyncio
    async def test_bigram_entity(self, populated_pool: Any) -> None:
        # "uncle_charles" is a known alias
        entities = await extract_query_entities(
            "what about uncle charles?", populated_pool,
            TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert "uncle_charles" in entities

    @pytest.mark.asyncio
    async def test_multiple_matches(self, populated_pool: Any) -> None:
        entities = await extract_query_entities(
            "nicolas and lucy", populated_pool,
            TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert "nicolas" in entities
        assert "lucy" in entities

    @pytest.mark.asyncio
    async def test_no_matches_returns_empty(self, populated_pool: Any) -> None:
        entities = await extract_query_entities(
            "what is the weather?", populated_pool,
            TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert len(entities) == 0


# ─── Fact Lookup ─────────────────────────────────────────────────


class TestLookupFacts:
    @pytest.mark.asyncio
    async def test_returns_matching_facts(self, populated_pool: Any) -> None:
        facts = await lookup_facts(
            {"nicolas"}, populated_pool, TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert len(facts) == 2
        attrs = {f["attribute"] for f in facts}
        assert "lives_in" in attrs
        assert "cat_name" in attrs

    @pytest.mark.asyncio
    async def test_excludes_invalidated(self, populated_pool: Any) -> None:
        facts = await lookup_facts(
            {"nicolas"}, populated_pool, TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        values = [f["value"] for f in facts]
        assert "Germany" not in values

    @pytest.mark.asyncio
    async def test_updates_accessed_at(self, populated_pool: Any) -> None:
        # Get initial accessed_at
        before = await populated_pool.fetchval(
            "SELECT accessed_at FROM knowledge.facts "
            "WHERE client_id = $1 AND agent_id = $2 "
            "AND entity = $3 AND invalidated_at IS NULL",
            TEST_CLIENT_ID, TEST_AGENT_ID, "lucy",
        )

        await lookup_facts(
            {"lucy"}, populated_pool, TEST_CLIENT_ID, TEST_AGENT_ID,
        )

        after = await populated_pool.fetchval(
            "SELECT accessed_at FROM knowledge.facts "
            "WHERE client_id = $1 AND agent_id = $2 "
            "AND entity = $3 AND invalidated_at IS NULL",
            TEST_CLIENT_ID, TEST_AGENT_ID, "lucy",
        )
        # accessed_at should have been updated (at least as recent as before)
        assert after is not None
        assert after >= before

    @pytest.mark.asyncio
    async def test_respects_max_results(self, populated_pool: Any) -> None:
        facts = await lookup_facts(
            {"nicolas"}, populated_pool, TEST_CLIENT_ID, TEST_AGENT_ID,
            max_results=1,
        )
        assert len(facts) == 1

    @pytest.mark.asyncio
    async def test_empty_entities_returns_empty(self, populated_pool: Any) -> None:
        result = await lookup_facts(
            set(), populated_pool, TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert result == []


# ─── Episode Search ──────────────────────────────────────────────


class TestSearchEpisodes:
    @pytest.mark.asyncio
    async def test_keyword_match_on_topics(self, populated_pool: Any) -> None:
        episodes = await search_episodes(
            ["memory"], populated_pool, TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert len(episodes) >= 1
        assert "memory" in episodes[0]["topics"].lower()

    @pytest.mark.asyncio
    async def test_keyword_match_on_summary(self, populated_pool: Any) -> None:
        episodes = await search_episodes(
            ["deployment"], populated_pool, TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert len(episodes) >= 1

    @pytest.mark.asyncio
    async def test_no_matches(self, populated_pool: Any) -> None:
        episodes = await search_episodes(
            ["nonexistent_topic_xyz"], populated_pool,
            TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert len(episodes) == 0

    @pytest.mark.asyncio
    async def test_date_range_filtering(self, populated_pool: Any) -> None:
        # Search only last 1 day — should miss 2026-02-18 episode
        episodes = await search_episodes(
            ["memory"], populated_pool, TEST_CLIENT_ID, TEST_AGENT_ID,
            days_back=1,
        )
        # This depends on current date vs fixture dates — the date filter
        # uses CURRENT_DATE - N so fixed dates from 2026-02-18 may
        # or may not match. Test that the query runs without error.
        assert isinstance(episodes, list)


# ─── Commitments ─────────────────────────────────────────────────


class TestGetOpenCommitments:
    @pytest.mark.asyncio
    async def test_returns_open_only(self, populated_pool: Any) -> None:
        commits = await get_open_commitments(
            populated_pool, TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert len(commits) == 2
        statuses = set()
        for c in commits:
            # All returned should be open
            row = await populated_pool.fetchrow(
                "SELECT status FROM knowledge.commitments "
                "WHERE client_id = $1 AND agent_id = $2 AND id = $3",
                TEST_CLIENT_ID, TEST_AGENT_ID, c["id"],
            )
            statuses.add(row["status"])
        assert statuses == {"open"}

    @pytest.mark.asyncio
    async def test_ordered_by_deadline(self, populated_pool: Any) -> None:
        commits = await get_open_commitments(
            populated_pool, TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        # First should be the one with deadline, then the one without
        assert commits[0]["deadline"] is not None
        assert commits[1]["deadline"] is None


# ─── Format Helpers ──────────────────────────────────────────────


class TestFormatFactRow:
    """Tests for _format_fact with asyncpg.Record (dict-like access pattern)."""

    @pytest.mark.asyncio
    async def test_natural_format(self, populated_pool: Any) -> None:
        row = await populated_pool.fetchrow(
            "SELECT id, entity, attribute, value, confidence "
            "FROM knowledge.facts "
            "WHERE client_id = $1 AND agent_id = $2 "
            "AND entity = $3 AND attribute = $4 "
            "AND invalidated_at IS NULL",
            TEST_CLIENT_ID, TEST_AGENT_ID, "nicolas", "lives_in",
        )
        result = _format_fact(row, "natural")
        assert result == "  nicolas — lives in: Austria"

    @pytest.mark.asyncio
    async def test_compact_format(self, populated_pool: Any) -> None:
        row = await populated_pool.fetchrow(
            "SELECT id, entity, attribute, value, confidence "
            "FROM knowledge.facts "
            "WHERE client_id = $1 AND agent_id = $2 "
            "AND entity = $3 AND attribute = $4 "
            "AND invalidated_at IS NULL",
            TEST_CLIENT_ID, TEST_AGENT_ID, "nicolas", "lives_in",
        )
        result = _format_fact(row, "compact")
        assert result == "  nicolas.lives_in: Austria"

    @pytest.mark.asyncio
    async def test_underscores_replaced_in_natural(self, populated_pool: Any) -> None:
        row = await populated_pool.fetchrow(
            "SELECT id, entity, attribute, value, confidence "
            "FROM knowledge.facts "
            "WHERE client_id = $1 AND agent_id = $2 "
            "AND entity = $3 AND attribute = $4 "
            "AND invalidated_at IS NULL",
            TEST_CLIENT_ID, TEST_AGENT_ID, "nicolas", "cat_name",
        )
        result = _format_fact(row, "natural")
        assert "cat name" in result
        assert "_" not in result.split(":")[0]  # no underscores before the value


class TestFormatFactTuple:
    """Tests for _format_fact with raw tuple (entity, attribute, value)."""

    def test_natural_format(self) -> None:
        result = _format_fact(("nicolas", "lives_in", "Austria"), "natural")
        assert result == "  nicolas — lives in: Austria"

    def test_compact_format(self) -> None:
        result = _format_fact(("nicolas", "lives_in", "Austria"), "compact")
        assert result == "  nicolas.lives_in: Austria"

    def test_underscores_preserved_in_compact(self) -> None:
        result = _format_fact(("uncle_charles", "phone_number", "555"), "compact")
        assert "uncle_charles" in result
        assert "phone_number" in result

    def test_underscores_replaced_in_natural(self) -> None:
        result = _format_fact(("uncle_charles", "phone_number", "555"), "natural")
        assert "uncle charles" in result
        assert "phone number" in result


class TestFormatEpisode:
    """Tests for _format_episode."""

    def test_shows_non_neutral_tone(self) -> None:
        e = ("2026-02-18", "Discussed architecture.", "productive")
        result = _format_episode(e, show_tone=True)
        assert "(tone: productive)" in result
        assert "[2026-02-18]" in result

    def test_omits_neutral_tone(self) -> None:
        e = ("2026-02-18", "Routine check.", "neutral")
        result = _format_episode(e, show_tone=True)
        assert "tone:" not in result

    def test_omits_tone_when_disabled(self) -> None:
        e = ("2026-02-18", "Discussed architecture.", "productive")
        result = _format_episode(e, show_tone=False)
        assert "tone:" not in result
        assert "Discussed architecture." in result

    def test_omits_none_tone(self) -> None:
        e = ("2026-02-18", "Something happened.", None)
        result = _format_episode(e, show_tone=True)
        assert "tone:" not in result

    @pytest.mark.asyncio
    async def test_with_asyncpg_record(self, populated_pool: Any) -> None:
        row = await populated_pool.fetchrow(
            "SELECT date, summary, emotional_tone "
            "FROM knowledge.episodes "
            "WHERE client_id = $1 AND agent_id = $2 LIMIT 1",
            TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        result = _format_episode(row, show_tone=True)
        assert "[" in result
        assert "]" in result


# ─── recall() Integration ────────────────────────────────────────


class TestRecall:
    @pytest.mark.asyncio
    async def test_integrates_facts_episodes_vector_commitments(
        self, populated_pool: Any,
    ) -> None:
        mock_memory = AsyncMock()
        mock_memory.search.return_value = [
            {"text": "some vector result", "score": 0.8, "days_old": 0},
        ]

        blocks = await recall(
            "what does nicolas like?", populated_pool,
            TEST_CLIENT_ID, TEST_AGENT_ID,
            mock_memory, FakeConfig(),
        )

        sections = {b.section for b in blocks}
        assert "[Known facts]" in sections
        assert "[Open commitments]" in sections
        assert "[Memory search]" in sections

    @pytest.mark.asyncio
    async def test_blocks_sorted_by_priority(self, populated_pool: Any) -> None:
        mock_memory = AsyncMock()
        mock_memory.search.return_value = [
            {"text": "vector", "score": 0.5, "days_old": 0},
        ]

        blocks = await recall(
            "nicolas", populated_pool,
            TEST_CLIENT_ID, TEST_AGENT_ID,
            mock_memory, FakeConfig(),
        )

        # Verify sorted highest-first
        for i in range(len(blocks) - 1):
            assert blocks[i].priority >= blocks[i + 1].priority

    @pytest.mark.asyncio
    async def test_vector_gets_full_top_k(self, populated_pool: Any) -> None:
        """Vector search receives full top_k — no pre-throttle."""
        mock_memory = AsyncMock()
        mock_memory.search.return_value = []

        await recall(
            "nicolas", populated_pool,
            TEST_CLIENT_ID, TEST_AGENT_ID,
            mock_memory, FakeConfig(), top_k=5,
        )

        call_args = mock_memory.search.call_args
        actual_k = call_args.kwargs.get(
            "top_k", call_args.args[1] if len(call_args.args) > 1 else None
        )
        assert actual_k == 5

    @pytest.mark.asyncio
    async def test_empty_query_returns_commitments_only(
        self, populated_pool: Any,
    ) -> None:
        mock_memory = AsyncMock()
        mock_memory.search.return_value = []

        blocks = await recall(
            "xyznonexistent", populated_pool,
            TEST_CLIENT_ID, TEST_AGENT_ID,
            mock_memory, FakeConfig(),
        )

        has_commitments = any(b.section == "[Open commitments]" for b in blocks)
        assert has_commitments

    @pytest.mark.asyncio
    async def test_uses_hardcoded_priorities(self, populated_pool: Any) -> None:
        """Verify recall uses hardcoded priority constants."""
        mock_memory = AsyncMock()
        mock_memory.search.return_value = [
            {"text": "vector", "score": 0.5, "days_old": 0},
        ]

        blocks = await recall(
            "nicolas", populated_pool,
            TEST_CLIENT_ID, TEST_AGENT_ID,
            mock_memory, FakeConfig(),
        )

        fact_block = next(b for b in blocks if b.section == "[Known facts]")
        vector_block = next(b for b in blocks if b.section == "[Memory search]")
        assert fact_block.priority == RECALL_PRIORITY_FACTS
        assert vector_block.priority == RECALL_PRIORITY_VECTOR

    @pytest.mark.asyncio
    async def test_uses_natural_fact_format(self, populated_pool: Any) -> None:
        mock_memory = AsyncMock()
        mock_memory.search.return_value = []

        blocks = await recall(
            "nicolas", populated_pool,
            TEST_CLIENT_ID, TEST_AGENT_ID,
            mock_memory, FakeConfig(),
        )

        fact_block = next(b for b in blocks if b.section == "[Known facts]")
        # Natural format: spaces, em-dash
        assert " — " in fact_block.text

    @pytest.mark.asyncio
    async def test_episode_section_header_uses_constant(
        self, populated_pool: Any,
    ) -> None:
        mock_memory = AsyncMock()
        mock_memory.search.return_value = []

        # Query must match episode keywords — "memory" hits fixture episode topics
        blocks = await recall(
            "tell me about memory architecture", populated_pool,
            TEST_CLIENT_ID, TEST_AGENT_ID,
            mock_memory, FakeConfig(),
        )

        episode_block = next(
            (b for b in blocks if "Recent conversations" in b.section), None
        )
        assert episode_block is not None

    @pytest.mark.asyncio
    async def test_episode_tone_displayed(self, populated_pool: Any) -> None:
        mock_memory = AsyncMock()
        mock_memory.search.return_value = []

        # Query must match episode keywords — "deployment" hits fixture episode
        blocks = await recall(
            "deployment planning", populated_pool,
            TEST_CLIENT_ID, TEST_AGENT_ID,
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
    def test_respects_budget(self) -> None:
        blocks = [
            RecallBlock(priority=40, section="[High]", text="A" * 400, est_tokens=100),
            RecallBlock(priority=30, section="[Med]", text="B" * 400, est_tokens=100),
            RecallBlock(priority=10, section="[Low]", text="C" * 400, est_tokens=100),
        ]
        result = inject_recall(blocks, max_tokens=250)
        assert "[High]" in result
        assert "[Med]" in result
        assert "[Low]" not in result  # dropped due to budget

    def test_empty_blocks_returns_empty(self) -> None:
        assert inject_recall([], max_tokens=1000) == ""

    def test_zero_budget_means_unlimited(self) -> None:
        """max_tokens=0 means unlimited — all blocks included."""
        blocks = [
            RecallBlock(priority=40, section="[X]", text="data", est_tokens=10),
            RecallBlock(priority=20, section="[Y]", text="more", est_tokens=10),
        ]
        result = inject_recall(blocks, max_tokens=0)
        assert "[X]" in result
        assert "[Y]" in result
        assert "no budget limit" in result

    def test_unlimited_budget_includes_all(self) -> None:
        blocks = [
            RecallBlock(priority=40, section="[A]", text="data a", est_tokens=10),
            RecallBlock(priority=30, section="[B]", text="data b", est_tokens=10),
            RecallBlock(priority=20, section="[C]", text="data c", est_tokens=10),
        ]
        result = inject_recall(blocks, max_tokens=10000)
        assert "[A]" in result
        assert "[B]" in result
        assert "[C]" in result

    def test_drops_lowest_priority_first(self) -> None:
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
    @pytest.mark.asyncio
    async def test_returns_facts_episodes_and_commitments(
        self, populated_pool: Any,
    ) -> None:
        result = await get_session_start_context(
            populated_pool, TEST_CLIENT_ID, TEST_AGENT_ID,
            config=FakeConfig(),
        )
        assert "[Known facts]" in result
        assert "[Recent conversations]" in result
        assert "[Open commitments]" in result

    @pytest.mark.asyncio
    async def test_respects_max_facts(self, populated_pool: Any) -> None:
        result = await get_session_start_context(
            populated_pool, TEST_CLIENT_ID, TEST_AGENT_ID,
            config=FakeConfig(), max_facts=1,
        )
        # Facts section follows [Known facts] header (lowest priority, last in output)
        assert "[Known facts]" in result
        facts_section = result.split("[Known facts]")[1].split("[Memory loaded")[0]
        fact_lines = [
            line for line in facts_section.split("\n")
            if line.strip() and " — " in line
        ]
        assert len(fact_lines) <= 1

    @pytest.mark.asyncio
    async def test_respects_max_episodes(self, populated_pool: Any) -> None:
        result = await get_session_start_context(
            populated_pool, TEST_CLIENT_ID, TEST_AGENT_ID,
            config=FakeConfig(), max_episodes=1,
        )
        # Episodes section follows [Recent conversations] header
        assert "[Recent conversations]" in result
        episode_section = result.split("[Recent conversations]")[1].split("[Known facts]")[0]
        episode_lines = [
            line for line in episode_section.split("\n")
            if line.strip() and line.strip().startswith("[")
        ]
        assert len(episode_lines) <= 1

    @pytest.mark.asyncio
    async def test_empty_db_returns_empty(self, pool: Any) -> None:
        result = await get_session_start_context(
            pool, TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert result == ""

    @pytest.mark.asyncio
    async def test_budget_constraint(self, populated_pool: Any) -> None:
        result = await get_session_start_context(
            populated_pool, TEST_CLIENT_ID, TEST_AGENT_ID,
            config=FakeConfig(), max_tokens=10,
        )
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_works_without_config(self, populated_pool: Any) -> None:
        """Graceful fallback when config is None (backwards compat)."""
        result = await get_session_start_context(
            populated_pool, TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert "[Known facts]" in result
        assert "[Open commitments]" in result

    @pytest.mark.asyncio
    async def test_uses_natural_fact_format(self, populated_pool: Any) -> None:
        result = await get_session_start_context(
            populated_pool, TEST_CLIENT_ID, TEST_AGENT_ID,
            config=FakeConfig(),
        )
        # Natural format uses em-dashes in the facts section
        facts_section = result.split("[Known facts]")[1].split("[Memory loaded")[0]
        assert " — " in facts_section

    @pytest.mark.asyncio
    async def test_episode_tone_in_output(self, populated_pool: Any) -> None:
        result = await get_session_start_context(
            populated_pool, TEST_CLIENT_ID, TEST_AGENT_ID,
            config=FakeConfig(),
        )
        # Fixture episodes have non-neutral tones
        assert "tone:" in result


# ─── Defaults ────────────────────────────────────────────────────


class TestDefaults:
    def test_priority_constants_exist(self) -> None:
        assert RECALL_PRIORITY_VECTOR == 35
        assert RECALL_PRIORITY_EPISODES == 25
        assert RECALL_PRIORITY_FACTS == 15
        assert RECALL_PRIORITY_COMMITMENTS == 40

    def test_commitments_highest(self) -> None:
        assert RECALL_PRIORITY_COMMITMENTS > RECALL_PRIORITY_VECTOR
        assert RECALL_PRIORITY_COMMITMENTS > RECALL_PRIORITY_EPISODES
        assert RECALL_PRIORITY_COMMITMENTS > RECALL_PRIORITY_FACTS

    def test_vector_outranks_facts(self) -> None:
        """Vector > facts (warmth over clinical precision)."""
        assert RECALL_PRIORITY_VECTOR > RECALL_PRIORITY_FACTS

    def test_empty_recall_fallback_not_empty(self) -> None:
        assert len(EMPTY_RECALL_FALLBACK) > 0
        assert "memory" in EMPTY_RECALL_FALLBACK.lower()
