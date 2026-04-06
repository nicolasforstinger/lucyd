"""Tests for consolidation.py — state tracking, serializer, fact/episode extraction."""

import json
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory import resolve_entity as _resolve_entity
from consolidation import (
    _normalize_entity,
    _strip_json_fences,
    consolidate_session,
    extract_facts,
    extract_from_file,
    extract_structured_data,
    get_unprocessed_range,
    serialize_messages,
    update_consolidation_state,
)

TEST_CLIENT_ID = "test"
TEST_AGENT_ID = "test_agent"


def _make_provider(response_text: str):
    """Create a mock provider returning the given text."""

    @dataclass
    class FakeUsage:
        input_tokens: int = 100
        output_tokens: int = 50
        cache_read_tokens: int = 0
        cache_write_tokens: int = 0

    @dataclass
    class FakeResponse:
        text: str
        usage: FakeUsage = None

        def __post_init__(self):
            if self.usage is None:
                self.usage = FakeUsage()

    provider = MagicMock()
    provider.format_system.return_value = "system"
    provider.format_messages.return_value = "messages"
    provider.complete = AsyncMock(return_value=FakeResponse(text=response_text))
    return provider


# ─── State Tracking ──────────────────────────────────────────────


class TestGetUnprocessedRange:
    @pytest.mark.asyncio
    async def test_first_run_processes_everything(self, pool):
        messages = [{"role": "user"}, {"role": "assistant"}] * 3
        start, end = await get_unprocessed_range("sess1", messages, 0, pool, TEST_CLIENT_ID, TEST_AGENT_ID)
        assert start == 0
        assert end == 6

    @pytest.mark.asyncio
    async def test_returns_last_to_n_after_previous(self, pool):
        messages = [{"role": "user"}] * 10
        await update_consolidation_state("sess1", 0, 6, pool, TEST_CLIENT_ID, TEST_AGENT_ID)

        start, end = await get_unprocessed_range("sess1", messages, 0, pool, TEST_CLIENT_ID, TEST_AGENT_ID)
        assert start == 6
        assert end == 10

    @pytest.mark.asyncio
    async def test_after_compaction_skips_summary(self, pool):
        # compaction_count=0 was processed, now compaction_count=1
        await update_consolidation_state("sess1", 0, 10, pool, TEST_CLIENT_ID, TEST_AGENT_ID)
        messages = [{"role": "assistant"}] * 5  # index 0 = summary

        start, end = await get_unprocessed_range("sess1", messages, 1, pool, TEST_CLIENT_ID, TEST_AGENT_ID)
        assert start == 1
        assert end == 5

    @pytest.mark.asyncio
    async def test_no_new_messages(self, pool):
        messages = [{"role": "user"}] * 5
        await update_consolidation_state("sess1", 0, 5, pool, TEST_CLIENT_ID, TEST_AGENT_ID)

        start, end = await get_unprocessed_range("sess1", messages, 0, pool, TEST_CLIENT_ID, TEST_AGENT_ID)
        assert start == 0
        assert end == 0


class TestUpdateConsolidationState:
    @pytest.mark.asyncio
    async def test_insert_new_state(self, pool):
        await update_consolidation_state("sess1", 0, 10, pool, TEST_CLIENT_ID, TEST_AGENT_ID)
        row = await pool.fetchrow(
            "SELECT * FROM knowledge.consolidation_state "
            "WHERE client_id = $1 AND agent_id = $2 AND session_id = $3",
            TEST_CLIENT_ID, TEST_AGENT_ID, "sess1",
        )
        assert row["last_message_count"] == 10
        assert row["last_compaction_count"] == 0

    @pytest.mark.asyncio
    async def test_replace_existing_state(self, pool):
        await update_consolidation_state("sess1", 0, 5, pool, TEST_CLIENT_ID, TEST_AGENT_ID)
        await update_consolidation_state("sess1", 0, 10, pool, TEST_CLIENT_ID, TEST_AGENT_ID)
        rows = await pool.fetch(
            "SELECT * FROM knowledge.consolidation_state "
            "WHERE client_id = $1 AND agent_id = $2 AND session_id = $3",
            TEST_CLIENT_ID, TEST_AGENT_ID, "sess1",
        )
        assert len(rows) == 1
        assert rows[0]["last_message_count"] == 10

    @pytest.mark.asyncio
    async def test_new_compaction_replaces_state(self, pool):
        """Single PK on session_id — new compaction replaces the row."""
        await update_consolidation_state("sess1", 0, 10, pool, TEST_CLIENT_ID, TEST_AGENT_ID)
        await update_consolidation_state("sess1", 1, 5, pool, TEST_CLIENT_ID, TEST_AGENT_ID)
        row = await pool.fetchrow(
            "SELECT * FROM knowledge.consolidation_state "
            "WHERE client_id = $1 AND agent_id = $2 AND session_id = $3",
            TEST_CLIENT_ID, TEST_AGENT_ID, "sess1",
        )
        assert row["last_compaction_count"] == 1
        assert row["last_message_count"] == 5


