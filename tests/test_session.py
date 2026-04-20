"""Tests for session.py — PostgreSQL persistence, state, compaction, persist methods."""

import json

import pytest

from providers import CostContext
from session import (
    AUDIT_TRUNCATION_LIMIT,
    Session,
    SessionManager,
    _context_tokens_from_usage,
    _text_from_content,
    _validate_turn_structure,
    build_session_info,
    read_history_events,
)

TEST_CLIENT_ID = "test"
TEST_AGENT_ID = "test_agent"


# ── Test helpers for the production call path (bundles) ─────────────
# Values are test constants, NOT config defaults — config is the
# single source of truth in lucyd.toml; tests just need known values.

_TEST_COMPACTION = dict(
    keep_recent_pct=0.33,
    min_messages=4,
    tool_result_max_chars=2000,
    max_tokens=2048,
)

_TEST_COST = CostContext(
    metering=None,
    session_id="",
    model_name="test",
    cost_rates=[],
)


class MockCompactionProvider:
    """Minimal mock provider for compaction tests."""

    def __init__(self, summary_text: str = "Mock summary."):
        self.summary_text = summary_text
        self.call_count = 0

    def format_system(self, blocks):
        return [{"type": "text", "text": b["text"]} for b in blocks]

    def format_messages(self, messages):
        return [{"role": m["role"], "content": m.get("content", "")} for m in messages]

    async def complete(self, system, messages, tools, **kwargs):
        self.call_count += 1
        from providers import LLMResponse, Usage
        return LLMResponse(
            text=self.summary_text,
            tool_calls=[],
            stop_reason="end_turn",
            usage=Usage(input_tokens=50, output_tokens=30),
        )


# ── Helper: create a DB-backed session with a row in sessions.sessions ──

async def _create_session(pool, session_id, model="", contact=""):
    """Insert a sessions.sessions row and return a Session object."""
    await pool.execute(
        """INSERT INTO sessions.sessions (id, contact, model)
           VALUES ($1, $2, $3)""",
        session_id, contact, model,
    )
    return Session(session_id, pool, model=model, contact=contact)


# ─── Event Persistence ──────────────────────────────────────────────


class TestEventPersistence:
    @pytest.mark.asyncio
    async def test_append_creates_event_row(self, pool):
        session = await _create_session(pool, "test-abc")
        await session.append_event({"type": "message", "role": "user", "content": "hello"})

        rows = await pool.fetch(
            "SELECT * FROM sessions.events WHERE session_id = $1", "test-abc"
        )
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload"])
        assert payload["content"] == "hello"

    @pytest.mark.asyncio
    async def test_multiple_appends(self, pool):
        session = await _create_session(pool, "test-abc-multi")
        await session.append_event({"type": "message", "role": "user", "content": "first"})
        await session.append_event({"type": "message", "role": "user", "content": "second"})

        rows = await pool.fetch(
            "SELECT payload FROM sessions.events WHERE session_id = $1 ORDER BY created_at",
            "test-abc-multi",
        )
        assert len(rows) == 2
        assert json.loads(rows[0]["payload"])["content"] == "first"
        assert json.loads(rows[1]["payload"])["content"] == "second"


# ─── State Round-Trip ───────────────────────────────────────────────


class TestStateRoundTrip:
    @pytest.mark.asyncio
    async def test_state_preserves_compaction_fields(self, pool):
        session = await _create_session(pool, "test-state")
        session.compaction_count = 3
        session.warned_about_compaction = True
        session.pending_system_warning = "Context at 130k tokens"
        session.messages = [{"role": "user", "content": "test"}]
        await session.save_state()

        loaded = Session("test-state", pool)
        assert await loaded.load() is True
        assert loaded.compaction_count == 3
        assert loaded.warned_about_compaction is True
        assert loaded.pending_system_warning == "Context at 130k tokens"
        assert len(loaded.messages) == 1

    @pytest.mark.asyncio
    async def test_warning_survives_reload(self, pool):
        """Warning persists across save/load without being consumed."""
        session = await _create_session(pool, "warn-persist")
        session.pending_system_warning = "Context at 130k"
        session.messages = [{"role": "user", "content": "x"}]
        await session.save_state()

        loaded = Session("warn-persist", pool)
        await loaded.load()
        assert loaded.pending_system_warning == "Context at 130k"

    @pytest.mark.asyncio
    async def test_warning_cleared_is_persisted(self, pool):
        """After clearing the warning and saving, reload shows empty."""
        session = await _create_session(pool, "warn-clear")
        session.pending_system_warning = "Context at 130k"
        session.messages = [{"role": "user", "content": "x"}]
        await session.save_state()

        loaded = Session("warn-clear", pool)
        await loaded.load()
        loaded.pending_system_warning = ""
        await loaded.save_state()

        reloaded = Session("warn-clear", pool)
        await reloaded.load()
        assert reloaded.pending_system_warning == ""

    @pytest.mark.asyncio
    async def test_warning_absent_defaults_empty(self, pool):
        """Session without warning set defaults to empty string."""
        session = await _create_session(pool, "no-warn")
        session.messages = [{"role": "user", "content": "x"}]
        await session.save_state()

        loaded = Session("no-warn", pool)
        await loaded.load()
        assert loaded.pending_system_warning == ""

    @pytest.mark.asyncio
    async def test_duplicate_warning_overwrites(self, pool):
        """Setting warning again overwrites (no duplication)."""
        session = await _create_session(pool, "warn-dup")
        session.pending_system_warning = "First warning"
        session.pending_system_warning = "Second warning"
        session.messages = [{"role": "user", "content": "x"}]
        await session.save_state()

        loaded = Session("warn-dup", pool)
        await loaded.load()
        assert loaded.pending_system_warning == "Second warning"


# ─── Compaction Warning ─────────────────────────────────────────────


