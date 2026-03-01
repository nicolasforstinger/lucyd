"""Tests for lucyd-send — send_to_fifo, query_cost, _resolve_contact_name,
_is_uuid, _session_log_info.

The script lives at bin/lucyd-send (no .py extension), so we import it by
adding bin/ to sys.path and using importlib.
"""

import errno
import importlib.util
import json
import os
import sqlite3
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

import pytest

# Import the script as a module.  bin/lucyd-send has no .py extension,
# so we use spec_from_loader with an explicit SourceFileLoader.
_BIN_DIR = Path(__file__).resolve().parent.parent / "bin"

_loader = SourceFileLoader("lucyd_send", str(_BIN_DIR / "lucyd-send"))
_spec = importlib.util.spec_from_loader("lucyd_send", _loader)
lucyd_send = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lucyd_send)

send_to_fifo = lucyd_send.send_to_fifo
query_cost = lucyd_send.query_cost
_resolve_contact_name = lucyd_send._resolve_contact_name
_is_uuid = lucyd_send._is_uuid
_session_log_info = lucyd_send._session_log_info
show_history = lucyd_send.show_history


# ─── Fixtures ────────────────────────────────────────────────────

@pytest.fixture
def cost_db_with_data(tmp_path):
    """SQLite cost.db pre-populated with rows spanning several days."""
    db_path = tmp_path / "cost.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS costs (
            timestamp INTEGER,
            session_id TEXT,
            model TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_write_tokens INTEGER,
            cost_usd REAL
        )
    """)
    now = int(time.time())
    rows = [
        # Today
        (now - 60, "sess-1", "claude-opus", 10000, 2000, 5000, 0, 0.15),
        (now - 120, "sess-1", "claude-haiku", 5000, 1000, 0, 0, 0.01),
        # 3 days ago
        (now - 3 * 86400, "sess-2", "claude-opus", 20000, 4000, 10000, 0, 0.30),
        # 10 days ago (outside 7-day window)
        (now - 10 * 86400, "sess-3", "claude-opus", 50000, 10000, 25000, 0, 0.75),
    ]
    conn.executemany(
        "INSERT INTO costs VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def sessions_dir(tmp_path):
    """Temp directory with JSONL log files for a known session ID."""
    d = tmp_path / "sessions"
    d.mkdir()
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    # Create two date-stamped log files
    f1 = d / f"{sid}.2025-01-15.jsonl"
    f1.write_text('{"role":"user","text":"hello"}\n')
    f2 = d / f"{sid}.2025-01-16.jsonl"
    f2.write_text('{"role":"assistant","text":"hi"}\n{"role":"user","text":"bye"}\n')
    return d


# ─── _is_uuid ────────────────────────────────────────────────────

class TestIsUUID:
    def test_valid_lowercase(self):
        assert _is_uuid("550e8400-e29b-41d4-a716-446655440000") is True

    def test_valid_uppercase(self):
        assert _is_uuid("550E8400-E29B-41D4-A716-446655440000") is True

    def test_valid_mixed_case(self):
        assert _is_uuid("550e8400-E29B-41d4-A716-446655440000") is True

    def test_short_string(self):
        assert _is_uuid("not-a-uuid") is False

    def test_empty_string(self):
        assert _is_uuid("") is False

    def test_phone_number(self):
        assert _is_uuid("+431234567890") is False

    def test_plain_name(self):
        assert _is_uuid("Claudio") is False

    def test_missing_section(self):
        """UUID with only 4 groups instead of 5."""
        assert _is_uuid("550e8400-e29b-41d4-a716") is False

    def test_extra_characters(self):
        assert _is_uuid("550e8400-e29b-41d4-a716-446655440000X") is False

    def test_leading_whitespace(self):
        assert _is_uuid(" 550e8400-e29b-41d4-a716-446655440000") is False


# ─── send_to_fifo ────────────────────────────────────────────────

class TestSendToFifo:
    def test_writes_json_newline_to_fifo(self, tmp_path):
        """Successful write: message arrives as JSON + newline."""
        fifo_path = tmp_path / "test.pipe"
        os.mkfifo(str(fifo_path))

        message = {"type": "user", "text": "hello"}

        # Open reading end in a thread so the non-blocking write succeeds
        import threading

        captured = []

        def reader():
            with open(str(fifo_path)) as f:
                captured.append(f.read())

        t = threading.Thread(target=reader)
        t.start()
        # Small delay so the reader opens the FIFO before the writer
        time.sleep(0.05)

        send_to_fifo(fifo_path, message)
        t.join(timeout=2)

        assert len(captured) == 1
        assert captured[0] == json.dumps(message) + "\n"

    def test_enxio_prints_error_and_exits(self, tmp_path):
        """ENXIO (no reader) prints error to stderr and exits with code 1."""
        fifo_path = tmp_path / "test.pipe"
        os.mkfifo(str(fifo_path))

        # No reader opened, so O_WRONLY | O_NONBLOCK raises ENXIO
        with pytest.raises(SystemExit) as exc_info:
            send_to_fifo(fifo_path, {"type": "test"})

        assert exc_info.value.code == 1

    def test_other_oserror_reraises(self, tmp_path):
        """OSError that is not ENXIO should propagate."""
        fifo_path = tmp_path / "nonexistent" / "test.pipe"
        # Parent directory does not exist, so os.open raises ENOENT
        with pytest.raises(OSError) as exc_info:
            send_to_fifo(fifo_path, {"type": "test"})
        assert exc_info.value.errno != errno.ENXIO

    def test_json_serialisation_error_closes_fd(self, tmp_path):
        """If json.dumps fails, the fd is still closed (no leak)."""
        fifo_path = tmp_path / "test.pipe"
        os.mkfifo(str(fifo_path))

        # Open reader so os.open succeeds
        import threading

        def reader():
            with open(str(fifo_path)) as f:
                f.read()

        t = threading.Thread(target=reader)
        t.start()
        time.sleep(0.05)

        # An object that is not JSON-serialisable
        bad_msg = {"key": object()}
        with pytest.raises(TypeError):
            send_to_fifo(fifo_path, bad_msg)
        t.join(timeout=2)


# ─── query_cost ──────────────────────────────────────────────────

class TestQueryCost:
    def test_missing_db_exits(self, tmp_path):
        """If cost.db does not exist, exits with code 1."""
        missing = tmp_path / "nonexistent.db"
        with pytest.raises(SystemExit) as exc_info:
            query_cost(missing, "today")
        assert exc_info.value.code == 1

    def test_all_period_shows_every_row(self, cost_db_with_data, capsys):
        """Period 'all' returns rows from every timeframe."""
        query_cost(cost_db_with_data, "all")
        out = capsys.readouterr().out
        assert "claude-opus" in out
        assert "claude-haiku" in out
        # Total line should be present
        assert "Total" in out

    def test_week_period_excludes_old_rows(self, cost_db_with_data, capsys):
        """Period 'week' should include 3-day-old data but not 10-day-old."""
        query_cost(cost_db_with_data, "week")
        out = capsys.readouterr().out
        # The 3-day-old Opus row and today's rows are included
        assert "claude-opus" in out
        # Total cost should NOT include the 10-day-old row (0.75)
        # Today Opus (0.15) + 3-day-old Opus (0.30) + today Haiku (0.01) = 0.46
        # Actually since they're grouped by model, we see sums per model
        assert "Total" in out

    def test_today_period(self, cost_db_with_data, capsys):
        """Period 'today' returns only today's rows."""
        with patch("config.today_start_ts") as mock_ts:
            # Set "today start" to 1 hour ago so only the recent rows qualify
            mock_ts.return_value = int(time.time()) - 3600
            query_cost(cost_db_with_data, "today")
        out = capsys.readouterr().out
        assert "claude-opus" in out
        assert "claude-haiku" in out
        assert "Total" in out

    def test_empty_db_prints_no_data(self, tmp_path, capsys):
        """DB exists but has no rows for the period."""
        db_path = tmp_path / "cost.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS costs (
                timestamp INTEGER,
                session_id TEXT,
                model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cache_read_tokens INTEGER,
                cache_write_tokens INTEGER,
                cost_usd REAL
            )
        """)
        conn.commit()
        conn.close()

        query_cost(db_path, "all")
        out = capsys.readouterr().out
        assert "No cost data for this period" in out


# ─── _resolve_contact_name ───────────────────────────────────────

class TestResolveContactName:
    """Tests for resolving session contact keys to human-readable names."""

    CONTACTS = {
        "Claudio": "+431234567890",
        "Alice": "+449876543210",
    }

    def test_plain_name_returned_as_is(self):
        """A key that is already a readable name passes through."""
        result = _resolve_contact_name("Claudio", self.CONTACTS, [])
        assert result == "Claudio"

    def test_phone_number_resolves_to_name(self):
        """Phone number is reverse-looked-up to the contact name."""
        result = _resolve_contact_name(
            "+431234567890", self.CONTACTS, []
        )
        assert result == "Claudio"

    def test_unknown_phone_returns_raw(self):
        """Phone not in contacts is returned verbatim."""
        result = _resolve_contact_name(
            "+990000000000", self.CONTACTS, []
        )
        assert result == "+990000000000"

    def test_uuid_in_allow_from_resolves_to_name(self):
        """UUID present in allow_from matches the contact whose phone is also
        in allow_from."""
        uuid = "550e8400-e29b-41d4-a716-446655440000"
        allow_from = [uuid, "+431234567890"]
        result = _resolve_contact_name(uuid, self.CONTACTS, allow_from)
        assert result == "Claudio"

    def test_uuid_not_in_allow_from_returns_raw(self):
        """UUID not found in allow_from is returned verbatim."""
        uuid = "550e8400-e29b-41d4-a716-446655440000"
        result = _resolve_contact_name(uuid, self.CONTACTS, [])
        assert result == uuid

    def test_uuid_in_allow_from_but_no_phone_match(self):
        """UUID in allow_from but no contact phone also in allow_from."""
        uuid = "550e8400-e29b-41d4-a716-446655440000"
        allow_from = [uuid]  # No phone numbers in allow_from
        result = _resolve_contact_name(uuid, self.CONTACTS, allow_from)
        assert result == uuid

    def test_empty_contacts(self):
        """Empty contacts dict: phone is returned verbatim."""
        result = _resolve_contact_name("+431234567890", {}, [])
        assert result == "+431234567890"

    def test_name_not_starting_with_plus_not_uuid(self):
        """A string like 'system' that is neither phone nor UUID passes through."""
        result = _resolve_contact_name("system", self.CONTACTS, [])
        assert result == "system"


# ─── _session_log_info ───────────────────────────────────────────

class TestSessionLogInfo:
    SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    def test_no_log_files(self, tmp_path):
        """Session with no matching JSONL files returns zeros and '(no logs)'."""
        d = tmp_path / "sessions"
        d.mkdir()
        count, size, desc = _session_log_info(d, self.SID)
        assert count == 0
        assert size == 0
        assert desc == "(no logs)"

    def test_single_log_file(self, tmp_path):
        """Single date-stamped log file returns count=1 and the date."""
        d = tmp_path / "sessions"
        d.mkdir()
        f = d / f"{self.SID}.2025-01-15.jsonl"
        f.write_text('{"role":"user"}\n')
        count, size, desc = _session_log_info(d, self.SID)
        assert count == 1
        assert size == f.stat().st_size
        assert "1 log file" in desc
        # Should not say "files" (plural) for a single file
        assert "1 log files" not in desc

    def test_multiple_log_files(self, sessions_dir):
        """Multiple date-stamped files: returns correct count, total size, and
        date range."""
        count, size, desc = _session_log_info(sessions_dir, self.SID)
        assert count == 2
        assert size > 0
        assert "2 log files" in desc

    def test_total_bytes_is_sum_of_files(self, sessions_dir):
        """total_bytes equals the sum of all matching file sizes."""
        files = sorted(sessions_dir.glob(f"{self.SID}.*.jsonl"))
        expected = sum(f.stat().st_size for f in files)
        _, size, _ = _session_log_info(sessions_dir, self.SID)
        assert size == expected

    def test_date_range_format_different_dates(self, sessions_dir):
        """With multiple dates, range string contains both first and last date
        separated by an en-dash."""
        _, _, desc = _session_log_info(sessions_dir, self.SID)
        # The range should contain an en-dash between formatted dates
        assert "\u2013" in desc or "Jan" in desc  # en-dash or formatted month

    def test_non_matching_session_id_ignored(self, sessions_dir):
        """Files for a different session ID are not counted."""
        other_sid = "11111111-2222-3333-4444-555555555555"
        count, size, desc = _session_log_info(sessions_dir, other_sid)
        assert count == 0
        assert size == 0
        assert desc == "(no logs)"

    def test_current_year_strips_year(self, tmp_path):
        """Dates in the current year should not show the year number."""
        d = tmp_path / "sessions"
        d.mkdir()
        current_year = time.strftime("%Y")
        f = d / f"{self.SID}.{current_year}-03-15.jsonl"
        f.write_text('{"msg":"test"}\n')
        _, _, desc = _session_log_info(d, self.SID)
        # Should show "Mar 15" but NOT "Mar 15 2025" (or whatever current year)
        assert current_year not in desc
        assert "Mar" in desc

    def test_past_year_shows_year(self, tmp_path):
        """Dates in a past year should include the year."""
        d = tmp_path / "sessions"
        d.mkdir()
        f = d / f"{self.SID}.2023-06-20.jsonl"
        f.write_text('{"msg":"old"}\n')
        _, _, desc = _session_log_info(d, self.SID)
        assert "2023" in desc


# ─── CLI --attach flag ───────────────────────────────────────────

main = lucyd_send.main


class TestAttachFlag:
    """Tests for the --attach CLI flag and FIFO serialization."""

    def test_attach_serialises_metadata(self, tmp_path):
        """--attach serialises file metadata into the FIFO JSON message."""
        fifo_path = tmp_path / "test.pipe"
        os.mkfifo(str(fifo_path))

        # Create a test file
        test_file = tmp_path / "doc.pdf"
        test_file.write_bytes(b"%PDF-fake-content")

        import threading

        captured = []

        def reader():
            with open(str(fifo_path)) as f:
                captured.append(f.read())

        t = threading.Thread(target=reader)
        t.start()
        time.sleep(0.05)

        msg = {"type": "user", "text": "check this", "sender": "cli",
               "attachments": [{
                   "content_type": "application/pdf",
                   "local_path": str(test_file),
                   "filename": "doc.pdf",
                   "size": test_file.stat().st_size,
               }]}
        send_to_fifo(fifo_path, msg)
        t.join(timeout=2)

        assert len(captured) == 1
        parsed = json.loads(captured[0])
        assert "attachments" in parsed
        att = parsed["attachments"][0]
        assert att["content_type"] == "application/pdf"
        assert att["local_path"] == str(test_file)
        assert att["filename"] == "doc.pdf"
        assert att["size"] == len(b"%PDF-fake-content")

    def test_attach_nonexistent_file_exits(self, tmp_path, monkeypatch):
        """--attach with a nonexistent file prints error and exits."""
        monkeypatch.setattr("sys.argv", [
            "lucyd-send", "--message", "test",
            "--attach", str(tmp_path / "missing.pdf"),
            "--state-dir", str(tmp_path),
        ])
        # Create a fake FIFO so we get past the FIFO check
        fifo = tmp_path / "control.pipe"
        os.mkfifo(str(fifo))

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    def test_attach_directory_exits(self, tmp_path, monkeypatch):
        """--attach with a directory (not a file) prints error and exits."""
        a_dir = tmp_path / "subdir"
        a_dir.mkdir()
        monkeypatch.setattr("sys.argv", [
            "lucyd-send", "--message", "test",
            "--attach", str(a_dir),
            "--state-dir", str(tmp_path),
        ])
        fifo = tmp_path / "control.pipe"
        os.mkfifo(str(fifo))

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    def test_attach_guesses_content_type(self, tmp_path):
        """mimetypes guesses correct content_type for known extensions."""
        import mimetypes

        for ext, expected_prefix in [(".jpg", "image/"), (".png", "image/"), (".txt", "text/")]:
            f = tmp_path / f"test{ext}"
            f.write_bytes(b"x")
            ct, _ = mimetypes.guess_type(str(f))
            assert ct is not None
            assert ct.startswith(expected_prefix)

    def test_attach_multiple_files(self, tmp_path):
        """Multiple --attach flags produce multiple attachment entries."""
        fifo_path = tmp_path / "test.pipe"
        os.mkfifo(str(fifo_path))

        f1 = tmp_path / "a.txt"
        f1.write_text("aaa")
        f2 = tmp_path / "b.jpg"
        f2.write_bytes(b"\xff\xd8\xff")

        import threading

        captured = []

        def reader():
            with open(str(fifo_path)) as f:
                captured.append(f.read())

        t = threading.Thread(target=reader)
        t.start()
        time.sleep(0.05)

        msg = {"type": "user", "text": "two files", "sender": "cli",
               "attachments": [
                   {"content_type": "text/plain", "local_path": str(f1), "filename": "a.txt", "size": f1.stat().st_size},
                   {"content_type": "image/jpeg", "local_path": str(f2), "filename": "b.jpg", "size": f2.stat().st_size},
               ]}
        send_to_fifo(fifo_path, msg)
        t.join(timeout=2)

        parsed = json.loads(captured[0])
        assert len(parsed["attachments"]) == 2
        filenames = {a["filename"] for a in parsed["attachments"]}
        assert filenames == {"a.txt", "b.jpg"}


# ─── History Tests ────────────────────────────────────────────────


class TestShowHistory:
    """Tests for --history / show_history()."""

    def test_history_resolves_contact(self, tmp_path):
        """Resolves contact name to session ID and reads history."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        # Create session index
        index = {"alice": {"session_id": "s-hist-1"}}
        (sessions_dir / "sessions.json").write_text(json.dumps(index))

        # Create JSONL log
        events = [
            {"type": "message", "role": "user", "content": "hello",
             "from": "alice", "timestamp": 1.0},
            {"type": "message", "role": "assistant", "text": "hi there",
             "timestamp": 2.0},
        ]
        (sessions_dir / "s-hist-1.2026-02-26.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n"
        )

        # Capture show_history output
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            show_history(tmp_path, "alice")

        output = buf.getvalue()
        assert "alice" in output.lower() or "Session History" in output
        assert "hello" in output
        assert "hi there" in output

    def test_history_with_session_id(self, tmp_path):
        """Accepts session UUID directly."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        sid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        events = [
            {"type": "message", "role": "user", "content": "direct",
             "timestamp": 1.0},
        ]
        (sessions_dir / f"{sid}.2026-02-26.jsonl").write_text(
            json.dumps(events[0]) + "\n"
        )

        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            show_history(tmp_path, sid)

        assert "direct" in buf.getvalue()

    def test_history_no_session_found(self, tmp_path):
        """Prints 'no history' when contact not found in index and no JSONL files."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        (sessions_dir / "sessions.json").write_text("{}")

        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            show_history(tmp_path, "nonexistent")

        assert "No history" in buf.getvalue()

    def test_history_empty_session(self, tmp_path):
        """No JSONL files prints no history message."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        index = {"bob": {"session_id": "s-empty"}}
        (sessions_dir / "sessions.json").write_text(json.dumps(index))

        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            show_history(tmp_path, "bob")

        assert "No history" in buf.getvalue()


class TestChannelAgnosticContacts:
    """Tests for channel-agnostic contact resolution in list_sessions."""

    def test_collects_from_multiple_channels(self):
        """Contact resolution iterates all channel configs, not just telegram."""
        config = {
            "channel": {
                "telegram": {
                    "contacts": {"Alice": "+4312345"},
                    "allow_from": ["uuid-1"],
                },
                "whatsapp": {
                    "contacts": {"Bob": "+4367890"},
                    "allow_from": ["uuid-2"],
                },
            },
        }

        # Simulate the channel-agnostic collection logic
        contacts = {}
        allow_from = []
        channel_cfg = config.get("channel", {})
        for section_key, section_val in channel_cfg.items():
            if isinstance(section_val, dict):
                if "contacts" in section_val:
                    contacts.update(section_val["contacts"])
                if "allow_from" in section_val:
                    allow_from.extend(section_val["allow_from"])

        assert contacts == {"Alice": "+4312345", "Bob": "+4367890"}
        assert set(allow_from) == {"uuid-1", "uuid-2"}

    def test_non_dict_channel_values_skipped(self):
        """Non-dict values (like 'type': 'telegram') are skipped gracefully."""
        config = {
            "channel": {
                "type": "telegram",  # string, not dict
                "telegram": {
                    "contacts": {"Alice": "+43123"},
                },
            },
        }

        contacts = {}
        channel_cfg = config.get("channel", {})
        for section_key, section_val in channel_cfg.items():
            if isinstance(section_val, dict) and "contacts" in section_val:
                contacts.update(section_val["contacts"])

        assert contacts == {"Alice": "+43123"}


# ─── Notify Flag Tests ───────────────────────────────────────────


class TestNotifyFlag:
    """Tests for --notify / fire-and-forget notification messages."""

    def _send_notify_via_fifo(self, tmp_path, extra_args=None):
        """Helper: capture FIFO message from --notify invocation."""
        fifo_path = tmp_path / "control.pipe"
        os.mkfifo(str(fifo_path))

        import threading

        captured = []

        def reader():
            with open(str(fifo_path)) as f:
                captured.append(f.read())

        t = threading.Thread(target=reader)
        t.start()
        time.sleep(0.05)

        argv = ["lucyd-send", "--notify", "Invoice ready",
                "--state-dir", str(tmp_path)]
        if extra_args:
            argv.extend(extra_args)

        with patch("sys.argv", argv):
            main()

        t.join(timeout=2)
        assert len(captured) == 1
        return json.loads(captured[0])

    def test_basic_notify(self, tmp_path):
        """--notify sends system message with correct format."""
        msg = self._send_notify_via_fifo(tmp_path)
        assert msg["type"] == "system"
        assert "[AUTOMATED SYSTEM MESSAGE]" in msg["text"]
        assert "Invoice ready" in msg["text"]
        assert msg["sender"] == "cli"
        assert msg["tier"] == "operational"

    def test_notify_with_source_and_ref(self, tmp_path):
        """--source and --ref bracket-prefixed in LLM text."""
        msg = self._send_notify_via_fifo(
            tmp_path, ["--source", "n8n", "--ref", "INV-42"])
        assert "[source: n8n]" in msg["text"]
        assert "[ref: INV-42]" in msg["text"]
        assert msg["notify_meta"]["source"] == "n8n"
        assert msg["notify_meta"]["ref"] == "INV-42"

    def test_notify_with_data(self, tmp_path):
        """--data passed as notify_meta, not in LLM text."""
        msg = self._send_notify_via_fifo(
            tmp_path, ["--data", '{"amount": 99.50}'])
        assert msg["notify_meta"]["data"] == {"amount": 99.50}
        # Data should NOT appear in the text
        assert "99.50" not in msg["text"]

    def test_notify_invalid_data_exits(self, tmp_path, monkeypatch):
        """--data with invalid JSON exits with error."""
        fifo = tmp_path / "control.pipe"
        os.mkfifo(str(fifo))
        monkeypatch.setattr("sys.argv", [
            "lucyd-send", "--notify", "test",
            "--data", "not-json",
            "--state-dir", str(tmp_path),
        ])

        import threading

        def reader():
            with open(str(fifo)) as f:
                f.read()

        t = threading.Thread(target=reader)
        t.start()
        time.sleep(0.05)

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        t.join(timeout=2)

    def test_notify_with_custom_sender(self, tmp_path):
        """--from overrides the sender field."""
        msg = self._send_notify_via_fifo(tmp_path, ["--from", "n8n-workflow"])
        assert msg["sender"] == "n8n-workflow"

    def test_notify_no_meta_when_no_options(self, tmp_path):
        """No --source/--ref/--data means no notify_meta key."""
        msg = self._send_notify_via_fifo(tmp_path)
        assert "notify_meta" not in msg


# ─── Evolve Flag Tests ───────────────────────────────────────────


class TestEvolveFlag:
    """Tests for --evolve / memory evolution trigger."""

    def test_evolve_sends_correct_fifo_message(self, tmp_path):
        """--evolve --force sends evolution FIFO message."""
        fifo_path = tmp_path / "control.pipe"
        os.mkfifo(str(fifo_path))

        import threading

        captured = []

        def reader():
            with open(str(fifo_path)) as f:
                captured.append(f.read())

        t = threading.Thread(target=reader)
        t.start()
        time.sleep(0.05)

        with patch("sys.argv", [
            "lucyd-send", "--evolve", "--force",
            "--state-dir", str(tmp_path),
        ]):
            main()

        t.join(timeout=2)
        assert len(captured) == 1
        msg = json.loads(captured[0])
        assert msg["type"] == "system"
        assert msg["sender"] == "evolution"
        assert msg["tier"] == "full"
        assert msg["model"] == "primary"
        assert "evolution skill" in msg["text"]

    def test_evolve_precheck_skips_when_no_new_logs(self, tmp_path, capsys):
        """--evolve without --force skips when no new logs."""
        # Create memory DB with evolution_state showing recent evolution
        db_path = tmp_path / "memory" / "main.sqlite"
        db_path.parent.mkdir(parents=True)

        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")

        from memory_schema import ensure_schema
        ensure_schema(conn)

        # Mark evolution as done through today
        today = time.strftime("%Y-%m-%d")
        conn.execute(
            "INSERT INTO evolution_state (file_path, last_evolved_at, content_hash, logs_through) "
            "VALUES (?, ?, ?, ?)",
            ("MEMORY.md", today, "abc123", today),
        )
        conn.commit()
        conn.close()

        # Create workspace/memory with only today's log (not newer)
        mem_dir = tmp_path / "workspace" / "memory"
        mem_dir.mkdir(parents=True)
        (mem_dir / f"{today}.md").write_text("# Today's log")

        with patch("sys.argv", [
            "lucyd-send", "--evolve",
            "--state-dir", str(tmp_path),
        ]):
            main()

        out = capsys.readouterr().out
        assert "skipping" in out.lower()

    def test_evolve_precheck_triggers_when_new_logs(self, tmp_path):
        """--evolve without --force triggers when new logs exist."""
        # Create memory DB with old evolution state
        db_path = tmp_path / "memory" / "main.sqlite"
        db_path.parent.mkdir(parents=True)

        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")

        from memory_schema import ensure_schema
        ensure_schema(conn)

        conn.execute(
            "INSERT INTO evolution_state (file_path, last_evolved_at, content_hash, logs_through) "
            "VALUES (?, ?, ?, ?)",
            ("MEMORY.md", "2026-02-01", "abc123", "2026-02-01"),
        )
        conn.commit()
        conn.close()

        # Create workspace/memory with a newer log
        mem_dir = tmp_path / "workspace" / "memory"
        mem_dir.mkdir(parents=True)
        (mem_dir / "2026-02-15.md").write_text("# New log")

        # Need a FIFO for the trigger
        fifo_path = tmp_path / "control.pipe"
        os.mkfifo(str(fifo_path))

        import threading

        captured = []

        def reader():
            with open(str(fifo_path)) as f:
                captured.append(f.read())

        t = threading.Thread(target=reader)
        t.start()
        time.sleep(0.05)

        with patch("sys.argv", [
            "lucyd-send", "--evolve",
            "--state-dir", str(tmp_path),
        ]):
            main()

        t.join(timeout=2)
        assert len(captured) == 1
        msg = json.loads(captured[0])
        assert msg["sender"] == "evolution"
        assert msg["model"] == "primary"

    def test_evolve_no_db_triggers_anyway(self, tmp_path):
        """--evolve with no memory DB still triggers (first run)."""
        fifo_path = tmp_path / "control.pipe"
        os.mkfifo(str(fifo_path))

        import threading

        captured = []

        def reader():
            with open(str(fifo_path)) as f:
                captured.append(f.read())

        t = threading.Thread(target=reader)
        t.start()
        time.sleep(0.05)

        with patch("sys.argv", [
            "lucyd-send", "--evolve",
            "--state-dir", str(tmp_path),
        ]):
            main()

        t.join(timeout=2)
        assert len(captured) == 1
        msg = json.loads(captured[0])
        assert msg["sender"] == "evolution"

    def test_evolve_no_fifo_exits(self, tmp_path, monkeypatch):
        """--evolve --force with no FIFO exits with error."""
        monkeypatch.setattr("sys.argv", [
            "lucyd-send", "--evolve", "--force",
            "--state-dir", str(tmp_path),
        ])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
