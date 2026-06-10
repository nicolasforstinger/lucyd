"""Tests for lucyd.py — _is_silent, PID file locking, _release_pid_file."""

import json
import os
import time
from unittest.mock import AsyncMock

import pytest

from lucyd import _acquire_pid_file, _release_pid_file
from pipeline import (
    _brief_snippet,
    _history_tokens,
    _is_silent,
    _recent_user_context,
    _time_of_day_steer,
)


class TestHistoryTokens:
    """_history_tokens counts the full request body, not just user+agent text."""

    def test_counts_user_and_agent_text(self):
        msgs = [
            {"role": "user", "content": "hello there friend"},
            {"role": "agent", "text": "hi back to you"},
        ]
        assert _history_tokens(msgs) > 0

    def test_counts_tool_result_content(self):
        """A tool_result body must add to the total — the path the old math skipped."""
        without = [
            {"role": "user", "content": "fetch the page"},
            {"role": "agent", "text": "", "tool_calls": [
                {"id": "t1", "name": "web_fetch", "arguments": {"url": "x"}}]},
        ]
        with_result = [
            *without,
            {"role": "tool_result", "results": [
                {"tool_call_id": "t1", "tool_name": "web_fetch",
                 "content": "a very long page body " * 200}]},
        ]
        assert _history_tokens(with_result) > _history_tokens(without) + 100

    def test_counts_tool_call_arguments(self):
        """A write with a large content arg counts even when agent text is empty."""
        small = [{"role": "agent", "text": "", "tool_calls": [
            {"id": "t1", "name": "write", "arguments": {"path": "/a"}}]}]
        big = [{"role": "agent", "text": "", "tool_calls": [
            {"id": "t1", "name": "write",
             "arguments": {"path": "/a", "content": "x" * 5000}}]}]
        assert _history_tokens(big) > _history_tokens(small) + 100


def _user_row(content: str, ts: float):
    return {"role": "user", "content": json.dumps({"role": "user", "content": content}), "ts": ts}


def _agent_row(payload: dict, ts: float):
    return {"role": "agent", "content": json.dumps({"role": "agent", **payload}), "ts": ts}


# ─── _brief_snippet (semantic-text extraction for the brief) ─────


class TestBriefSnippet:
    def test_user_voice_message_strips_metadata_keeps_content(self):
        # Real shape: [timestamp]\n[voice message, saved: /path]: <spoken text>.
        # The do-not-disturb clause sits PAST char 240 of the raw JSON — it must
        # survive now that the budget applies to cleaned text (dev-2026-05-21-001).
        spoken = (
            "Hey, I haven't checked in for a while because I am really busy "
            "with rebuilding the apartment and the day just started for me and "
            "my feet are completely sore, but I still have a lot to do — "
            "it will be today the full day."
        )
        raw = (
            "[Thu, 21. May 2026 - 08:48 UTC]\n"
            "[voice message, saved: /tmp/lucyd-http/1779_file_1082.oga]: " + spoken
        )
        out = _brief_snippet("user", json.dumps({"role": "user", "content": raw}))
        assert out.startswith("Hey, I haven't checked in")
        assert "it will be today the full day." in out  # load-bearing clause kept
        assert "saved:" not in out  # file path stripped
        assert ".oga" not in out
        assert "08:48 UTC" not in out  # timestamp header stripped

    def test_agent_tool_only_turn_yields_marker_not_empty(self):
        out = _brief_snippet(
            "agent",
            json.dumps({"role": "agent", "tool_calls": [{"name": "tts"}, {"name": "memory_write"}]}),
        )
        assert out == "[tool call: tts, memory_write]"

    def test_agent_text_preferred_over_thinking(self):
        out = _brief_snippet(
            "agent",
            json.dumps({"role": "agent", "text": "sending now", "thinking": "internal note"}),
        )
        assert out == "sending now"

    def test_agent_thinking_fallback_when_no_text(self):
        out = _brief_snippet("agent", json.dumps({"role": "agent", "thinking": "weigh the timing"}))
        assert out == "(thinking) weigh the timing"

    def test_user_empty_after_strip_falls_back_to_attachment_marker(self):
        raw = "[Thu, 21. May 2026 - 08:48 UTC]\n[image, saved: /tmp/x.png]:"
        out = _brief_snippet("user", json.dumps({"role": "user", "content": raw}))
        assert out == "[attachment]"

    def test_snippet_budget_applied(self):
        long = "word " * 300
        out = _brief_snippet("user", json.dumps({"role": "user", "content": long}))
        assert len(out) <= 400


# ─── _time_of_day_steer (night-guard for fired turns) ────────────