class TestCompactionWarning:
    def test_needs_compaction_above_threshold(self):
        session = Session("test-warn", None)
        session.messages = [{
            "role": "agent",
            "text": "response",
            "usage": {"input_tokens": 160000, "output_tokens": 500},
        }]
        assert session.needs_compaction(150000) is True

    def test_no_compaction_below_threshold(self):
        session = Session("test-ok", None)
        session.messages = [{
            "role": "agent",
            "text": "response",
            "usage": {"input_tokens": 100000, "output_tokens": 500},
        }]
        assert session.needs_compaction(150000) is False


# ─── Persist Methods ────────────────────────────────────────────────


class TestPersistMethods:
    """Tests for add_assistant_message(persist_only=True) and add_tool_results(persist_only=True)."""

    @pytest.mark.asyncio
    async def test_persist_assistant_message_updates_tokens(self, pool):
        session = await _create_session(pool, "test-persist")
        assert session.total_input_tokens == 0
        assert session.total_output_tokens == 0

        msg = {
            "role": "agent", "text": "hello",
            "usage": {"input_tokens": 1000, "output_tokens": 200},
        }
        await session.add_assistant_message(msg, persist_only=True)

        assert session.total_input_tokens == 1000
        assert session.total_output_tokens == 200
        # Should NOT have appended to messages list
        assert len(session.messages) == 0

    @pytest.mark.asyncio
    async def test_persist_assistant_message_writes_event(self, pool):
        session = await _create_session(pool, "test-persist-j")
        msg = {
            "role": "agent", "text": "hi",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        await session.add_assistant_message(msg, persist_only=True)

        rows = await pool.fetch(
            "SELECT payload FROM sessions.events WHERE session_id = $1",
            "test-persist-j",
        )
        assert len(rows) == 1
        event = json.loads(rows[0]["payload"])
        assert event["type"] == "message"
        assert event["role"] == "agent"

    @pytest.mark.asyncio
    async def test_persist_tool_results_writes_events(self, pool):
        session = await _create_session(pool, "test-persist-t")
        results = [
            {"tool_call_id": "tc1", "content": "result one"},
            {"tool_call_id": "tc2", "content": "result two"},
        ]
        await session.add_tool_results(results, persist_only=True)

        rows = await pool.fetch(
            "SELECT payload FROM sessions.events WHERE session_id = $1 ORDER BY created_at",
            "test-persist-t",
        )
        assert len(rows) == 2
        assert json.loads(rows[0]["payload"])["tool_use_id"] == "tc1"
        assert json.loads(rows[1]["payload"])["tool_use_id"] == "tc2"

    @pytest.mark.asyncio
    async def test_persist_tool_results_truncates(self, pool):
        session = await _create_session(pool, "test-trunc")
        long_content = "x" * 1000
        results = [{"tool_call_id": "tc1", "content": long_content}]
        await session.add_tool_results(results, persist_only=True)

        rows = await pool.fetch(
            "SELECT payload FROM sessions.events WHERE session_id = $1",
            "test-trunc",
        )
        event = json.loads(rows[0]["payload"])
        assert len(event["content"]) == AUDIT_TRUNCATION_LIMIT


# ─── Audit Truncation Limit ────────────────────────────────────────


class TestAuditTruncationLimit:
    def test_constant_value(self):
        assert AUDIT_TRUNCATION_LIMIT == 500

    @pytest.mark.asyncio
    async def test_add_tool_results_truncates(self, pool):
        session = await _create_session(pool, "test-trunc2")
        long_content = "y" * 1000
        await session.add_tool_results([{"tool_call_id": "tc1", "content": long_content}])

        rows = await pool.fetch(
            "SELECT payload FROM sessions.events WHERE session_id = $1 "
            "AND event_type = 'tool_result'",
            "test-trunc2",
        )
        event = json.loads(rows[0]["payload"])
        assert len(event["content"]) == AUDIT_TRUNCATION_LIMIT


# ─── Session Manager ───────────────────────────────────────────────


class TestSessionManager:
    @pytest.mark.asyncio
    async def test_get_or_create_new(self, pool):
        mgr = SessionManager(pool)
        session = await mgr.get_or_create("user1", model="test")
        assert session is not None
        assert session.id  # Should have a UUID

    @pytest.mark.asyncio
    async def test_get_or_create_returns_same(self, pool):
        mgr = SessionManager(pool)
        s1 = await mgr.get_or_create("user1")
        s2 = await mgr.get_or_create("user1")
        assert s1.id == s2.id

    @pytest.mark.asyncio
    async def test_different_contacts_different_sessions(self, pool):
        mgr = SessionManager(pool)
        s1 = await mgr.get_or_create("user1")
        s2 = await mgr.get_or_create("user2")
        assert s1.id != s2.id


# ─── Message Order ──────────────────────────────────────────────────


class TestMessageOrder:
    @pytest.mark.asyncio
    async def test_add_user_then_assistant_preserves_order(self, pool):
        session = await _create_session(pool, "test-order")
        await session.add_user_message("hello", sender="test", source="cli")
        session.messages.append({
            "role": "agent", "text": "hi back",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })

        assert session.messages[0]["role"] == "user"
        assert session.messages[0]["content"] == "hello"
        assert session.messages[1]["role"] == "agent"
        assert session.messages[1]["text"] == "hi back"


# ─── Compaction End-to-End ──────────────────────────────────────────


class TestCompactionEndToEnd:
    """TEST-4: Verify SessionManager.compact_session end-to-end."""

    @pytest.fixture
    async def six_message_session(self, pool):
        """Create a session with 6 messages (3 user + 3 assistant with usage)."""
        session = await _create_session(pool, "test-e2e-compact")
        for i in range(3):
            session.messages.append(
                {"role": "user", "content": f"user message {i}"}
            )
            session.messages.append(
                {
                    "role": "agent",
                    "text": f"assistant reply {i}",
                    "usage": {"input_tokens": 100 * (i + 1), "output_tokens": 50 * (i + 1)},
                }
            )
        return session

    @pytest.mark.asyncio
    async def test_compact_replaces_messages_with_summary_plus_recent(
        self, pool, six_message_session
    ):
        """After compaction, messages = [summary_msg] + recent_messages."""
        session = six_message_session
        mgr = SessionManager(pool)

        mock_provider = MockCompactionProvider(summary_text="Summary of old conversation.")
        await mgr.compact_session(
            session, mock_provider, "Summarize this conversation.",
            cost=_TEST_COST, **_TEST_COMPACTION,
        )

        # split_point = 6 * 2 // 3 = 4, so 4 old, 2 recent
        # Result: 1 summary + 1 compaction marker + 2 recent = 4
        assert len(session.messages) == 4
        assert "[Previous conversation summary]" in session.messages[0]["content"]
        assert "Summary of old conversation." in session.messages[0]["content"]
        # Compaction marker
        assert "[system: This conversation was compacted" in session.messages[1]["content"]
        # Recent messages preserved
        assert session.messages[2]["role"] == "user"
        assert session.messages[2]["content"] == "user message 2"
        assert session.messages[3]["role"] == "agent"
        assert session.messages[3]["text"] == "assistant reply 2"

    @pytest.mark.asyncio
    async def test_compact_increments_compaction_count(
        self, pool, six_message_session
    ):
        session = six_message_session
        mgr = SessionManager(pool)
        assert session.compaction_count == 0

        mock_provider = MockCompactionProvider(summary_text="Summary.")
        await mgr.compact_session(
            session, mock_provider, "Summarize.",
            cost=_TEST_COST, **_TEST_COMPACTION,
        )

        assert session.compaction_count == 1

        # Compact again (add messages to get back above threshold)
        for i in range(4):
            session.messages.append({"role": "user", "content": f"extra {i}"})
        await mgr.compact_session(
            session, mock_provider, "Summarize.",
            cost=_TEST_COST, **_TEST_COMPACTION,
        )
        assert session.compaction_count == 2

    @pytest.mark.asyncio
    async def test_compact_writes_compaction_event(
        self, pool, six_message_session
    ):
        session = six_message_session
        mgr = SessionManager(pool)

        mock_provider = MockCompactionProvider(summary_text="Event summary test.")
        await mgr.compact_session(
            session, mock_provider, "Summarize.",
            cost=_TEST_COST, **_TEST_COMPACTION,
        )

        rows = await pool.fetch(
            "SELECT payload FROM sessions.events WHERE session_id = $1 "
            "AND event_type = 'compaction'",
            "test-e2e-compact",
        )
        assert len(rows) >= 1
        event = json.loads(rows[0]["payload"])
        assert event["type"] == "compaction"
        assert event["compaction_number"] == 1
        assert event["removed_messages"] == 4  # 6 * 2 // 3
        assert "Event summary test." in event["summary"]

    @pytest.mark.asyncio
    async def test_compact_saves_state(self, pool, six_message_session):
        session = six_message_session
        mgr = SessionManager(pool)

        mock_provider = MockCompactionProvider(summary_text="State save test.")
        await mgr.compact_session(
            session, mock_provider, "Summarize.",
            cost=_TEST_COST, **_TEST_COMPACTION,
        )

        # Reload from DB and verify compacted state
        reloaded = Session("test-e2e-compact", pool)
        await reloaded.load()
        assert reloaded.compaction_count == 1
        assert len(reloaded.messages) == 4  # 1 summary + 1 compaction marker + 2 recent

    @pytest.mark.asyncio
    async def test_compact_skips_when_fewer_than_4_messages(self, pool):
        """Sessions with < 4 messages should not be compacted."""
        session = await _create_session(pool, "test-skip-compact")
        session.messages = [
            {"role": "user", "content": "hello"},
            {"role": "agent", "text": "hi", "usage": {"input_tokens": 10, "output_tokens": 5}},
            {"role": "user", "content": "bye"},
        ]
        mgr = SessionManager(pool)
        mock_provider = MockCompactionProvider(summary_text="Should not appear.")
        await mgr.compact_session(
            session, mock_provider, "Summarize.",
            cost=_TEST_COST, **_TEST_COMPACTION,
        )

        # Messages unchanged
        assert len(session.messages) == 3
        assert session.compaction_count == 0
        # Provider should not have been called
        assert mock_provider.call_count == 0

    @pytest.mark.asyncio
    async def test_compact_resets_warned_flag(self, pool, six_message_session):
        """Compaction should reset warned_about_compaction to False."""
        session = six_message_session
        session.warned_about_compaction = True
        mgr = SessionManager(pool)

        mock_provider = MockCompactionProvider(summary_text="Reset flag test.")
        await mgr.compact_session(
            session, mock_provider, "Summarize.",
            cost=_TEST_COST, **_TEST_COMPACTION,
        )

        assert session.warned_about_compaction is False

    @pytest.mark.asyncio
    async def test_compact_custom_keep_recent_pct(self, pool):
        """keep_recent_pct controls how many recent messages are kept verbatim."""
        session = await _create_session(pool, "test-keep-pct")
        for i in range(10):
            session.messages.append({"role": "user", "content": f"msg {i}"})
            session.messages.append({
                "role": "agent", "text": f"reply {i}",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            })
        assert len(session.messages) == 20
        mgr = SessionManager(pool)
        mock_provider = MockCompactionProvider(summary_text="Summary.")

        # keep_recent_pct=0.25 -> split_point = int(20 * 0.75) = 15
        # 15 old -> summary, 5 recent kept
        # Result: 1 summary + 1 marker + 5 recent = 7
        await mgr.compact_session(
            session, mock_provider, "Summarize.",
            cost=_TEST_COST, **{**_TEST_COMPACTION, "keep_recent_pct": 0.25},
        )
        assert len(session.messages) == 7
        assert "[Previous conversation summary]" in session.messages[0]["content"]
        # Last message should be the final assistant reply
        assert session.messages[-1]["text"] == "reply 9"

    @pytest.mark.asyncio
    async def test_compact_keep_recent_pct_clamped(self, pool):
        """keep_recent_pct clamping now happens in config.py, not session.py.

        compact_session uses keep_recent_pct directly (no re-clamping).
        With keep_recent_pct=0.0, all messages are old -> summary + marker only.
        """
        session = await _create_session(pool, "test-clamp")
        for i in range(10):
            session.messages.append({"role": "user", "content": f"msg {i}"})
            session.messages.append({
                "role": "agent", "text": f"reply {i}",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            })
        mgr = SessionManager(pool)
        mock_provider = MockCompactionProvider(summary_text="Summary.")

        # keep_recent_pct=0.0 -> split_point = int(20 * 1.0) = 20
        # All 20 old -> summary + marker, 0 recent kept
        await mgr.compact_session(
            session, mock_provider, "Summarize.",
            cost=_TEST_COST, **{**_TEST_COMPACTION, "keep_recent_pct": 0.0},
        )
        assert len(session.messages) == 2  # summary + marker, no recent kept

    @pytest.mark.asyncio
    async def test_compact_skips_orphaned_tool_results(self, pool):
        """Compaction split must not leave tool_result without matching tool_use."""
        session = await _create_session(pool, "test-tool-boundary")
        # Build: 8 user/assistant pairs + 1 tool exchange + 1 user/assistant
        for i in range(8):
            session.messages.append({"role": "user", "content": f"msg {i}"})
            session.messages.append({
                "role": "agent", "text": f"reply {i}",
                "tool_calls": [], "usage": {"input_tokens": 100, "output_tokens": 50},
            })
        # Tool exchange at positions 16-18
        session.messages.append({
            "role": "agent", "text": "",
            "tool_calls": [{"id": "tool_1", "name": "tts", "arguments": {"text": "hi"}}],
            "usage": {"input_tokens": 100, "output_tokens": 50},
        })
        session.messages.append({
            "role": "tool_result",
            "results": [{"tool_call_id": "tool_1", "content": "sent"}],
        })
        session.messages.append({
            "role": "agent", "text": "done",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        })
        # Final pair at 19-20
        session.messages.append({"role": "user", "content": "last msg"})
        session.messages.append({
            "role": "agent", "text": "last reply",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        })
        # 21 messages total. keep_recent_pct=0.25 -> split at int(21*0.75)=15
        # Position 15 is an assistant (reply 7). No tool_result -> split unchanged.
        # But if we force split to land on tool_result (index 17):
        # keep_recent_pct such that split = 17 -> 17/21 = 0.81 -> 1-pct = 0.19
        # split_point = int(21 * 0.81) = 17 -> message[17] is tool_result
        mgr = SessionManager(pool)
        mock_provider = MockCompactionProvider(summary_text="Summary.")

        await mgr.compact_session(
            session, mock_provider, "Summarize.",
            cost=_TEST_COST, **{**_TEST_COMPACTION, "keep_recent_pct": 0.19},
        )
        # Split should skip past tool_result at index 17 to index 18 (assistant)
        # old=18, recent=3 -> summary + marker + 3 = 5
        # Verify no tool_result in first position of recent messages
        assert session.messages[0]["content"].startswith("[Previous conversation summary]")
        for msg in session.messages[2:]:
            if msg.get("role") == "tool_result":
                # Any remaining tool_result must have a preceding assistant with tool_use
                idx = session.messages.index(msg)
                prev = session.messages[idx - 1]
                assert prev.get("role") == "agent"
                assert prev.get("tool_calls"), "tool_result without preceding tool_use"


# ─── Compaction Round-Trip ──────────────────────────────────────────


class TestCompactionRoundTrip:
    """End-to-end compaction: real Session, mock only the LLM provider."""

    @pytest.mark.asyncio
    async def test_round_trip(self, pool):
        """Compact a 30-message session, verify structure and provider input."""
        session = await _create_session(pool, "test-roundtrip")
        for i in range(15):
            session.messages.append({"role": "user", "content": f"user msg {i}"})
            session.messages.append({
                "role": "agent", "text": f"reply {i}",
                "content": f"reply {i}",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            })
        await session.save_state()
        assert len(session.messages) == 30

        # Capture what the provider receives
        captured_input = []
        provider = MockCompactionProvider(summary_text="Round-trip summary.")
        original_complete = provider.complete

        async def capturing_complete(system, messages, tools, **kwargs):
            captured_input.extend(messages)
            return await original_complete(system, messages, tools, **kwargs)

        provider.complete = capturing_complete

        mgr = SessionManager(pool)
        await mgr.compact_session(
            session, provider, "Summarize this conversation.",
            cost=_TEST_COST, **_TEST_COMPACTION,
        )

        # split_point = 30 * 2 // 3 = 20 old, 10 recent
        # Result: 1 summary + 1 compaction marker + 10 recent = 12
        assert len(session.messages) == 12
        assert "[Previous conversation summary]" in session.messages[0]["content"]
        assert "Round-trip summary." in session.messages[0]["content"]
        # Compaction marker
        assert "[system: This conversation was compacted" in session.messages[1]["content"]

        # Recent 10 messages preserved unchanged
        assert session.messages[2]["content"] == "user msg 10"
        assert session.messages[-1]["text"] == "reply 14"

        assert session.compaction_count == 1
        assert session.warned_about_compaction is False

        # DB reflects compacted state
        reloaded = Session("test-roundtrip", pool)
        await reloaded.load()
        assert reloaded.compaction_count == 1
        assert len(reloaded.messages) == 12

        # Provider received the oldest 2/3 as formatted text
        # 30 msgs, split_point=20: old = indices 0-19 (user 0..9, reply 0..9)
        assert len(captured_input) == 1  # single user message with conversation text
        sent_text = captured_input[0]["content"]
        assert "user msg 0" in sent_text
        assert "user msg 9" in sent_text   # last user message in old 2/3
        assert "reply 9" in sent_text      # last assistant message in old 2/3
        # Recent 1/3 messages NOT in compaction input
        assert "user msg 10" not in sent_text
        assert "reply 14" not in sent_text

    @pytest.mark.asyncio
    async def test_double_compaction_preserves_prior_summary(self, pool):
        """Second compaction includes first summary in its input."""
        session = await _create_session(pool, "test-double")

        # Round 1: 30 messages
        for i in range(15):
            session.messages.append({"role": "user", "content": f"r1 msg {i}"})
            session.messages.append({
                "role": "agent", "text": f"r1 reply {i}",
                "content": f"r1 reply {i}",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            })
        await session.save_state()

        mgr = SessionManager(pool)
        provider_a = MockCompactionProvider(summary_text="Summary A.")
        await mgr.compact_session(
            session, provider_a, "Summarize.",
            cost=_TEST_COST, **_TEST_COMPACTION,
        )
        assert session.compaction_count == 1
        assert "Summary A." in session.messages[0]["content"]

        # Round 2: add 30 more messages, then compact again
        for i in range(15):
            session.messages.append({"role": "user", "content": f"r2 msg {i}"})
            session.messages.append({
                "role": "agent", "text": f"r2 reply {i}",
                "content": f"r2 reply {i}",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            })
        await session.save_state()

        # Capture round 2 input
        captured_input = []
        provider_b = MockCompactionProvider(summary_text="Summary B.")
        original_complete = provider_b.complete

        async def capturing_complete(system, messages, tools, **kwargs):
            captured_input.extend(messages)
            return await original_complete(system, messages, tools, **kwargs)

        provider_b.complete = capturing_complete

        await mgr.compact_session(
            session, provider_b, "Summarize.",
            cost=_TEST_COST, **_TEST_COMPACTION,
        )

        assert session.compaction_count == 2
        # Current messages start with Summary B, not Summary A
        assert "Summary B." in session.messages[0]["content"]

        # Summary A was in the old 2/3 and sent to the provider
        sent_text = captured_input[0]["content"]
        assert "Summary A." in sent_text


# ─── Compaction Replaces Messages ───────────────────────────────────


class TestCompactionReplacesMessages:
    """Compaction replaces old messages with summary, keeps recent."""

    @pytest.mark.asyncio
    async def test_compaction_replaces_not_appends(self, pool):
        """After compaction, old messages are GONE, replaced by summary."""
        session = await _create_session(pool, "test-replace")
        for i in range(6):
            session.messages.append({"role": "user", "content": f"msg-{i}"})
        mgr = SessionManager(pool)
        mock = MockCompactionProvider("Summary text.")
        await mgr.compact_session(
            session, mock, "Summarize.",
            cost=_TEST_COST, **_TEST_COMPACTION,
        )

        # Old messages should be gone
        contents = [m.get("content", "") for m in session.messages]
        assert "msg-0" not in contents
        assert "msg-1" not in contents
        assert "msg-2" not in contents
        # Summary should be first
        assert "Summary text." in session.messages[0]["content"]

    @pytest.mark.asyncio
    async def test_compaction_strips_stale_usage(self, pool):
        """After compaction, surviving assistant messages have no stale usage."""
        session = await _create_session(pool, "test-usage-strip")
        # Build a conversation with assistant messages carrying usage data
        for i in range(10):
            session.messages.append({"role": "user", "content": f"msg-{i}"})
            session.messages.append({
                "role": "agent", "text": f"reply-{i}",
                "usage": {"context_tokens": 50000, "input_tokens": 40000,
                          "output_tokens": 200, "cache_read_tokens": 10000},
            })
        mgr = SessionManager(pool)
        mock = MockCompactionProvider("Summary.")
        await mgr.compact_session(
            session, mock, "Summarize.",
            cost=_TEST_COST, **_TEST_COMPACTION,
        )

        # No surviving assistant message should carry usage
        for msg in session.messages:
            if msg.get("role") == "agent":
                assert "usage" not in msg, "Stale usage not stripped after compaction"
        # last_input_tokens should return 0 (no usage data)
        assert session.last_input_tokens == 0


# ─── Session Manager Lifecycle ──────────────────────────────────────


class TestSessionManagerLifecycle:
    """Session creation and persistence."""

    @pytest.mark.asyncio
    async def test_close_session_removes_from_active(self, pool):
        mgr = SessionManager(pool)
        await mgr.get_or_create("user1")
        assert await mgr.has_session("user1") is True

        result = await mgr.close_session("user1")
        assert result is True
        assert await mgr.has_session("user1") is False

    @pytest.mark.asyncio
    async def test_close_nonexistent_returns_false(self, pool):
        mgr = SessionManager(pool)
        result = await mgr.close_session("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_close_by_id(self, pool):
        mgr = SessionManager(pool)
        session = await mgr.get_or_create("user1")
        sid = session.id
        result = await mgr.close_session_by_id(sid)
        assert result is True
        assert await mgr.has_session("user1") is False

    @pytest.mark.asyncio
    async def test_close_by_unknown_id_returns_false(self, pool):
        mgr = SessionManager(pool)
        result = await mgr.close_session_by_id("nonexistent-uuid")
        assert result is False


# ─── Session Add Messages ──────────────────────────────────────────


class TestSessionAddMessages:
    """add_user_message and add_assistant_message."""

    @pytest.mark.asyncio
    async def test_add_user_message_persists(self, pool):
        session = await _create_session(pool, "test-add-user")
        await session.add_user_message("hello", sender="nico", source="telegram")
        assert len(session.messages) == 1
        assert session.messages[0]["role"] == "user"
        assert session.messages[0]["content"] == "hello"

        # Verify persisted to DB
        reloaded = Session("test-add-user", pool)
        await reloaded.load()
        assert len(reloaded.messages) == 1

    @pytest.mark.asyncio
    async def test_add_assistant_message_updates_tokens(self, pool):
        session = await _create_session(pool, "test-add-asst")
        msg = {
            "role": "agent", "text": "hi",
            "usage": {"input_tokens": 500, "output_tokens": 100},
        }
        await session.add_assistant_message(msg)
        assert session.total_input_tokens == 500
        assert session.total_output_tokens == 100
        assert len(session.messages) == 1


# ─── _validate_turn_structure ────────────────────────────────────────


class TestValidateTurnStructure:
    """Turn structure validation detects corruption without mutating."""

    def test_valid_structure_no_errors(self, caplog):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "agent", "tool_calls": [{"id": "tc1"}]},
            {"role": "tool_result", "results": []},
            {"role": "agent", "text": "done"},
        ]
        original = [dict(m) for m in messages]
        _validate_turn_structure(messages)
        assert messages == original
        assert "corruption" not in caplog.text

    def test_orphaned_tool_calls_detected_not_mutated(self, caplog):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "agent", "tool_calls": [{"id": "tc1"}]},
            {"role": "user", "content": "interruption"},
        ]
        original_len = len(messages)
        _validate_turn_structure(messages)
        assert len(messages) == original_len  # NOT mutated
        assert messages[1].get("tool_calls") is not None  # NOT stripped
        assert "orphaned tool_calls at index 1" in caplog.text

    def test_orphaned_tool_result_detected_not_removed(self, caplog):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "tool_result", "results": []},
            {"role": "agent", "text": "ok"},
        ]
        original_len = len(messages)
        _validate_turn_structure(messages)
        assert len(messages) == original_len  # NOT removed
        assert "orphaned tool_result at index 1" in caplog.text

    def test_tool_calls_at_end_detected(self, caplog):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "agent", "tool_calls": [{"id": "tc1"}]},
        ]
        _validate_turn_structure(messages)
        assert messages[1].get("tool_calls") is not None  # NOT stripped
        assert "orphaned tool_calls" in caplog.text