# ─── Serializer ──────────────────────────────────────────────────


class TestSerializeMessages:
    def test_basic_serialization(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "text": "world"},
        ]
        result = serialize_messages(messages, 0, 2)
        assert "user: hello" in result
        assert "assistant: world" in result

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
            {"role": "tool_results", "results": [
                {"content": "X" * 5000}
            ]},
        ]
        result = serialize_messages(messages, 0, 1)
        assert result == ""  # tool_results are excluded from serialization

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
            {"role": "assistant", "text": "thinking about tools",
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
            "INSERT INTO knowledge.entity_aliases (client_id, agent_id, alias, canonical) "
            "VALUES ($1, $2, $3, $4)",
            TEST_CLIENT_ID, TEST_AGENT_ID, "alex_johnson", "alex",
        )
        assert await _resolve_entity("alex_johnson", pool, TEST_CLIENT_ID, TEST_AGENT_ID) == "alex"

    @pytest.mark.asyncio
    async def test_resolve_entity_no_alias(self, pool):
        assert await _resolve_entity("unknown_entity", pool, TEST_CLIENT_ID, TEST_AGENT_ID) == "unknown_entity"

    def test_strip_json_fences(self):
        assert _strip_json_fences('```json\n{"a":1}\n```') == '{"a":1}'
        assert _strip_json_fences('```\n{"a":1}\n```') == '{"a":1}'
        assert _strip_json_fences('{"a":1}') == '{"a":1}'


# ─── Fact Extraction ─────────────────────────────────────────────


