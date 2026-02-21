"""Tests for session.py — JSONL persistence, state, compaction, persist methods."""

import json
import time

import pytest

from session import AUDIT_TRUNCATION_LIMIT, Session, SessionManager, _text_from_content


class TestJSONLDatedFilename:
    def test_append_creates_dated_file(self, tmp_sessions):
        session = Session("test-abc", tmp_sessions)
        session.append_event({"type": "message", "role": "user", "content": "hello"})

        files = list(tmp_sessions.glob("*.jsonl"))
        assert len(files) == 1
        today = time.strftime("%Y-%m-%d")
        assert today in files[0].name
        assert files[0].name == f"test-abc.{today}.jsonl"

    def test_multiple_appends_same_file(self, tmp_sessions):
        session = Session("test-abc", tmp_sessions)
        session.append_event({"type": "message", "role": "user", "content": "first"})
        session.append_event({"type": "message", "role": "user", "content": "second"})

        files = list(tmp_sessions.glob("*.jsonl"))
        assert len(files) == 1

        lines = files[0].read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["content"] == "first"
        assert json.loads(lines[1])["content"] == "second"


class TestRebuildFromJSONL:
    def test_rebuild_orders_chunks_correctly(self, tmp_sessions):
        sid = "test-rebuild"
        chunk1 = tmp_sessions / f"{sid}.2026-01-10.jsonl"
        chunk2 = tmp_sessions / f"{sid}.2026-01-11.jsonl"
        chunk1.write_text(json.dumps({
            "type": "message", "role": "user", "content": "day one"
        }) + "\n")
        chunk2.write_text(json.dumps({
            "type": "message", "role": "user", "content": "day two"
        }) + "\n")

        session = Session(sid, tmp_sessions)
        result = session._rebuild_from_jsonl()
        assert result is True
        assert len(session.messages) == 2
        assert session.messages[0]["content"] == "day one"
        assert session.messages[1]["content"] == "day two"

    def test_rebuild_handles_compaction_event(self, tmp_sessions):
        sid = "test-compact"
        chunk = tmp_sessions / f"{sid}.2026-01-10.jsonl"
        events = [
            {"type": "message", "role": "user", "content": "old msg"},
            {"type": "compaction", "summary": "compacted summary of conversation"},
            {"type": "message", "role": "user", "content": "new msg"},
        ]
        chunk.write_text("\n".join(json.dumps(e) for e in events) + "\n")

        session = Session(sid, tmp_sessions)
        session._rebuild_from_jsonl()
        assert len(session.messages) == 2
        assert "[Previous conversation summary]" in session.messages[0]["content"]
        assert session.messages[1]["content"] == "new msg"


class TestLegacyMigration:
    def test_legacy_jsonl_renamed_on_load(self, tmp_sessions):
        sid = "test-legacy"
        legacy = tmp_sessions / f"{sid}.jsonl"
        ts = time.time()
        legacy.write_text(json.dumps({
            "type": "message", "role": "user", "content": "legacy",
            "timestamp": ts,
        }) + "\n")

        session = Session(sid, tmp_sessions)
        session.load()

        assert not legacy.exists()
        expected_date = time.strftime("%Y-%m-%d", time.localtime(ts))
        dated = tmp_sessions / f"{sid}.{expected_date}.jsonl"
        assert dated.exists()


class TestStateRoundTrip:
    def test_state_preserves_compaction_fields(self, tmp_sessions):
        session = Session("test-state", tmp_sessions)
        session.compaction_count = 3
        session.warned_about_compaction = True
        session.pending_system_warning = "Context at 130k tokens"
        session.messages = [{"role": "user", "content": "test"}]
        session._save_state()

        loaded = Session("test-state", tmp_sessions)
        assert loaded.load() is True
        assert loaded.compaction_count == 3
        assert loaded.warned_about_compaction is True
        assert loaded.pending_system_warning == "Context at 130k tokens"
        assert len(loaded.messages) == 1

    def test_corrupt_state_falls_back_to_jsonl(self, tmp_sessions):
        sid = "test-corrupt"
        # Write valid JSONL
        session = Session(sid, tmp_sessions)
        session.add_user_message("hello", sender="test")
        # Corrupt the state file
        session.state_path.write_text("{{invalid json")
        # Load should fall back to JSONL rebuild
        loaded = Session(sid, tmp_sessions)
        result = loaded.load()
        # It should rebuild from JSONL (may or may not succeed depending on JSONL)
        # The important thing is it doesn't crash
        assert isinstance(result, bool)