# ─── _context_tokens_from_usage ──────────────────────────────────────


class TestContextTokensFromUsage:
    """Tests for _context_tokens_from_usage helper."""

    def test_reads_context_tokens_field(self):
        usage = {"context_tokens": 500, "input_tokens": 300, "cache_read_tokens": 100}
        assert _context_tokens_from_usage(usage) == 500

    def test_missing_field_falls_through_with_warning(self, caplog):
        usage = {"input_tokens": 300, "cache_read_tokens": 100}
        result = _context_tokens_from_usage(usage)
        assert result == 400
        assert "missing context_tokens" in caplog.text


# ─── _text_from_content ─────────────────────────────────────────────


class TestTextFromContent:
    """Tests for _text_from_content helper."""

    def test_plain_string_passthrough(self):
        assert _text_from_content("hello world") == "hello world"

    def test_empty_string(self):
        assert _text_from_content("") == ""

    def test_none_returns_empty(self):
        assert _text_from_content(None) == ""

    def test_list_content_coerced_with_warning(self, caplog):
        """Non-string content is coerced but triggers a warning."""
        content = [
            {"type": "text", "text": "describe this"},
            {"type": "image", "media_type": "image/jpeg", "data": "base64data"},
        ]
        result = _text_from_content(content)
        assert result == "describe this"
        assert "Non-string content detected" in caplog.text

    def test_integer_coerced_with_warning(self, caplog):
        assert _text_from_content(42) == ""
        assert "Non-string content detected" in caplog.text


