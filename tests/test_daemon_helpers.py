"""Tests for lucyd.py — _is_silent, PID file locking, _release_pid_file."""

import os

import pytest

from lucyd import _acquire_pid_file, _release_pid_file
from pipeline import _is_silent


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
