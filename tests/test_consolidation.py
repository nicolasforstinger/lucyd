"""Tests for consolidation.py — state tracking, serializer, fact/episode extraction."""

import json
import sqlite3
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from consolidation import (
    _normalize_entity,
    _resolve_entity,
    _strip_json_fences,
    consolidate_session,
    extract_episode,
    extract_facts,
    extract_from_file,
    get_unprocessed_range,
    serialize_messages,
    update_consolidation_state,
)
from memory_schema import ensure_schema


@pytest.fixture
def mem_conn(tmp_path):
    """In-memory SQLite DB with structured memory schema."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    yield conn
    conn.close()


def _make_provider(response_text: str):
    """Create a mock provider returning the given text."""

    @dataclass
    class FakeResponse:
        text: str

    provider = AsyncMock()
    provider.format_system.return_value = "system"
    provider.format_messages.return_value = "messages"
    provider.complete.return_value = FakeResponse(text=response_text)
    return provider


# ─── State Tracking ──────────────────────────────────────────────


class TestGetUnprocessedRange:
    def test_first_run_processes_everything(self, mem_conn):
        messages = [{"role": "user"}, {"role": "assistant"}] * 3
        start, end = get_unprocessed_range("sess1", messages, 0, mem_conn)
        assert start == 0
        assert end == 6

    def test_returns_last_to_n_after_previous(self, mem_conn):
        messages = [{"role": "user"}] * 10
        update_consolidation_state("sess1", 0, 6, mem_conn)

        start, end = get_unprocessed_range("sess1", messages, 0, mem_conn)
        assert start == 6
        assert end == 10

    def test_after_compaction_skips_summary(self, mem_conn):
        # compaction_count=0 was processed, now compaction_count=1
        update_consolidation_state("sess1", 0, 10, mem_conn)
        messages = [{"role": "assistant"}] * 5  # index 0 = summary

        start, end = get_unprocessed_range("sess1", messages, 1, mem_conn)
        assert start == 1
        assert end == 5

    def test_no_new_messages(self, mem_conn):
        messages = [{"role": "user"}] * 5
        update_consolidation_state("sess1", 0, 5, mem_conn)

        start, end = get_unprocessed_range("sess1", messages, 0, mem_conn)
        assert start == 0
        assert end == 0


class TestUpdateConsolidationState:
    def test_insert_new_state(self, mem_conn):
        update_consolidation_state("sess1", 0, 10, mem_conn)
        row = mem_conn.execute(
            "SELECT * FROM consolidation_state WHERE session_id = ?",
            ("sess1",),
        ).fetchone()
        assert row["last_message_count"] == 10
        assert row["last_compaction_count"] == 0

    def test_replace_existing_state(self, mem_conn):
        update_consolidation_state("sess1", 0, 5, mem_conn)
        update_consolidation_state("sess1", 0, 10, mem_conn)
        rows = mem_conn.execute(
            "SELECT * FROM consolidation_state WHERE session_id = ?",
            ("sess1",),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["last_message_count"] == 10

    def test_new_compaction_replaces_state(self, mem_conn):
        """Single PK on session_id — new compaction replaces the row."""
        update_consolidation_state("sess1", 0, 10, mem_conn)
        update_consolidation_state("sess1", 1, 5, mem_conn)
        row = mem_conn.execute(
            "SELECT * FROM consolidation_state WHERE session_id = ?",
            ("sess1",),
        ).fetchone()
        assert row["last_compaction_count"] == 1
        assert row["last_message_count"] == 5


# ─── Serializer ──────────────────────────────────────────────────


class TestSerializeMessages:
    def test_basic_serialization(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
        result = serialize_messages(messages, 0, 2)
        assert "Human: hello" in result
        assert "Assistant: world" in result

    def test_respects_max_chars_drops_oldest(self):
        messages = [
            {"role": "user", "content": "A" * 100},
            {"role": "user", "content": "B" * 100},
            {"role": "user", "content": "C" * 100},
        ]
        result = serialize_messages(messages, 0, 3, max_chars=150)
        # Should have dropped the oldest (A) and kept most recent
        assert "A" * 100 not in result
        assert "C" * 100 in result

    def test_truncates_tool_output(self):
        messages = [
            {"role": "tool_results", "results": [
                {"content": "X" * 5000}
            ]},
        ]
        result = serialize_messages(messages, 0, 1, max_tool_output=50)
        assert len(result) < 200  # truncated to 50 + prefix

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

    def test_serializes_tool_calls(self):
        messages = [
            {"role": "assistant", "content": "thinking",
             "tool_calls": [{"name": "web_search", "arguments": {"query": "test"}}]},
        ]
        result = serialize_messages(messages, 0, 1)
        assert "web_search" in result


# ─── Helpers ─────────────────────────────────────────────────────


class TestHelpers:
    def test_normalize_entity(self):
        assert _normalize_entity("Uncle Charles") == "uncle_charles"
        assert _normalize_entity("  Lucy  ") == "lucy"
        assert _normalize_entity("NICOLAS") == "nicolas"

    def test_resolve_entity_with_alias(self, mem_conn):
        mem_conn.execute(
            "INSERT INTO entity_aliases (alias, canonical) VALUES (?, ?)",
            ("lucy_belladonna", "lucy"),
        )
        assert _resolve_entity("lucy_belladonna", mem_conn) == "lucy"

    def test_resolve_entity_no_alias(self, mem_conn):
        assert _resolve_entity("unknown_entity", mem_conn) == "unknown_entity"

    def test_strip_json_fences(self):
        assert _strip_json_fences('```json\n{"a":1}\n```') == '{"a":1}'
        assert _strip_json_fences('```\n{"a":1}\n```') == '{"a":1}'
        assert _strip_json_fences('{"a":1}') == '{"a":1}'


# ─── Fact Extraction ─────────────────────────────────────────────


class TestExtractFacts:
    @pytest.mark.asyncio
    async def test_valid_json_stores_facts(self, mem_conn):
        response = json.dumps({
            "facts": [
                {"entity": "nicolas", "attribute": "lives_in", "value": "Austria", "confidence": 0.9},
                {"entity": "lucy", "attribute": "role", "value": "companion", "confidence": 1.0},
            ],
            "aliases": [],
        })
        provider = _make_provider(response)

        count = await extract_facts("test text", "sess1", provider, mem_conn)
        assert count == 2

        rows = mem_conn.execute(
            "SELECT entity, attribute, value FROM facts WHERE invalidated_at IS NULL"
        ).fetchall()
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_duplicate_fact_skipped(self, mem_conn):
        # Insert existing fact
        mem_conn.execute(
            "INSERT INTO facts (entity, attribute, value, confidence, source_session) "
            "VALUES ('nicolas', 'lives_in', 'Austria', 0.9, 'test')"
        )
        mem_conn.commit()

        response = json.dumps({
            "facts": [
                {"entity": "nicolas", "attribute": "lives_in", "value": "Austria", "confidence": 0.9},
            ],
            "aliases": [],
        })
        provider = _make_provider(response)

        count = await extract_facts("test text", "sess1", provider, mem_conn)
        assert count == 0  # duplicate, just touches accessed_at

    @pytest.mark.asyncio
    async def test_changed_value_invalidates_old(self, mem_conn):
        mem_conn.execute(
            "INSERT INTO facts (entity, attribute, value, confidence, source_session) "
            "VALUES ('nicolas', 'lives_in', 'Germany', 0.9, 'test')"
        )
        mem_conn.commit()

        response = json.dumps({
            "facts": [
                {"entity": "nicolas", "attribute": "lives_in", "value": "Austria", "confidence": 0.9},
            ],
            "aliases": [],
        })
        provider = _make_provider(response)

        count = await extract_facts("test text", "sess1", provider, mem_conn)
        assert count == 1

        # Old fact should be invalidated
        old = mem_conn.execute(
            "SELECT invalidated_at FROM facts WHERE value = 'Germany'"
        ).fetchone()
        assert old[0] is not None

        # New fact should exist
        new = mem_conn.execute(
            "SELECT value FROM facts WHERE entity = 'nicolas' "
            "AND attribute = 'lives_in' AND invalidated_at IS NULL"
        ).fetchone()
        assert new[0] == "Austria"

    @pytest.mark.asyncio
    async def test_below_confidence_dropped(self, mem_conn):
        response = json.dumps({
            "facts": [
                {"entity": "test", "attribute": "weak_fact", "value": "maybe", "confidence": 0.3},
            ],
            "aliases": [],
        })
        provider = _make_provider(response)

        count = await extract_facts("text", "sess1", provider, mem_conn, confidence_threshold=0.6)
        assert count == 0

    @pytest.mark.asyncio
    async def test_malformed_json_returns_zero(self, mem_conn):
        provider = _make_provider("this is not json at all")
        count = await extract_facts("text", "sess1", provider, mem_conn)
        assert count == 0

    @pytest.mark.asyncio
    async def test_aliases_stored(self, mem_conn):
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

        await extract_facts("text", "sess1", provider, mem_conn)

        alias = mem_conn.execute(
            "SELECT canonical FROM entity_aliases WHERE alias = 'charles'"
        ).fetchone()
        assert alias[0] == "uncle_charles"

    @pytest.mark.asyncio
    async def test_alias_resolution_in_same_batch(self, mem_conn):
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

        await extract_facts("text", "sess1", provider, mem_conn)

        fact = mem_conn.execute(
            "SELECT entity FROM facts WHERE attribute = 'age' AND invalidated_at IS NULL"
        ).fetchone()
        assert fact[0] == "uncle_charles"

    @pytest.mark.asyncio
    async def test_provider_error_returns_zero(self, mem_conn):
        provider = _make_provider("")
        provider.complete.side_effect = RuntimeError("API error")
        count = await extract_facts("text", "sess1", provider, mem_conn)
        assert count == 0

    @pytest.mark.asyncio
    async def test_strips_json_fences(self, mem_conn):
        response = '```json\n' + json.dumps({
            "facts": [{"entity": "test", "attribute": "a", "value": "b", "confidence": 1.0}],
            "aliases": [],
        }) + '\n```'
        provider = _make_provider(response)

        count = await extract_facts("text", "sess1", provider, mem_conn)
        assert count == 1


# ─── Episode Extraction ──────────────────────────────────────────


class TestExtractEpisode:
    @pytest.mark.asyncio
    async def test_valid_episode_stored(self, mem_conn):
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

        episode_id = await extract_episode(
            "test text", "sess1", provider,
            [{"text": "I am Lucy."}], mem_conn,
        )
        assert episode_id is not None

        ep = mem_conn.execute(
            "SELECT summary, emotional_tone FROM episodes WHERE id = ?",
            (episode_id,),
        ).fetchone()
        assert "memory system" in ep[0]
        assert ep[1] == "productive"

    @pytest.mark.asyncio
    async def test_commitments_linked_to_episode(self, mem_conn):
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

        episode_id = await extract_episode(
            "text", "sess1", provider, [{"text": "persona"}], mem_conn,
        )

        commits = mem_conn.execute(
            "SELECT who, what, deadline FROM commitments WHERE episode_id = ?",
            (episode_id,),
        ).fetchall()
        assert len(commits) == 2
        assert commits[0]["who"] == "lucy"

    @pytest.mark.asyncio
    async def test_trivial_episode_returns_none(self, mem_conn):
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

        result = await extract_episode(
            "text", "sess1", provider, [{"text": "persona"}], mem_conn,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_malformed_json_returns_none(self, mem_conn):
        provider = _make_provider("not json")
        result = await extract_episode(
            "text", "sess1", provider, [{"text": "persona"}], mem_conn,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_provider_error_returns_none(self, mem_conn):
        provider = _make_provider("")
        provider.complete.side_effect = RuntimeError("API fail")
        result = await extract_episode(
            "text", "sess1", provider, [{"text": "persona"}], mem_conn,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_null_deadline_handled(self, mem_conn):
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

        episode_id = await extract_episode(
            "text", "sess1", provider, [{"text": "persona"}], mem_conn,
        )
        commit = mem_conn.execute(
            "SELECT deadline FROM commitments WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        assert commit["deadline"] is None


# ─── File Extraction ─────────────────────────────────────────────


class TestExtractFromFile:
    @pytest.mark.asyncio
    async def test_new_file_extracted(self, mem_conn, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("# Nicolas lives in Austria.\n")

        response = json.dumps({
            "facts": [{"entity": "nicolas", "attribute": "lives_in",
                        "value": "Austria", "confidence": 1.0}],
            "aliases": [],
        })
        provider = _make_provider(response)

        count = await extract_from_file(str(f), provider, mem_conn)
        assert count == 1

        # Hash stored
        row = mem_conn.execute(
            "SELECT content_hash FROM consolidation_file_hashes WHERE file_path = ?",
            (str(f),),
        ).fetchone()
        assert row is not None

    @pytest.mark.asyncio
    async def test_unchanged_file_skipped(self, mem_conn, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("# Some content\n")

        response = json.dumps({"facts": [], "aliases": []})
        provider = _make_provider(response)

        # First run
        await extract_from_file(str(f), provider, mem_conn)
        # Second run — same content, should skip
        provider.complete.reset_mock()
        count = await extract_from_file(str(f), provider, mem_conn)
        assert count == 0
        provider.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_changed_file_reextracted(self, mem_conn, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("# Version 1\n")

        response = json.dumps({"facts": [], "aliases": []})
        provider = _make_provider(response)

        await extract_from_file(str(f), provider, mem_conn)

        # Change file content
        f.write_text("# Version 2\n")

        response2 = json.dumps({
            "facts": [{"entity": "test", "attribute": "version",
                        "value": "2", "confidence": 1.0}],
            "aliases": [],
        })
        provider2 = _make_provider(response2)

        await extract_from_file(str(f), provider2, mem_conn)
        # Provider was called (file changed)
        provider2.complete.assert_called_once()

    @pytest.mark.asyncio
    async def test_nonexistent_file_returns_zero(self, mem_conn):
        provider = _make_provider("")
        count = await extract_from_file("/nonexistent/path.md", provider, mem_conn)
        assert count == 0


# ─── consolidate_session ─────────────────────────────────────────


class TestConsolidateSession:
    @pytest.mark.asyncio
    async def test_full_pipeline(self, mem_conn):
        messages = [
            {"role": "user", "content": "Nicolas lives in Austria"},
            {"role": "assistant", "content": "Noted!"},
            {"role": "user", "content": "And he has a cat named Miso"},
            {"role": "assistant", "content": "Got it."},
            {"role": "user", "content": "Can you remember that?"},
        ]

        fact_response = json.dumps({
            "facts": [
                {"entity": "nicolas", "attribute": "lives_in", "value": "Austria", "confidence": 1.0},
                {"entity": "nicolas", "attribute": "cat_name", "value": "Miso", "confidence": 0.9},
            ],
            "aliases": [],
        })
        episode_response = json.dumps({
            "episode": {
                "topics": ["personal info"],
                "decisions": [],
                "commitments": [],
                "summary": "Nicolas shared personal details.",
                "emotional_tone": "warm",
            }
        })

        sub_provider = _make_provider(fact_response)
        primary_provider = _make_provider(episode_response)

        class FakeContextBuilder:
            def build_stable(self):
                return [{"text": "I am Lucy."}]

        class FakeConfig:
            consolidation_min_messages = 2
            consolidation_max_extraction_chars = 50000
            consolidation_confidence_threshold = 0.6

        result = await consolidate_session(
            session_id="sess1",
            messages=messages,
            compaction_count=0,
            config=FakeConfig(),
            subagent_provider=sub_provider,
            primary_provider=primary_provider,
            context_builder=FakeContextBuilder(),
            conn=mem_conn,
        )

        assert result["facts_added"] == 2
        assert result["episode_id"] is not None

    @pytest.mark.asyncio
    async def test_too_few_messages_skips(self, mem_conn):
        messages = [{"role": "user", "content": "hi"}]

        class FakeConfig:
            consolidation_min_messages = 4
            consolidation_max_extraction_chars = 50000
            consolidation_confidence_threshold = 0.6

        result = await consolidate_session(
            session_id="sess1",
            messages=messages,
            compaction_count=0,
            config=FakeConfig(),
            subagent_provider=_make_provider(""),
            primary_provider=_make_provider(""),
            context_builder=type("CB", (), {"build_stable": lambda self: []})(),
            conn=mem_conn,
        )

        assert result["facts_added"] == 0
        assert result["episode_id"] is None