# ─── Content Blocks in Audit ────────────────────────────────────────


class TestContentBlocksInAudit:
    """Content blocks handled correctly in event audit trail."""

    @pytest.mark.asyncio
    async def test_add_tool_results_with_content_blocks(self, pool):
        """Tool result with list content truncates text, not the list."""
        session = await _create_session(pool, "test-blocks-audit")
        block_content = [
            {"type": "text", "text": "x" * 1000},
            {"type": "image", "media_type": "image/jpeg", "data": "abc"},
        ]
        await session.add_tool_results([{"tool_call_id": "tc1", "content": block_content}])

        rows = await pool.fetch(
            "SELECT payload FROM sessions.events WHERE session_id = $1 "
            "AND event_type = 'tool_result'",
            "test-blocks-audit",
        )
        event = json.loads(rows[0]["payload"])
        # Should be a truncated string, not a list
        assert isinstance(event["content"], str)
        assert len(event["content"]) == AUDIT_TRUNCATION_LIMIT

    @pytest.mark.asyncio
    async def test_persist_tool_results_with_content_blocks(self, pool):
        session = await _create_session(pool, "test-blocks-persist")
        block_content = [
            {"type": "text", "text": "result text"},
            {"type": "image", "media_type": "image/png", "data": "data"},
        ]
        await session.add_tool_results(
            [{"tool_call_id": "tc1", "content": block_content}], persist_only=True,
        )

        rows = await pool.fetch(
            "SELECT payload FROM sessions.events WHERE session_id = $1 "
            "AND event_type = 'tool_result'",
            "test-blocks-persist",
        )
        event = json.loads(rows[0]["payload"])
        assert isinstance(event["content"], str)
        assert "result text" in event["content"]


