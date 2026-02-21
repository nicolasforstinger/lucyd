"""Tests for lucyd.py — _is_silent, _check_pid_file, _write_pid_file, _remove_pid_file."""

import os

import pytest

from lucyd import _check_pid_file, _is_silent, _remove_pid_file, _write_pid_file

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


# ─── PID File ────────────────────────────────────────────────────

class TestPIDFile:
    def test_write_creates_file_with_pid(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        _write_pid_file(pid_file)
        assert pid_file.exists()
        assert int(pid_file.read_text().strip()) == os.getpid()

    def test_write_creates_parent_dir(self, tmp_path):
        pid_file = tmp_path / "deep" / "nested" / "test.pid"
        _write_pid_file(pid_file)
        assert pid_file.exists()

    def test_remove_deletes_file(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("12345")
        _remove_pid_file(pid_file)
        assert not pid_file.exists()

    def test_remove_missing_no_error(self, tmp_path):
        pid_file = tmp_path / "nonexistent.pid"
        _remove_pid_file(pid_file)  # Should not raise

    def test_check_stale_pid_removes_file(self, tmp_path):
        """A PID file pointing to a dead process should be cleaned up."""
        pid_file = tmp_path / "test.pid"
        # Use a PID that (almost certainly) doesn't exist
        pid_file.write_text("999999999")
        _check_pid_file(pid_file)  # Should remove stale file
        assert not pid_file.exists()

    def test_check_pid_permission_error_exits(self, tmp_path):
        """BUG-1: PermissionError during os.kill exits with SystemExit."""
        from unittest.mock import patch
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("12345")
        with patch("os.kill", side_effect=PermissionError("Operation not permitted")):
            with pytest.raises(SystemExit):
                _check_pid_file(pid_file)