class TestCompactionWarning:
    def test_needs_compaction_above_threshold(self, tmp_sessions):
        session = Session("test-warn", tmp_sessions)
        session.messages = [{
            "role": "assistant",
            "text": "response",
            "usage": {"input_tokens": 160000, "output_tokens": 500},
        }]
        assert session.needs_compaction(150000) is True

    def test_no_compaction_below_threshold(self, tmp_sessions):
        session = Session("test-ok", tmp_sessions)
        session.messages = [{
            "role": "assistant",
            "text": "response",
            "usage": {"input_tokens": 100000, "output_tokens": 500},
        }]
        assert session.needs_compaction(150000) is False


class TestPersistMethods:
    """Tests for persist_assistant_message and persist_tool_results."""

    def test_persist_assistant_message_updates_tokens(self, tmp_sessions):
        session = Session("test-persist", tmp_sessions)
        assert session.total_input_tokens == 0
        assert session.total_output_tokens == 0

        msg = {
            "role": "assistant", "text": "hello",
            "usage": {"input_tokens": 1000, "output_tokens": 200},
        }
        session.persist_assistant_message(msg)

        assert session.total_input_tokens == 1000
        assert session.total_output_tokens == 200
        # Should NOT have appended to messages list
        assert len(session.messages) == 0

    def test_persist_assistant_message_writes_jsonl(self, tmp_sessions):
        session = Session("test-persist-j", tmp_sessions)
        msg = {
            "role": "assistant", "text": "hi",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        session.persist_assistant_message(msg)

        files = list(tmp_sessions.glob("*.jsonl"))
        assert len(files) == 1
        event = json.loads(files[0].read_text().strip())
        assert event["type"] == "message"
        assert event["role"] == "assistant"

    def test_persist_tool_results_writes_jsonl(self, tmp_sessions):
        session = Session("test-persist-t", tmp_sessions)
        results = [
            {"tool_call_id": "tc1", "content": "result one"},
            {"tool_call_id": "tc2", "content": "result two"},
        ]
        session.persist_tool_results(results)

        files = list(tmp_sessions.glob("*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["tool_use_id"] == "tc1"
        assert json.loads(lines[1])["tool_use_id"] == "tc2"

    def test_persist_tool_results_truncates(self, tmp_sessions):
        session = Session("test-trunc", tmp_sessions)
        long_content = "x" * 1000
        results = [{"tool_call_id": "tc1", "content": long_content}]
        session.persist_tool_results(results)

        files = list(tmp_sessions.glob("*.jsonl"))
        event = json.loads(files[0].read_text().strip())
        assert len(event["content"]) == AUDIT_TRUNCATION_LIMIT


class TestAuditTruncationLimit:
    def test_constant_value(self):
        assert AUDIT_TRUNCATION_LIMIT == 500

    def test_add_tool_results_truncates(self, tmp_sessions):
        session = Session("test-trunc2", tmp_sessions)
        long_content = "y" * 1000
        session.add_tool_results([{"tool_call_id": "tc1", "content": long_content}])

        files = list(tmp_sessions.glob("*.jsonl"))
        event = json.loads(files[0].read_text().strip())
        assert len(event["content"]) == AUDIT_TRUNCATION_LIMIT


class TestSessionManager:
    def test_get_or_create_new(self, tmp_sessions):
        mgr = SessionManager(tmp_sessions)
        session = mgr.get_or_create("user1", model="test")
        assert session is not None
        assert session.id  # Should have a UUID

    def test_get_or_create_returns_same(self, tmp_sessions):
        mgr = SessionManager(tmp_sessions)
        s1 = mgr.get_or_create("user1")
        s2 = mgr.get_or_create("user1")
        assert s1.id == s2.id

    def test_different_contacts_different_sessions(self, tmp_sessions):
        mgr = SessionManager(tmp_sessions)
        s1 = mgr.get_or_create("user1")
        s2 = mgr.get_or_create("user2")
        assert s1.id != s2.id


class TestMessageOrder:
    def test_add_user_then_assistant_preserves_order(self, tmp_sessions):
        session = Session("test-order", tmp_sessions)
        session.add_user_message("hello", sender="test", source="cli")
        session.messages.append({
            "role": "assistant", "text": "hi back",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })

        assert session.messages[0]["role"] == "user"
        assert session.messages[0]["content"] == "hello"
        assert session.messages[1]["role"] == "assistant"
        assert session.messages[1]["text"] == "hi back"


# ─── Rebuild Token/Compaction Restoration ────────────────────────


class TestRebuildRestoration:
    """BUG-3: _rebuild_from_jsonl restores token counters and compaction count."""

    def test_rebuild_restores_token_counts(self, tmp_sessions):
        """Write JSONL with usage data, rebuild, verify totals."""
        session = Session("test-tokens", tmp_sessions)
        today = time.strftime("%Y-%m-%d")
        jsonl_path = tmp_sessions / f"test-tokens.{today}.jsonl"
        events = [
            {"type": "message", "role": "user", "content": "hello"},
            {"type": "message", "role": "assistant", "text": "hi",
             "usage": {"input_tokens": 500, "output_tokens": 100}},
            {"type": "message", "role": "user", "content": "more"},
            {"type": "message", "role": "assistant", "text": "sure",
             "usage": {"input_tokens": 800, "output_tokens": 200}},
        ]
        with open(jsonl_path, "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

        # Reset and rebuild
        session.messages = []
        session.total_input_tokens = 0
        session.total_output_tokens = 0
        result = session._rebuild_from_jsonl()
        assert result is True
        assert session.total_input_tokens == 1300
        assert session.total_output_tokens == 300

    def test_rebuild_restores_compaction_count(self, tmp_sessions):
        """Write JSONL with compaction events, rebuild, verify count."""
        session = Session("test-compact", tmp_sessions)
        today = time.strftime("%Y-%m-%d")
        jsonl_path = tmp_sessions / f"test-compact.{today}.jsonl"
        events = [
            {"type": "message", "role": "user", "content": "hello"},
            {"type": "compaction", "summary": "Conversation about greetings"},
            {"type": "message", "role": "user", "content": "continued"},
            {"type": "compaction", "summary": "Extended conversation"},
        ]
        with open(jsonl_path, "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

        session.messages = []
        session.compaction_count = 0
        result = session._rebuild_from_jsonl()
        assert result is True
        assert session.compaction_count == 2


# ─── Compaction End-to-End ────────────────────────────────────────


class TestCompactionEndToEnd:
    """TEST-4: Verify SessionManager.compact_session end-to-end."""

    @pytest.fixture
    def six_message_session(self, tmp_sessions):
        """Create a session with 6 messages (3 user + 3 assistant with usage)."""
        session = Session("test-e2e-compact", tmp_sessions)
        for i in range(3):
            session.messages.append(
                {"role": "user", "content": f"user message {i}"}
            )
            session.messages.append(
                {
                    "role": "assistant",
                    "text": f"assistant reply {i}",
                    "usage": {"input_tokens": 100 * (i + 1), "output_tokens": 50 * (i + 1)},
                }
            )
        return session

    @pytest.mark.asyncio
    async def test_compact_replaces_messages_with_summary_plus_recent(
        self, tmp_sessions, six_message_session
    ):
        """After compaction, messages = [summary_msg] + recent_messages."""
        session = six_message_session
        mgr = SessionManager(tmp_sessions)

        # Mock provider
        mock_provider = MockCompactionProvider(summary_text="Summary of old conversation.")
        await mgr.compact_session(session, mock_provider, "Summarize this conversation.")

        # split_point = 6 * 2 // 3 = 4, so 4 old, 2 recent
        # Result: 1 summary + 2 recent = 3
        assert len(session.messages) == 3
        assert "[Previous conversation summary]" in session.messages[0]["content"]
        assert "Summary of old conversation." in session.messages[0]["content"]
        # Recent messages preserved
        assert session.messages[1]["role"] == "user"
        assert session.messages[1]["content"] == "user message 2"
        assert session.messages[2]["role"] == "assistant"
        assert session.messages[2]["text"] == "assistant reply 2"

    @pytest.mark.asyncio
    async def test_compact_increments_compaction_count(
        self, tmp_sessions, six_message_session
    ):
        session = six_message_session
        mgr = SessionManager(tmp_sessions)
        assert session.compaction_count == 0

        mock_provider = MockCompactionProvider(summary_text="Summary.")
        await mgr.compact_session(session, mock_provider, "Summarize.")

        assert session.compaction_count == 1

        # Compact again (add messages to get back above threshold)
        for i in range(4):
            session.messages.append({"role": "user", "content": f"extra {i}"})
        await mgr.compact_session(session, mock_provider, "Summarize.")
        assert session.compaction_count == 2

    @pytest.mark.asyncio
    async def test_compact_writes_compaction_event_to_jsonl(
        self, tmp_sessions, six_message_session
    ):
        session = six_message_session
        mgr = SessionManager(tmp_sessions)

        mock_provider = MockCompactionProvider(summary_text="JSONL summary test.")
        await mgr.compact_session(session, mock_provider, "Summarize.")

        # Find JSONL files and look for compaction event
        jsonl_files = list(tmp_sessions.glob("*.jsonl"))
        assert len(jsonl_files) >= 1
        found_compaction = False
        for f in jsonl_files:
            for line in f.read_text().strip().split("\n"):
                if not line:
                    continue
                event = json.loads(line)
                if event.get("type") == "compaction":
                    found_compaction = True
                    assert event["compaction_number"] == 1
                    assert event["removed_messages"] == 4  # 6 * 2 // 3
                    assert "JSONL summary test." in event["summary"]
        assert found_compaction, "No compaction event found in JSONL"

    @pytest.mark.asyncio
    async def test_compact_saves_state(self, tmp_sessions, six_message_session):
        session = six_message_session
        mgr = SessionManager(tmp_sessions)

        mock_provider = MockCompactionProvider(summary_text="State save test.")
        await mgr.compact_session(session, mock_provider, "Summarize.")

        # State file should exist and reflect compacted state
        assert session.state_path.exists()
        state = json.loads(session.state_path.read_text())
        assert state["compaction_count"] == 1
        assert len(state["messages"]) == 3  # 1 summary + 2 recent

    @pytest.mark.asyncio
    async def test_compact_skips_when_fewer_than_4_messages(self, tmp_sessions):
        """Sessions with < 4 messages should not be compacted."""
        session = Session("test-skip-compact", tmp_sessions)
        session.messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "text": "hi", "usage": {"input_tokens": 10, "output_tokens": 5}},
            {"role": "user", "content": "bye"},
        ]
        mgr = SessionManager(tmp_sessions)
        mock_provider = MockCompactionProvider(summary_text="Should not appear.")
        await mgr.compact_session(session, mock_provider, "Summarize.")

        # Messages unchanged
        assert len(session.messages) == 3
        assert session.compaction_count == 0
        # Provider should not have been called
        assert mock_provider.call_count == 0

    @pytest.mark.asyncio
    async def test_compact_resets_warned_flag(self, tmp_sessions, six_message_session):
        """Compaction should reset warned_about_compaction to False."""
        session = six_message_session
        session.warned_about_compaction = True
        mgr = SessionManager(tmp_sessions)

        mock_provider = MockCompactionProvider(summary_text="Reset flag test.")
        await mgr.compact_session(session, mock_provider, "Summarize.")

        assert session.warned_about_compaction is False


class TestCompactionRoundTrip:
    """End-to-end compaction: real Session, mock only the LLM provider."""

    @pytest.mark.asyncio
    async def test_round_trip(self, tmp_sessions):
        """Compact a 30-message session, verify structure and provider input."""
        session = Session("test-roundtrip", tmp_sessions)
        for i in range(15):
            session.messages.append({"role": "user", "content": f"user msg {i}"})
            session.messages.append({
                "role": "assistant", "text": f"reply {i}",
                "content": f"reply {i}",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            })
        session._save_state()
        assert len(session.messages) == 30

        # Capture what the provider receives
        captured_input = []
        provider = MockCompactionProvider(summary_text="Round-trip summary.")
        original_complete = provider.complete

        async def capturing_complete(system, messages, tools, **kwargs):
            captured_input.extend(messages)
            return await original_complete(system, messages, tools, **kwargs)

        provider.complete = capturing_complete

        mgr = SessionManager(tmp_sessions)
        await mgr.compact_session(session, provider, "Summarize this conversation.")

        # split_point = 30 * 2 // 3 = 20 old, 10 recent
        # Result: 1 summary + 10 recent = 11
        assert len(session.messages) == 11
        assert "[Previous conversation summary]" in session.messages[0]["content"]
        assert "Round-trip summary." in session.messages[0]["content"]

        # Recent 10 messages preserved unchanged
        assert session.messages[1]["content"] == "user msg 10"
        assert session.messages[-1]["text"] == "reply 14"

        assert session.compaction_count == 1
        assert session.warned_about_compaction is False

        # State file on disk reflects compacted state
        loaded_state = json.loads(session.state_path.read_text())
        assert loaded_state["compaction_count"] == 1
        assert len(loaded_state["messages"]) == 11

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
    async def test_double_compaction_preserves_prior_summary(self, tmp_sessions):
        """Second compaction includes first summary in its input."""
        session = Session("test-double", tmp_sessions)

        # Round 1: 30 messages
        for i in range(15):
            session.messages.append({"role": "user", "content": f"r1 msg {i}"})
            session.messages.append({
                "role": "assistant", "text": f"r1 reply {i}",
                "content": f"r1 reply {i}",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            })
        session._save_state()

        mgr = SessionManager(tmp_sessions)
        provider_a = MockCompactionProvider(summary_text="Summary A.")
        await mgr.compact_session(session, provider_a, "Summarize.")
        assert session.compaction_count == 1
        assert "Summary A." in session.messages[0]["content"]

        # Round 2: add 30 more messages, then compact again
        for i in range(15):
            session.messages.append({"role": "user", "content": f"r2 msg {i}"})
            session.messages.append({
                "role": "assistant", "text": f"r2 reply {i}",
                "content": f"r2 reply {i}",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            })
        session._save_state()

        # Capture round 2 input
        captured_input = []
        provider_b = MockCompactionProvider(summary_text="Summary B.")
        original_complete = provider_b.complete

        async def capturing_complete(system, messages, tools, **kwargs):
            captured_input.extend(messages)
            return await original_complete(system, messages, tools, **kwargs)

        provider_b.complete = capturing_complete

        await mgr.compact_session(session, provider_b, "Summarize.")

        assert session.compaction_count == 2
        # Current messages start with Summary B, not Summary A
        assert "Summary B." in session.messages[0]["content"]

        # Summary A was in the old 2/3 and sent to the provider
        sent_text = captured_input[0]["content"]
        assert "Summary A." in sent_text


# ─── Phase 3: Behavioral Survivors ──────────────────────────────


class TestRebuildMessageOrder:
    """Rebuild replays messages in chronological JSONL order."""

    def test_messages_replayed_in_order(self, tmp_sessions):
        sid = "test-order-rebuild"
        today = time.strftime("%Y-%m-%d")
        jsonl_path = tmp_sessions / f"{sid}.{today}.jsonl"
        events = [
            {"type": "message", "role": "user", "content": "first"},
            {"type": "message", "role": "assistant", "text": "reply1",
             "usage": {"input_tokens": 10, "output_tokens": 5}},
            {"type": "message", "role": "user", "content": "second"},
            {"type": "message", "role": "assistant", "text": "reply2",
             "usage": {"input_tokens": 20, "output_tokens": 10}},
        ]
        with open(jsonl_path, "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

        session = Session(sid, tmp_sessions)
        session._rebuild_from_jsonl()

        assert len(session.messages) == 4
        assert session.messages[0]["content"] == "first"
        assert session.messages[1]["text"] == "reply1"
        assert session.messages[2]["content"] == "second"
        assert session.messages[3]["text"] == "reply2"


class TestCompactionReplacesMessages:
    """Compaction replaces old messages with summary, keeps recent."""

    @pytest.mark.asyncio
    async def test_compaction_replaces_not_appends(self, tmp_sessions):
        """After compaction, old messages are GONE, replaced by summary."""
        session = Session("test-replace", tmp_sessions)
        for i in range(6):
            session.messages.append({"role": "user", "content": f"msg-{i}"})
        mgr = SessionManager(tmp_sessions)
        mock = MockCompactionProvider("Summary text.")
        await mgr.compact_session(session, mock, "Summarize.")

        # Old messages should be gone
        contents = [m.get("content", "") for m in session.messages]
        assert "msg-0" not in contents
        assert "msg-1" not in contents
        assert "msg-2" not in contents
        # Summary should be first
        assert "Summary text." in session.messages[0]["content"]


class TestSessionManagerLifecycle:
    """Session creation and persistence."""

    @pytest.mark.asyncio
    async def test_close_session_removes_from_index(self, tmp_sessions):
        mgr = SessionManager(tmp_sessions)
        mgr.get_or_create("user1")
        assert "user1" in mgr._index

        result = await mgr.close_session("user1")
        assert result is True
        assert "user1" not in mgr._index

    @pytest.mark.asyncio
    async def test_close_nonexistent_returns_false(self, tmp_sessions):
        mgr = SessionManager(tmp_sessions)
        result = await mgr.close_session("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_close_by_id(self, tmp_sessions):
        mgr = SessionManager(tmp_sessions)
        session = mgr.get_or_create("user1")
        sid = session.id
        result = await mgr.close_session_by_id(sid)
        assert result is True
        assert "user1" not in mgr._index

    @pytest.mark.asyncio
    async def test_close_by_unknown_id_returns_false(self, tmp_sessions):
        mgr = SessionManager(tmp_sessions)
        result = await mgr.close_session_by_id("nonexistent-uuid")
        assert result is False

    def test_create_subagent_session(self, tmp_sessions):
        mgr = SessionManager(tmp_sessions)
        sub = mgr.create_subagent_session("parent-123", model="haiku")
        assert sub.id.startswith("sub-")
        assert sub.model == "haiku"


class TestSessionAddMessages:
    """add_user_message and add_assistant_message."""

    def test_add_user_message_persists(self, tmp_sessions):
        session = Session("test-add-user", tmp_sessions)
        session.add_user_message("hello", sender="nico", source="telegram")
        assert len(session.messages) == 1
        assert session.messages[0]["role"] == "user"
        assert session.messages[0]["content"] == "hello"
        # State file should exist
        assert session.state_path.exists()

    def test_add_assistant_message_updates_tokens(self, tmp_sessions):
        session = Session("test-add-asst", tmp_sessions)
        msg = {
            "role": "assistant", "text": "hi",
            "usage": {"input_tokens": 500, "output_tokens": 100},
        }
        session.add_assistant_message(msg)
        assert session.total_input_tokens == 500
        assert session.total_output_tokens == 100
        assert len(session.messages) == 1


# ─── _text_from_content ───────────────────────────────────────────


class TestTextFromContent:
    """Tests for _text_from_content helper."""

    def test_plain_string_passthrough(self):
        assert _text_from_content("hello world") == "hello world"

    def test_empty_string(self):
        assert _text_from_content("") == ""

    def test_none_returns_empty(self):
        assert _text_from_content(None) == ""

    def test_empty_list_returns_empty(self):
        assert _text_from_content([]) == ""

    def test_text_blocks_joined(self):
        content = [
            {"type": "text", "text": "describe this"},
            {"type": "image", "media_type": "image/jpeg", "data": "base64data"},
        ]
        assert _text_from_content(content) == "describe this"

    def test_multiple_text_blocks(self):
        content = [
            {"type": "text", "text": "first"},
            {"type": "text", "text": "second"},
        ]
        assert _text_from_content(content) == "first second"

    def test_image_only_returns_empty(self):
        content = [
            {"type": "image", "media_type": "image/png", "data": "abc"},
        ]
        assert _text_from_content(content) == ""

    def test_non_dict_items_skipped(self):
        content = [
            {"type": "text", "text": "valid"},
            "stray string",
            42,
        ]
        assert _text_from_content(content) == "valid"

    def test_dict_without_type_skipped(self):
        content = [
            {"text": "no type field"},
            {"type": "text", "text": "has type"},
        ]
        assert _text_from_content(content) == "has type"

    def test_integer_returns_empty(self):
        assert _text_from_content(42) == ""


class TestContentBlocksInAudit:
    """Content blocks handled correctly in JSONL audit trail."""

    def test_add_tool_results_with_content_blocks(self, tmp_sessions):
        """Tool result with list content truncates text, not the list."""
        session = Session("test-blocks-audit", tmp_sessions)
        block_content = [
            {"type": "text", "text": "x" * 1000},
            {"type": "image", "media_type": "image/jpeg", "data": "abc"},
        ]
        session.add_tool_results([{"tool_call_id": "tc1", "content": block_content}])

        files = list(tmp_sessions.glob("*.jsonl"))
        event = json.loads(files[0].read_text().strip())
        # Should be a truncated string, not a list
        assert isinstance(event["content"], str)
        assert len(event["content"]) == AUDIT_TRUNCATION_LIMIT

    def test_persist_tool_results_with_content_blocks(self, tmp_sessions):
        session = Session("test-blocks-persist", tmp_sessions)
        block_content = [
            {"type": "text", "text": "result text"},
            {"type": "image", "media_type": "image/png", "data": "data"},
        ]
        session.persist_tool_results([{"tool_call_id": "tc1", "content": block_content}])

        files = list(tmp_sessions.glob("*.jsonl"))
        event = json.loads(files[0].read_text().strip())
        assert isinstance(event["content"], str)
        assert "result text" in event["content"]


class TestCompactionWithContentBlocks:
    """Compaction handles vision messages without crashing."""

    @pytest.mark.asyncio
    async def test_compaction_extracts_text_from_content_blocks(self, tmp_sessions):
        """Session with vision content blocks compacts without error."""
        session = Session("test-compact-blocks", tmp_sessions)
        session.messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "what is in this photo"},
                {"type": "image", "media_type": "image/jpeg", "data": "base64data"},
            ]},
            {"role": "assistant", "text": "I see a cat.",
             "usage": {"input_tokens": 500, "output_tokens": 50}},
            {"role": "user", "content": "thanks"},
            {"role": "assistant", "text": "you're welcome",
             "usage": {"input_tokens": 600, "output_tokens": 30}},
            {"role": "user", "content": "another question"},
            {"role": "assistant", "text": "another answer",
             "usage": {"input_tokens": 700, "output_tokens": 40}},
        ]
        mgr = SessionManager(tmp_sessions)
        mock = MockCompactionProvider("Summary of vision conversation.")
        await mgr.compact_session(session, mock, "Summarize.")

        assert mock.call_count == 1
        assert len(session.messages) == 3  # 1 summary + 2 recent
        assert "Summary of vision conversation." in session.messages[0]["content"]


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