# ─── Compaction with Content Blocks ─────────────────────────────────


class TestCompactionWithContentBlocks:
    """Compaction handles vision messages without crashing."""

    @pytest.mark.asyncio
    async def test_compaction_extracts_text_from_content_blocks(self, pool):
        """Session with vision content blocks compacts without error."""
        session = await _create_session(pool, "test-compact-blocks")
        session.messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "what is in this photo"},
                {"type": "image", "media_type": "image/jpeg", "data": "base64data"},
            ]},
            {"role": "agent", "text": "I see a cat.",
             "usage": {"input_tokens": 500, "output_tokens": 50}},
            {"role": "user", "content": "thanks"},
            {"role": "agent", "text": "you're welcome",
             "usage": {"input_tokens": 600, "output_tokens": 30}},
            {"role": "user", "content": "another question"},
            {"role": "agent", "text": "another answer",
             "usage": {"input_tokens": 700, "output_tokens": 40}},
        ]
        mgr = SessionManager(pool)
        mock = MockCompactionProvider("Summary of vision conversation.")
        await mgr.compact_session(
            session, mock, "Summarize.",
            cost=_TEST_COST, **_TEST_COMPACTION,
        )

        assert mock.call_count == 1
        assert len(session.messages) == 4  # 1 summary + 1 compaction marker + 2 recent
        assert "Summary of vision conversation." in session.messages[0]["content"]