class TestTimeOfDaySteer:
    def test_sleeping_hour_adds_dont_disturb_steer(self):
        import datetime as dt
        for hour in (22, 23, 0, 4, 7):
            steer = _time_of_day_steer(dt.datetime(2026, 5, 26, hour, 0))
            assert "sleeping window" in steer
            assert "NO_REPLY" in steer or "hold it" in steer

    def test_daytime_hour_has_no_dont_disturb_steer(self):
        import datetime as dt
        for hour in (8, 12, 14, 21):
            steer = _time_of_day_steer(dt.datetime(2026, 5, 26, hour, 0))
            assert "sleeping window" not in steer
            # still states the local time for awareness
            assert "local time" in steer


# ─── _recent_user_context (situational brief for fired turns) ────


class TestRecentUserContext:
    @pytest.mark.asyncio
    async def test_no_rows_returns_standalone_directive(self):
        pool = AsyncMock()
        pool.fetch = AsyncMock(return_value=[])
        out = await _recent_user_context(pool, "user:nicolas")
        assert "clean standalone" in out
        assert "No recent conversation" in out

    @pytest.mark.asyncio
    async def test_rows_yield_recency_tail_and_weave_directive(self):
        now = time.time()
        # pool.fetch returns newest-first; helper reverses to oldest→newest
        rows = [
            _agent_row({"text": "got it, sending now"}, now - 360),
            _user_row("how's the plan looking", now - 400),
        ]
        pool = AsyncMock()
        pool.fetch = AsyncMock(return_value=rows)
        out = await _recent_user_context(pool, "user:nicolas")
        # recency derived from the newest row (~6 min)
        assert "6 min ago" in out
        # tail rendered oldest→newest
        assert out.index("how's the plan looking") < out.index("got it, sending now")
        # weave directive present
        assert "Weave" in out and "context-blind" in out


# ─── _is_silent ──────────────────────────────────────────────────

class TestIsSilent:
    def test_starts_with_token(self):
        assert _is_silent("HEARTBEAT_OK", ["HEARTBEAT_OK"]) is True

    def test_ends_with_token(self):
        assert _is_silent("All good. HEARTBEAT_OK", ["HEARTBEAT_OK"]) is True

    def test_token_in_middle_not_silent(self):
        """Token in the middle of text should NOT match (only start/end)."""
        assert _is_silent("before HEARTBEAT_OK after", ["HEARTBEAT_OK"]) is False

    def test_trailing_punctuation_ok(self):
        assert _is_silent("HEARTBEAT_OK.", ["HEARTBEAT_OK"]) is True

    def test_empty_text_returns_false(self):
        assert _is_silent("", ["HEARTBEAT_OK"]) is False

    def test_empty_tokens_returns_false(self):
        assert _is_silent("anything", []) is False

    def test_no_match(self):
        assert _is_silent("Hello world", ["HEARTBEAT_OK", "NO_REPLY"]) is False

    def test_multiple_tokens(self):
        assert _is_silent("NO_REPLY", ["HEARTBEAT_OK", "NO_REPLY"]) is True

    def test_whitespace_prefix_stripped(self):
        assert _is_silent("  HEARTBEAT_OK", ["HEARTBEAT_OK"]) is True


# ─── PID File (flock-based) ──────────────────────────────────────

class TestPIDFile:
    def test_acquire_creates_file_with_pid(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        _acquire_pid_file(pid_file)
        assert pid_file.exists()
        assert int(pid_file.read_text().strip()) == os.getpid()
        _release_pid_file(pid_file)

    def test_acquire_creates_parent_dir(self, tmp_path):
        pid_file = tmp_path / "deep" / "nested" / "test.pid"
        _acquire_pid_file(pid_file)
        assert pid_file.exists()
        _release_pid_file(pid_file)

    def test_release_deletes_file(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        _acquire_pid_file(pid_file)
        _release_pid_file(pid_file)
        assert not pid_file.exists()

    def test_release_missing_no_error(self, tmp_path):
        pid_file = tmp_path / "nonexistent.pid"
        _release_pid_file(pid_file)  # Should not raise

    def test_second_acquire_exits_if_locked(self, tmp_path):
        """A locked PID file prevents a second acquire."""
        pid_file = tmp_path / "test.pid"
        _acquire_pid_file(pid_file)
        # Simulate second process trying to acquire the same lock
        with pytest.raises(SystemExit):
            _acquire_pid_file(pid_file)
        _release_pid_file(pid_file)

    def test_stale_pid_file_reacquired(self, tmp_path):
        """An unlocked PID file (stale) can be reacquired."""
        pid_file = tmp_path / "test.pid"
        # Create a PID file without holding the lock
        pid_file.write_text("999999999")
        _acquire_pid_file(pid_file)  # Should succeed — no lock held
        assert int(pid_file.read_text().strip()) == os.getpid()
        _release_pid_file(pid_file)
