"""Tests for log_utils — canonical _log_safe sanitizer + structured logging."""

import json
import logging

from log_utils import (
    StructuredJSONFormatter,
    _log_context,
    _log_safe,
    set_log_context,
)


def _clear():
    """Test helper — reset log context."""
    _log_context.set(None)


class TestLogSafe:
    """Verify _log_safe sanitizes control characters in user input."""

    # ── Newline handling ─────────────────────────────────────────

    def test_newline_stripped(self):
        assert "\n" not in _log_safe("attacker\nFAKE LOG ENTRY")

    def test_newline_escaped_as_literal(self):
        result = _log_safe("user\nINJECTED LOG LINE")
        assert "\n" not in result
        assert "\\n" in result

    def test_newline_in_filename(self):
        result = _log_safe("report.pdf\nFAKE ERROR: database corrupted")
        assert "\n" not in result
        assert "report.pdf" in result

    # ── Carriage return handling ─────────────────────────────────

    def test_carriage_return_stripped(self):
        assert "\r" not in _log_safe("attacker\rFAKE LOG ENTRY")

    def test_carriage_return_escaped_as_literal(self):
        result = _log_safe("user\rINJECTED")
        assert "\r" not in result
        assert "\\r" in result

    def test_carriage_return_in_filename(self):
        result = _log_safe("file\r\nINJECTED")
        assert "\r" not in result
        assert "\n" not in result

    # ── Combined CR+LF ──────────────────────────────────────────

    def test_crlf_both_replaced(self):
        result = _log_safe("line1\r\nline2\nline3")
        assert "\r" not in result
        assert "\n" not in result
        assert "line1" in result
        assert "line2" in result
        assert "line3" in result

    def test_crlf_both_escaped(self):
        result = _log_safe("sender\r\nFAKE")
        assert "\r" not in result
        assert "\n" not in result

    # ── Edge cases ───────────────────────────────────────────────

    def test_none_returns_empty(self):
        assert _log_safe(None) == ""

    def test_clean_string_unchanged(self):
        assert _log_safe("normal sender") == "normal sender"

    def test_non_string_converted(self):
        assert _log_safe(12345) == "12345"


class TestLogContext:
    """Verify structured log context via contextvars."""

    def test_set_and_get(self):
        set_log_context(agent_id="c1", session_id="s1", trace_id="t1")
        ctx = _log_context.get()
        assert ctx["agent_id"] == "c1"
        assert ctx["session_id"] == "s1"
        assert ctx["trace_id"] == "t1"
        _clear()

    def test_clear(self):
        set_log_context(agent_id="c1")
        _clear()
        assert _log_context.get() is None

    def test_empty_strings_omitted(self):
        set_log_context(agent_id="", session_id="s1", trace_id="")
        ctx = _log_context.get()
        assert "agent_id" not in ctx
        assert ctx["session_id"] == "s1"
        _clear()


class TestStructuredJSONFormatter:
    """Verify JSON formatter includes context fields."""

    def test_basic_output(self):
        _clear()
        fmt = StructuredJSONFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="hello %s", args=("world",), exc_info=None,
        )
        data = json.loads(fmt.format(record))
        assert data["level"] == "INFO"
        assert data["logger"] == "test"
        assert data["msg"] == "hello world"
        assert "ts" in data

    def test_includes_context(self):
        set_log_context(agent_id="cx", session_id="sx", trace_id="tx")
        fmt = StructuredJSONFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="test", args=(), exc_info=None,
        )
        data = json.loads(fmt.format(record))
        assert data["agent_id"] == "cx"
        assert data["session_id"] == "sx"
        assert data["trace_id"] == "tx"
        _clear()

    def test_no_context_no_extra_fields(self):
        _clear()
        fmt = StructuredJSONFormatter()
        record = logging.LogRecord(
            name="test", level=logging.DEBUG, pathname="", lineno=0,
            msg="test", args=(), exc_info=None,
        )
        data = json.loads(fmt.format(record))
        assert "agent_id" not in data
        assert "session_id" not in data

    def test_exception_included(self):
        import sys
        fmt = StructuredJSONFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            record = logging.LogRecord(
                name="test", level=logging.ERROR, pathname="", lineno=0,
                msg="boom", args=(), exc_info=sys.exc_info(),
            )
        data = json.loads(fmt.format(record))
        assert "exception" in data
        assert "ValueError" in data["exception"]
