"""Tests for SessionManager.build_recall — archived session recall.

Covers: happy path, count filtering, contact matching, most recent archive
selection, empty/missing archives, message type filtering, JSONL fallback.
"""

import json
import os
import time

import pytest

from session import SessionManager


@pytest.fixture
def mgr(tmp_sessions):
    """SessionManager with agent name 'TestBot'."""
    return SessionManager(tmp_sessions, agent_name="TestBot")


def _create_and_archive(mgr, contact, messages):
    """Helper: create a session, add messages, archive it."""
    session = mgr.get_or_create(contact)
    for role, text in messages:
        if role == "user":
            session.add_user_message(text)
        elif role == "assistant":
            session.add_assistant_message({
                "role": "assistant",
                "content": text,
                "text": text,
            })
        elif role == "system":
            session.messages.append({"role": "system", "content": text})
            session._save_state()
        elif role == "tool_results":
            session.messages.append({
                "role": "tool_results",
                "results": [{"tool_call_id": "t1", "content": text}],
            })
            session._save_state()
    return session


class TestBuildRecallHappyPath:
    @pytest.mark.asyncio
    async def test_basic_recall(self, mgr):
        """Archived session with user/assistant pairs returns formatted recall."""
        messages = []
        for i in range(5):
            messages.append(("user", f"question {i}"))
            messages.append(("assistant", f"answer {i}"))

        _create_and_archive(mgr, "alice", messages)
        await mgr.close_session("alice")

        recall = mgr.build_recall("alice")
        assert recall.startswith("Session recall (last conversation):")
        # All 10 messages present
        for i in range(5):
            assert f"**alice:** question {i}" in recall
            assert f"**TestBot:** answer {i}" in recall

    @pytest.mark.asyncio
    async def test_user_formatted_with_contact_name(self, mgr):
        """User messages use **contact:** format."""
        _create_and_archive(mgr, "bob", [
            ("user", "hello"),
            ("assistant", "hi bob"),
        ])
        await mgr.close_session("bob")

        recall = mgr.build_recall("bob")
        assert "**bob:** hello" in recall

    @pytest.mark.asyncio
    async def test_assistant_formatted_with_agent_name(self, mgr):
        """Assistant messages use **agent_name:** format."""
        _create_and_archive(mgr, "carol", [
            ("user", "hi"),
            ("assistant", "hey carol"),
        ])
        await mgr.close_session("carol")

        recall = mgr.build_recall("carol")
        assert "**TestBot:** hey carol" in recall


class TestBuildRecallCountFiltering:
    @pytest.mark.asyncio
    async def test_count_limits_messages(self, mgr):
        """count=3 returns only the last 3 conversation messages."""
        messages = []
        for i in range(10):
            messages.append(("user", f"q{i}"))
            messages.append(("assistant", f"a{i}"))

        _create_and_archive(mgr, "dave", messages)
        await mgr.close_session("dave")

        recall = mgr.build_recall("dave", count=3)
        # Last 3 conversation messages: user q9, assistant a8, assistant a9
        # (depends on filtering — last 3 of the filtered user+assistant list)
        assert "q0" not in recall
        assert "a0" not in recall
        # The last messages should be present
        assert "q9" in recall
        assert "a9" in recall


class TestBuildRecallContactMatching:
    @pytest.mark.asyncio
    async def test_only_matching_contact(self, mgr):
        """build_recall('alice') does not include bob's session."""
        _create_and_archive(mgr, "alice", [
            ("user", "alice question"),
            ("assistant", "alice answer"),
        ])
        await mgr.close_session("alice")

        _create_and_archive(mgr, "bob", [
            ("user", "bob question"),
            ("assistant", "bob answer"),
        ])
        await mgr.close_session("bob")

        recall = mgr.build_recall("alice")
        assert "alice question" in recall
        assert "bob question" not in recall


class TestBuildRecallMostRecent:
    @pytest.mark.asyncio
    async def test_selects_most_recent_archive(self, mgr, tmp_sessions):
        """When multiple archives exist for same contact, use the most recent."""
        # First session
        _create_and_archive(mgr, "eve", [
            ("user", "old question"),
            ("assistant", "old answer"),
        ])
        await mgr.close_session("eve")

        # Ensure first archive has older mtime
        archive = tmp_sessions / ".archive"
        for f in archive.glob("*.state.json"):
            old_time = time.time() - 3600
            os.utime(f, (old_time, old_time))

        # Second session (same contact)
        _create_and_archive(mgr, "eve", [
            ("user", "new question"),
            ("assistant", "new answer"),
        ])
        await mgr.close_session("eve")

        recall = mgr.build_recall("eve")
        assert "new question" in recall
        assert "old question" not in recall


class TestBuildRecallEmpty:
    def test_no_archive_directory(self, mgr):
        """No .archive/ directory → empty string."""
        assert mgr.build_recall("nobody") == ""

    @pytest.mark.asyncio
    async def test_empty_archive_directory(self, mgr, tmp_sessions):
        """Empty .archive/ directory → empty string, no crash."""
        (tmp_sessions / ".archive").mkdir()
        assert mgr.build_recall("nobody") == ""

    def test_nonexistent_contact(self, mgr, tmp_sessions):
        """Archive exists but no session for this contact → empty string."""
        (tmp_sessions / ".archive").mkdir()
        # Write a state file for a different contact
        state = {
            "id": "other-session",
            "contact": "someone-else",
            "messages": [{"role": "user", "content": "hi"}],
        }
        state_file = tmp_sessions / ".archive" / "other-session.state.json"
        state_file.write_text(json.dumps(state))

        assert mgr.build_recall("ghost") == ""


class TestBuildRecallMessageTypeFiltering:
    @pytest.mark.asyncio
    async def test_excludes_system_and_tool_messages(self, mgr):
        """Only user and assistant messages appear in recall."""
        _create_and_archive(mgr, "frank", [
            ("user", "hello"),
            ("system", "system instruction"),
            ("assistant", "hi there"),
            ("tool_results", "tool output here"),
            ("user", "follow up"),
            ("assistant", "got it"),
        ])
        await mgr.close_session("frank")

        recall = mgr.build_recall("frank")
        assert "hello" in recall
        assert "hi there" in recall
        assert "follow up" in recall
        assert "got it" in recall
        assert "system instruction" not in recall
        assert "tool output" not in recall


class TestBuildRecallJSONLFallback:
    @pytest.mark.asyncio
    async def test_jsonl_fallback_when_state_lacks_contact(self, mgr, tmp_sessions):
        """When state.json has no contact field, fall back to JSONL session event."""
        # Create and archive normally
        session = mgr.get_or_create("grace")
        session.add_user_message("hello from grace")
        session.add_assistant_message({
            "role": "assistant", "content": "hi grace", "text": "hi grace",
        })
        session_id = session.id
        await mgr.close_session("grace")

        # Remove the contact field from the archived state file
        archive = tmp_sessions / ".archive"
        state_file = archive / f"{session_id}.state.json"
        state = json.loads(state_file.read_text())
        del state["contact"]
        state_file.write_text(json.dumps(state))

        recall = mgr.build_recall("grace")
        # Should still find via JSONL fallback
        assert "hello from grace" in recall