# ─── Compaction Anti-Hallucination ──────────────────────────────────


class TestCompactionAntiHallucination:
    """Compaction input must include structural boundaries against fabrication."""

    @pytest.mark.asyncio
    async def test_compaction_includes_end_marker_and_anti_fabrication(self, pool):
        """The conversation text sent to the provider must include an end-of-input
        marker and anti-fabrication instructions to prevent the model from
        generating fake dialogue beyond the real transcript."""
        session = await _create_session(pool, "test-anti-hallucination")
        for i in range(6):
            session.messages.append({"role": "user", "content": f"user msg {i}"})
            session.messages.append({
                "role": "agent", "text": f"reply {i}",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            })

        captured_messages = []
        captured_system = []
        provider = MockCompactionProvider(summary_text="Clean summary.")
        original_complete = provider.complete

        async def capturing_complete(system, messages, tools, **kwargs):
            captured_system.extend(system)
            captured_messages.extend(messages)
            return await original_complete(system, messages, tools, **kwargs)

        provider.complete = capturing_complete

        mgr = SessionManager(pool)
        await mgr.compact_session(
            session, provider, "Summarize.",
            cost=_TEST_COST, **_TEST_COMPACTION,
        )

        # Verify end-of-input marker in message content
        sent_text = captured_messages[0]["content"]
        assert "--- END OF CONVERSATION ---" in sent_text
        assert "Do not continue, extend, or invent" in sent_text

        # Verify anti-fabrication system prompt
        system_text = captured_system[0]["text"]
        assert "NEVER generate new dialogue" in system_text
        assert "NEVER" in system_text

    @pytest.mark.asyncio
    async def test_compaction_end_marker_after_all_conversation_content(self, pool):
        """End marker must appear AFTER all conversation content, not before."""
        session = await _create_session(pool, "test-marker-position")
        for i in range(6):
            session.messages.append({"role": "user", "content": f"msg {i}"})
            session.messages.append({
                "role": "agent", "text": f"reply {i}",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            })

        captured = []
        provider = MockCompactionProvider(summary_text="Summary.")
        original_complete = provider.complete

        async def capturing_complete(system, messages, tools, **kwargs):
            captured.extend(messages)
            return await original_complete(system, messages, tools, **kwargs)

        provider.complete = capturing_complete

        mgr = SessionManager(pool)
        await mgr.compact_session(
            session, provider, "Summarize.",
            cost=_TEST_COST, **_TEST_COMPACTION,
        )

        sent_text = captured[0]["content"]
        # Last real message should appear BEFORE the end marker
        last_msg_pos = sent_text.rfind("reply 3")  # last message in old 2/3
        end_marker_pos = sent_text.find("--- END OF CONVERSATION ---")
        assert last_msg_pos < end_marker_pos, (
            "End marker must come after all conversation content"
        )