class TestExtractFacts:
    @pytest.mark.asyncio
    async def test_valid_json_stores_facts(self, pool):
        response = json.dumps({
            "facts": [
                {"entity": "nicolas", "attribute": "lives_in", "value": "Austria", "confidence": 0.9},
                {"entity": "lucy", "attribute": "role", "value": "companion", "confidence": 1.0},
            ],
            "aliases": [],
        })
        provider = _make_provider(response)

        count, _ = await extract_facts("test text", "sess1", provider, pool, TEST_CLIENT_ID, TEST_AGENT_ID)
        assert count == 2

        rows = await pool.fetch(
            "SELECT entity, attribute, value FROM knowledge.facts "
            "WHERE client_id = $1 AND agent_id = $2 AND invalidated_at IS NULL",
            TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_duplicate_fact_skipped(self, pool):
        # Insert existing fact
        await pool.execute(
            "INSERT INTO knowledge.facts (client_id, agent_id, entity, attribute, value, confidence, source_session) "
            "VALUES ($1, $2, 'nicolas', 'lives_in', 'Austria', 0.9, 'test')",
            TEST_CLIENT_ID, TEST_AGENT_ID,
        )

        response = json.dumps({
            "facts": [
                {"entity": "nicolas", "attribute": "lives_in", "value": "Austria", "confidence": 0.9},
            ],
            "aliases": [],
        })
        provider = _make_provider(response)

        count, _ = await extract_facts("test text", "sess1", provider, pool, TEST_CLIENT_ID, TEST_AGENT_ID)
        assert count == 0  # duplicate, just touches accessed_at

    @pytest.mark.asyncio
    async def test_changed_value_invalidates_old(self, pool):
        await pool.execute(
            "INSERT INTO knowledge.facts (client_id, agent_id, entity, attribute, value, confidence, source_session) "
            "VALUES ($1, $2, 'nicolas', 'lives_in', 'Germany', 0.9, 'test')",
            TEST_CLIENT_ID, TEST_AGENT_ID,
        )

        response = json.dumps({
            "facts": [
                {"entity": "nicolas", "attribute": "lives_in", "value": "Austria", "confidence": 0.9},
            ],
            "aliases": [],
        })
        provider = _make_provider(response)

        count, _ = await extract_facts("test text", "sess1", provider, pool, TEST_CLIENT_ID, TEST_AGENT_ID)
        assert count == 1

        # Old fact should be invalidated
        old = await pool.fetchrow(
            "SELECT invalidated_at FROM knowledge.facts "
            "WHERE client_id = $1 AND agent_id = $2 AND value = 'Germany'",
            TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert old["invalidated_at"] is not None

        # New fact should exist
        new = await pool.fetchrow(
            "SELECT value FROM knowledge.facts "
            "WHERE client_id = $1 AND agent_id = $2 "
            "AND entity = 'nicolas' AND attribute = 'lives_in' AND invalidated_at IS NULL",
            TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert new["value"] == "Austria"

    @pytest.mark.asyncio
    async def test_below_confidence_dropped(self, pool):
        response = json.dumps({
            "facts": [
                {"entity": "test", "attribute": "weak_fact", "value": "maybe", "confidence": 0.3},
            ],
            "aliases": [],
        })
        provider = _make_provider(response)

        count, _ = await extract_facts("text", "sess1", provider, pool, TEST_CLIENT_ID, TEST_AGENT_ID, confidence_threshold=0.6)
        assert count == 0

    @pytest.mark.asyncio
    async def test_malformed_json_returns_zero(self, pool):
        provider = _make_provider("this is not json at all")
        count, _ = await extract_facts("text", "sess1", provider, pool, TEST_CLIENT_ID, TEST_AGENT_ID)
        assert count == 0

    @pytest.mark.asyncio
    async def test_aliases_stored(self, pool):
        response = json.dumps({
            "facts": [
                {"entity": "uncle_charles", "attribute": "relation", "value": "uncle", "confidence": 1.0},
            ],
            "aliases": [
                {"alias": "charles", "canonical": "uncle_charles"},
                {"alias": "uncle", "canonical": "uncle_charles"},
            ],
        })
        provider = _make_provider(response)

        await extract_facts("text", "sess1", provider, pool, TEST_CLIENT_ID, TEST_AGENT_ID)

        alias = await pool.fetchrow(
            "SELECT canonical FROM knowledge.entity_aliases "
            "WHERE client_id = $1 AND agent_id = $2 AND alias = 'charles'",
            TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert alias["canonical"] == "uncle_charles"

    @pytest.mark.asyncio
    async def test_alias_resolution_in_same_batch(self, pool):
        """Aliases stored first, so facts in same batch resolve through them."""
        response = json.dumps({
            "facts": [
                {"entity": "charles", "attribute": "age", "value": "60", "confidence": 1.0},
            ],
            "aliases": [
                {"alias": "charles", "canonical": "uncle_charles"},
            ],
        })
        provider = _make_provider(response)

        await extract_facts("text", "sess1", provider, pool, TEST_CLIENT_ID, TEST_AGENT_ID)

        fact = await pool.fetchrow(
            "SELECT entity FROM knowledge.facts "
            "WHERE client_id = $1 AND agent_id = $2 "
            "AND attribute = 'age' AND invalidated_at IS NULL",
            TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert fact["entity"] == "uncle_charles"

    @pytest.mark.asyncio
    async def test_provider_error_returns_zero(self, pool):
        provider = _make_provider("")
        provider.complete.side_effect = RuntimeError("API error")
        count, _ = await extract_facts("text", "sess1", provider, pool, TEST_CLIENT_ID, TEST_AGENT_ID)
        assert count == 0

    @pytest.mark.asyncio
    async def test_strips_json_fences(self, pool):
        response = '```json\n' + json.dumps({
            "facts": [{"entity": "test", "attribute": "a", "value": "b", "confidence": 1.0}],
            "aliases": [],
        }) + '\n```'
        provider = _make_provider(response)

        count, _ = await extract_facts("text", "sess1", provider, pool, TEST_CLIENT_ID, TEST_AGENT_ID)
        assert count == 1


# ─── Episode Extraction ──────────────────────────────────────────


class TestExtractEpisode:
    @pytest.mark.asyncio
    async def test_valid_episode_stored(self, pool):
        response = json.dumps({
            "episode": {
                "topics": ["memory system", "testing"],
                "decisions": ["use SQLite for facts"],
                "commitments": [
                    {"who": "nicolas", "what": "review the PR", "deadline": "2026-02-20"},
                ],
                "summary": "We discussed the memory system architecture.",
                "emotional_tone": "productive",
            }
        })
        provider = _make_provider(response)

        _, episode_id, _ = await extract_structured_data(
            "test text", "sess1", provider,
            [{"text": "I am Lucy."}], pool, TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert episode_id is not None

        ep = await pool.fetchrow(
            "SELECT summary, emotional_tone FROM knowledge.episodes "
            "WHERE client_id = $1 AND agent_id = $2 AND id = $3",
            TEST_CLIENT_ID, TEST_AGENT_ID, episode_id,
        )
        assert "memory system" in ep["summary"]
        assert ep["emotional_tone"] == "productive"

    @pytest.mark.asyncio
    async def test_commitments_linked_to_episode(self, pool):
        response = json.dumps({
            "episode": {
                "topics": ["planning"],
                "decisions": [],
                "commitments": [
                    {"who": "lucy", "what": "send morning briefing", "deadline": "2026-02-20"},
                    {"who": "nicolas", "what": "deploy update", "deadline": None},
                ],
                "summary": "Planning session for deployment.",
                "emotional_tone": "focused",
            }
        })
        provider = _make_provider(response)

        _, episode_id, _ = await extract_structured_data(
            "text", "sess1", provider, [{"text": "persona"}], pool, TEST_CLIENT_ID, TEST_AGENT_ID,
        )

        commits = await pool.fetch(
            "SELECT who, what, deadline FROM knowledge.commitments "
            "WHERE client_id = $1 AND agent_id = $2 AND episode_id = $3",
            TEST_CLIENT_ID, TEST_AGENT_ID, episode_id,
        )
        assert len(commits) == 2
        assert commits[0]["who"] == "lucy"

    @pytest.mark.asyncio
    async def test_trivial_episode_returns_none(self, pool):
        response = json.dumps({
            "episode": {
                "topics": [],
                "decisions": [],
                "commitments": [],
                "summary": "Brief mechanical exchange.",
                "emotional_tone": "neutral",
            }
        })
        provider = _make_provider(response)

        _, result, _ = await extract_structured_data(
            "text", "sess1", provider, [{"text": "persona"}], pool, TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_malformed_json_returns_none(self, pool):
        provider = _make_provider("not json")
        _, result, _ = await extract_structured_data(
            "text", "sess1", provider, [{"text": "persona"}], pool, TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_provider_error_returns_none(self, pool):
        provider = _make_provider("")
        provider.complete.side_effect = RuntimeError("API fail")
        _, result, _ = await extract_structured_data(
            "text", "sess1", provider, [{"text": "persona"}], pool, TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_null_deadline_handled(self, pool):
        """deadline: 'null' (string) should be stored as None."""
        response = json.dumps({
            "episode": {
                "topics": ["test"],
                "decisions": [],
                "commitments": [
                    {"who": "lucy", "what": "remember this", "deadline": "null"},
                ],
                "summary": "A conversation happened.",
                "emotional_tone": "warm",
            }
        })
        provider = _make_provider(response)

        _, episode_id, _ = await extract_structured_data(
            "text", "sess1", provider, [{"text": "persona"}], pool, TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        commit = await pool.fetchrow(
            "SELECT deadline FROM knowledge.commitments "
            "WHERE client_id = $1 AND agent_id = $2 AND episode_id = $3",
            TEST_CLIENT_ID, TEST_AGENT_ID, episode_id,
        )
        assert commit["deadline"] is None


# ─── File Extraction ─────────────────────────────────────────────


class TestExtractFromFile:
    @pytest.mark.asyncio
    async def test_new_file_extracted(self, pool, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("# Nicolas lives in Austria.\n")

        response = json.dumps({
            "facts": [{"entity": "nicolas", "attribute": "lives_in",
                        "value": "Austria", "confidence": 1.0}],
            "aliases": [],
        })
        provider = _make_provider(response)

        count = await extract_from_file(str(f), provider, pool, TEST_CLIENT_ID, TEST_AGENT_ID)
        assert count == 1

        # Hash stored
        row = await pool.fetchrow(
            "SELECT content_hash FROM knowledge.consolidation_file_hashes "
            "WHERE client_id = $1 AND agent_id = $2 AND file_path = $3",
            TEST_CLIENT_ID, TEST_AGENT_ID, str(f),
        )
        assert row is not None

    @pytest.mark.asyncio
    async def test_unchanged_file_skipped(self, pool, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("# Some content\n")

        response = json.dumps({"facts": [], "aliases": []})
        provider = _make_provider(response)

        # First run
        await extract_from_file(str(f), provider, pool, TEST_CLIENT_ID, TEST_AGENT_ID)
        # Second run — same content, should skip
        provider.complete.reset_mock()
        count = await extract_from_file(str(f), provider, pool, TEST_CLIENT_ID, TEST_AGENT_ID)
        assert count == 0
        provider.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_changed_file_reextracted(self, pool, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("# Version 1\n")

        response = json.dumps({"facts": [], "aliases": []})
        provider = _make_provider(response)

        await extract_from_file(str(f), provider, pool, TEST_CLIENT_ID, TEST_AGENT_ID)

        # Change file content
        f.write_text("# Version 2\n")

        response2 = json.dumps({
            "facts": [{"entity": "test", "attribute": "version",
                        "value": "2", "confidence": 1.0}],
            "aliases": [],
        })
        provider2 = _make_provider(response2)

        await extract_from_file(str(f), provider2, pool, TEST_CLIENT_ID, TEST_AGENT_ID)
        # Provider was called (file changed)
        provider2.complete.assert_called_once()

    @pytest.mark.asyncio
    async def test_nonexistent_file_returns_zero(self, pool):
        provider = _make_provider("")
        count = await extract_from_file("/nonexistent/path.md", provider, pool, TEST_CLIENT_ID, TEST_AGENT_ID)
        assert count == 0


# ─── consolidate_session ─────────────────────────────────────────


class TestConsolidateSession:
    @pytest.mark.asyncio
    async def test_full_pipeline(self, pool):
        messages = [
            {"role": "user", "content": "Nicolas lives in Austria"},
            {"role": "assistant", "content": "Noted!"},
            {"role": "user", "content": "And he has a cat named Miso"},
            {"role": "assistant", "content": "Got it."},
            {"role": "user", "content": "Can you remember that?"},
        ]

        combined_response = json.dumps({
            "facts": [
                {"entity": "nicolas", "attribute": "lives_in", "value": "Austria", "confidence": 1.0},
                {"entity": "nicolas", "attribute": "cat_name", "value": "Miso", "confidence": 0.9},
            ],
            "aliases": [],
            "episode": {
                "topics": ["personal info"],
                "decisions": [],
                "commitments": [],
                "summary": "Nicolas shared personal details.",
                "emotional_tone": "warm",
            },
        })

        # Single LLM call returns both facts and episode
        provider = _make_provider(combined_response)

        class FakeContextBuilder:
            def build_stable(self):
                return [{"text": "I am Lucy."}]

        class FakeConfig:
            consolidation_confidence_threshold = 0.6

        result = await consolidate_session(
            session_id="sess1",
            messages=messages,
            compaction_count=0,
            config=FakeConfig(),
            provider=provider,
            context_builder=FakeContextBuilder(),
            pool=pool,
            client_id=TEST_CLIENT_ID,
            agent_id=TEST_AGENT_ID,
        )

        assert result["facts_added"] == 2
        assert result["episode_id"] is not None

    @pytest.mark.asyncio
    async def test_too_few_messages_skips(self, pool):
        messages = [{"role": "user", "content": "hi"}]

        class FakeConfig:
            consolidation_confidence_threshold = 0.6

        result = await consolidate_session(
            session_id="sess1",
            messages=messages,
            compaction_count=0,
            config=FakeConfig(),
            provider=_make_provider(""),
            context_builder=type("CB", (), {"build_stable": lambda self: []})(),
            pool=pool,
            client_id=TEST_CLIENT_ID,
            agent_id=TEST_AGENT_ID,
        )

        assert result["facts_added"] == 0
        assert result["episode_id"] is None


class TestExtractThenLookupRoundTrip:
    """End-to-end: extract writes to real PostgreSQL, recall reads them back."""

    @pytest.mark.asyncio
    async def test_facts_round_trip(self, pool):
        """extract_facts -> lookup_facts returns matching facts."""
        from memory import lookup_facts

        response = json.dumps({
            "facts": [
                {"entity": "nicolas", "attribute": "lives_in", "value": "Austria", "confidence": 0.9},
                {"entity": "nicolas", "attribute": "cat_name", "value": "Miso", "confidence": 0.85},
            ],
            "aliases": [],
        })
        provider = _make_provider(response)
        count, _ = await extract_facts("test conversation", "sess-rt", provider, pool, TEST_CLIENT_ID, TEST_AGENT_ID)
        assert count == 2

        facts = await lookup_facts({"nicolas"}, pool, TEST_CLIENT_ID, TEST_AGENT_ID)
        assert len(facts) == 2
        attrs = {f["attribute"] for f in facts}
        assert attrs == {"lives_in", "cat_name"}

    @pytest.mark.asyncio
    async def test_episodes_round_trip(self, pool):
        """extract_structured_data -> search_episodes returns the episode."""
        from memory import search_episodes

        response = json.dumps({
            "episode": {
                "topics": ["memory architecture", "sqlite"],
                "decisions": ["use WAL mode"],
                "commitments": [],
                "summary": "Discussed memory system architecture.",
                "emotional_tone": "productive",
            }
        })
        provider = _make_provider(response)
        _, episode_id, _ = await extract_structured_data(
            "test text", "sess-rt", provider,
            [{"text": "I am an agent."}], pool, TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert episode_id is not None

        episodes = await search_episodes(["memory"], pool, TEST_CLIENT_ID, TEST_AGENT_ID)
        assert len(episodes) >= 1
        summaries = [e["summary"] for e in episodes]
        assert any("memory" in s.lower() for s in summaries)

    @pytest.mark.asyncio
    async def test_commitments_round_trip(self, pool):
        """extract_structured_data with commitments -> get_open_commitments returns them."""
        from memory import get_open_commitments

        response = json.dumps({
            "episode": {
                "topics": ["planning"],
                "decisions": [],
                "commitments": [
                    {"who": "nicolas", "what": "review the PR", "deadline": "2026-03-01"},
                ],
                "summary": "Planning session.",
                "emotional_tone": "focused",
            }
        })
        provider = _make_provider(response)
        _, episode_id, _ = await extract_structured_data(
            "text", "sess-rt", provider,
            [{"text": "persona"}], pool, TEST_CLIENT_ID, TEST_AGENT_ID,
        )
        assert episode_id is not None

        commits = await get_open_commitments(pool, TEST_CLIENT_ID, TEST_AGENT_ID)
        assert len(commits) >= 1
        assert any(c["what"] == "review the PR" for c in commits)