# ─── Compaction State Persistence Order ─────────────────────────────


class TestCompactionStatePersistenceOrder:
    """save_state() must be called before append_event() in compaction."""

    @pytest.mark.asyncio
    async def test_save_state_before_append_event(self, pool):
        """State is persisted before the audit event, so a crash between
        the two doesn't lose the compaction."""
        from unittest.mock import patch

        session = await _create_session(pool, "test-persist-order")
        for i in range(15):
            session.messages.append({"role": "user", "content": f"msg {i}"})
            session.messages.append({
                "role": "agent", "text": f"reply {i}",
                "content": f"reply {i}",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            })
        await session.save_state()

        call_order = []
        orig_replace = session.replace_all_messages
        orig_append = session.append_event

        async def tracking_replace():
            call_order.append("replace_all_messages")
            return await orig_replace()

        async def tracking_append(event):
            call_order.append("append_event")
            return await orig_append(event)

        mgr = SessionManager(pool)
        provider = MockCompactionProvider(summary_text="Summary.")

        with patch.object(session, "replace_all_messages", tracking_replace), \
             patch.object(session, "append_event", tracking_append):
            await mgr.compact_session(
                session, provider, "Summarize.",
                cost=_TEST_COST, **_TEST_COMPACTION,
            )

        assert "replace_all_messages" in call_order
        assert "append_event" in call_order
        replace_idx = call_order.index("replace_all_messages")
        append_idx = call_order.index("append_event")
        assert replace_idx < append_idx, "replace_all_messages must be called before append_event"


# ─── build_session_info Tests ──────────────────────────────────────


class TestBuildSessionInfo:
    """Tests for the shared build_session_info() function."""

    @pytest.mark.asyncio
    async def test_with_live_session(self, pool):
        """Enriches from live session object."""
        session = await _create_session(pool, "sess-1", model="primary", contact="alice")
        session.messages = [
            {"role": "user", "content": "hello"},
            {"role": "agent", "text": "hi", "usage": {
                "input_tokens": 500, "output_tokens": 100,
                "cache_read_tokens": 200, "cache_write_tokens": 50,
            }},
        ]
        session.compaction_count = 2

        info = await build_session_info(
            pool,
session_id="sess-1",
            session=session,
            max_context_tokens=10000,
        )

        assert info["session_id"] == "sess-1"
        assert info["message_count"] == 2
        assert info["compaction_count"] == 2
        assert info["context_tokens"] == 500 + 200  # input + cache_read (context, not billing)
        assert info["context_pct"] == 700 * 100 // 10000
        assert info["model"] == "primary"

    @pytest.mark.asyncio
    async def test_from_db(self, pool):
        """Loads from DB when no live session."""
        session = await _create_session(pool, "sess-2", model="primary")
        session.messages = [
            {"role": "user", "content": "test"},
            {"role": "agent", "text": "ok", "usage": {"input_tokens": 300}},
        ]
        session.compaction_count = 1
        await session.save_state()

        info = await build_session_info(
            pool,
session_id="sess-2",
        )

        assert info["message_count"] == 2
        assert info["compaction_count"] == 1
        assert info["context_tokens"] == 300
        # Root fix for empty model column: from-DB path must also return model.
        assert info["model"] == "primary"

    @pytest.mark.asyncio
    async def test_no_session_returns_defaults(self, pool):
        """Returns defaults when session does not exist."""
        info = await build_session_info(
            pool,
session_id="nonexistent",
        )

        assert info["message_count"] == 0
        assert info["compaction_count"] == 0
        assert info["context_tokens"] == 0
        assert info["context_pct"] == 0
        assert info["cost"] == 0.0


# ─── read_history_events Tests ─────────────────────────────────────


class TestReadHistoryEvents:
    """Tests for read_history_events()."""

    @pytest.mark.asyncio
    async def test_reads_user_and_assistant(self, pool):
        """Default mode returns user + assistant messages."""
        session = await _create_session(pool, "s-1")
        await session.append_event({"type": "session", "id": "s-1"})
        await session.append_event({
            "type": "message", "role": "user", "content": "hello", "from": "alice",
        })
        await session.append_event({
            "type": "message", "role": "agent", "text": "hi there",
        })
        await session.append_event({
            "type": "tool_result", "tool_use_id": "t1", "content": "ok",
        })

        result = await read_history_events(pool, "s-1")

        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[0]["content"] == "hello"
        assert result[0]["from"] == "alice"
        assert result[1]["role"] == "agent"
        assert result[1]["text"] == "hi there"

    @pytest.mark.asyncio
    async def test_full_mode_includes_all(self, pool):
        """Full mode includes session, tool_result, etc."""
        session = await _create_session(pool, "s-full")
        await session.append_event({"type": "session", "id": "s-full"})
        await session.append_event({
            "type": "message", "role": "user", "content": "hello",
        })
        await session.append_event({
            "type": "tool_result", "tool_use_id": "t1", "content": "ok",
        })
        await session.append_event({
            "type": "message", "role": "agent", "text": "done",
        })

        result = await read_history_events(pool, "s-full", full=True)

        assert len(result) == 4
        assert result[0]["type"] == "session"
        assert result[2]["type"] == "tool_result"

    @pytest.mark.asyncio
    async def test_empty_session(self, pool):
        """No events returns empty list."""
        result = await read_history_events(pool, "nonexistent")
        assert result == []

    @pytest.mark.asyncio
    async def test_chronological_order(self, pool):
        """Events returned in insertion order."""
        session = await _create_session(pool, "s-chrono")
        await session.append_event({
            "type": "message", "role": "user", "content": "first",
        })
        await session.append_event({
            "type": "message", "role": "user", "content": "second",
        })

        result = await read_history_events(pool, "s-chrono")

        assert result[0]["content"] == "first"
        assert result[1]["content"] == "second"


# ─── Compaction Identity + Verification ─────────────────────────────


class TestCompactionIdentity:
    """system_blocks parameter passes agent identity to compaction model."""

    @pytest.mark.asyncio
    async def test_system_blocks_used_when_provided(self, pool):
        """When system_blocks is provided, they replace the default summarizer prompt."""
        session = await _create_session(pool, "test-identity")
        for i in range(6):
            session.messages.append({"role": "user", "content": f"msg {i}"})
            session.messages.append({
                "role": "agent", "text": f"reply {i}",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            })

        captured_system = []
        provider = MockCompactionProvider(summary_text="Identity-aware summary.")
        original_complete = provider.complete

        async def capturing_complete(system, messages, tools, **kwargs):
            captured_system.extend(system)
            return await original_complete(system, messages, tools, **kwargs)

        provider.complete = capturing_complete

        persona_blocks = [{"text": "I am Lucy, a goth AI familiar.", "tier": "stable"}]
        mgr = SessionManager(pool)
        await mgr.compact_session(
            session, provider, "Summarize.",
            system_blocks=persona_blocks,
            cost=_TEST_COST, **_TEST_COMPACTION,
        )

        assert len(captured_system) == 1
        assert "Lucy" in captured_system[0]["text"]
        assert "conversation summarizer" not in captured_system[0]["text"]

    @pytest.mark.asyncio
    async def test_fallback_without_system_blocks(self, pool):
        """When system_blocks is None, the default summarizer prompt is used."""
        session = await _create_session(pool, "test-fallback")
        for i in range(6):
            session.messages.append({"role": "user", "content": f"msg {i}"})
            session.messages.append({
                "role": "agent", "text": f"reply {i}",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            })

        captured_system = []
        provider = MockCompactionProvider(summary_text="Default summary.")
        original_complete = provider.complete

        async def capturing_complete(system, messages, tools, **kwargs):
            captured_system.extend(system)
            return await original_complete(system, messages, tools, **kwargs)

        provider.complete = capturing_complete

        mgr = SessionManager(pool)
        await mgr.compact_session(
            session, provider, "Summarize.",
            cost=_TEST_COST, **_TEST_COMPACTION,
        )

        assert "conversation summarizer" in captured_system[0]["text"]
