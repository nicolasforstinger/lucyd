"""Tests for channels/email.py — IMAP/SMTP email bridge."""

from __future__ import annotations

import asyncio
import base64
import imaplib
import smtplib
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

import channels.email as email_mod


# ─── Helpers ──────────────────────────────────────────────────────

def _build_plain_email(
    from_addr: str = "sender@example.com",
    subject: str = "Test Subject",
    body: str = "Hello world",
) -> bytes:
    """Build a simple text/plain email and return raw bytes."""
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = from_addr
    msg["To"] = "me@example.com"
    msg["Subject"] = subject
    return msg.as_bytes()


def _build_multipart_email(
    from_addr: str = "sender@example.com",
    subject: str = "Multipart Subject",
    text_body: str = "Plain text body",
    html_body: str = "<p>HTML body</p>",
) -> bytes:
    """Build a multipart/alternative email and return raw bytes."""
    msg = MIMEMultipart("alternative")
    msg["From"] = from_addr
    msg["To"] = "me@example.com"
    msg["Subject"] = subject
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    return msg.as_bytes()


def _build_email_with_attachment(
    from_addr: str = "sender@example.com",
    subject: str = "With Attachment",
    text_body: str = "See attached",
    att_filename: str = "test.pdf",
    att_content_type: str = "application/pdf",
    att_data: bytes = b"%PDF-fake-content",
) -> bytes:
    """Build a multipart/mixed email with a text body and one attachment."""
    msg = MIMEMultipart("mixed")
    msg["From"] = from_addr
    msg["To"] = "me@example.com"
    msg["Subject"] = subject
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    maintype, _, subtype = att_content_type.partition("/")
    part = MIMEBase(maintype, subtype or "octet-stream")
    part.set_payload(att_data)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=att_filename)
    msg.attach(part)
    return msg.as_bytes()


def _mock_imap(
    search_uids: bytes = b"1 2",
    messages: dict[bytes, bytes] | None = None,
) -> MagicMock:
    """Return a mock IMAP4_SSL with configurable search/fetch results."""
    imap = MagicMock(spec=imaplib.IMAP4_SSL)
    imap.search.return_value = ("OK", [search_uids])
    if messages is None:
        raw = _build_plain_email()
        messages = {b"1": raw, b"2": raw}

    def _fetch(uid: Any, _spec: str) -> tuple[str, list[Any]]:
        raw = messages.get(uid if isinstance(uid, bytes) else uid.encode())
        if raw is None:
            return ("OK", [(None,)])
        return ("OK", [(b"1", raw)])

    imap.fetch.side_effect = _fetch
    imap.store.return_value = ("OK", [])
    imap.close.return_value = None
    imap.logout.return_value = None
    return imap


# ─── Fixture: save/restore module globals ────────────────────────

_GLOBALS = (
    "URL", "IMAP_HOST", "SMTP_HOST", "USER", "PASSWORD",
    "FOLDER", "POLL_INTERVAL", "FROM_ADDR",
    "IMAP_PORT", "SMTP_PORT", "SECURITY", "ALLOWED_SENDERS",
)


@pytest.fixture(autouse=True)
def _restore_globals() -> Any:
    """Save and restore email module globals around every test."""
    saved = {name: getattr(email_mod, name) for name in _GLOBALS}
    yield
    for name, value in saved.items():
        setattr(email_mod, name, value)


# ─── 1. Config loading ───────────────────────────────────────────


class TestLoadConfig:
    def test_load_config_reads_email_section(self, tmp_path: Any, monkeypatch: Any) -> None:
        """load_config() reads [email] section from lucyd.toml via LUCYD_CONFIG."""
        toml = tmp_path / "lucyd.toml"
        toml.write_text(
            '[email]\n'
            'imap_host = "imap.test.com"\n'
            'smtp_host = "smtp.test.com"\n'
            'folder = "Archive"\n'
            'poll_interval = 30\n'
            'from_address = "bot@test.com"\n'
            'user_env = "MY_USER"\n'
            'password_env = "MY_PASS"\n'
        )
        monkeypatch.setenv("LUCYD_CONFIG", str(toml))
        monkeypatch.setenv("MY_USER", "alice")
        monkeypatch.setenv("MY_PASS", "secret")

        email_mod.load_config()

        assert email_mod.IMAP_HOST == "imap.test.com"
        assert email_mod.SMTP_HOST == "smtp.test.com"
        assert email_mod.FOLDER == "Archive"
        assert email_mod.POLL_INTERVAL == 30
        assert email_mod.FROM_ADDR == "bot@test.com"
        assert email_mod.USER == "alice"
        assert email_mod.PASSWORD == "secret"

    def test_load_config_from_address_overrides(
        self, tmp_path: Any, monkeypatch: Any,
    ) -> None:
        """from_address in TOML overrides the USER-based FROM_ADDR."""
        toml = tmp_path / "lucyd.toml"
        toml.write_text(
            '[email]\n'
            'imap_host = "imap.test.com"\n'
            'smtp_host = "smtp.test.com"\n'
            'from_address = "noreply@example.com"\n'
        )
        monkeypatch.setenv("LUCYD_CONFIG", str(toml))
        email_mod.load_config()
        assert email_mod.FROM_ADDR == "noreply@example.com"

    def test_load_config_poll_interval_read(
        self, tmp_path: Any, monkeypatch: Any,
    ) -> None:
        """poll_interval in TOML is read correctly."""
        toml = tmp_path / "lucyd.toml"
        toml.write_text(
            '[email]\n'
            'imap_host = "x"\n'
            'smtp_host = "x"\n'
            'poll_interval = 120\n'
        )
        monkeypatch.setenv("LUCYD_CONFIG", str(toml))
        email_mod.load_config()
        assert email_mod.POLL_INTERVAL == 120

    def test_load_config_starttls_and_ports(
        self, tmp_path: Any, monkeypatch: Any,
    ) -> None:
        """load_config() reads imap_port, smtp_port, and security from TOML."""
        toml = tmp_path / "lucyd.toml"
        toml.write_text(
            '[email]\n'
            'imap_host = "imap.test.com"\n'
            'smtp_host = "smtp.test.com"\n'
            'imap_port = 1143\n'
            'smtp_port = 1025\n'
            'security = "starttls"\n'
        )
        monkeypatch.setenv("LUCYD_CONFIG", str(toml))
        email_mod.load_config()
        assert email_mod.IMAP_PORT == 1143
        assert email_mod.SMTP_PORT == 1025
        assert email_mod.SECURITY == "starttls"

    def test_load_config_allowed_senders_lowercased(
        self, tmp_path: Any, monkeypatch: Any,
    ) -> None:
        """load_config() reads allowed_senders from TOML and lowercases them."""
        toml = tmp_path / "lucyd.toml"
        toml.write_text(
            '[email]\n'
            'imap_host = "imap.test.com"\n'
            'smtp_host = "smtp.test.com"\n'
            'allowed_senders = ["Alice@Example.COM", "bob@test.com"]\n'
        )
        monkeypatch.setenv("LUCYD_CONFIG", str(toml))
        email_mod.load_config()
        assert email_mod.ALLOWED_SENDERS == ["alice@example.com", "bob@test.com"]

    def test_exits_when_lucyd_config_not_set(self, monkeypatch: Any) -> None:
        """load_config() exits when LUCYD_CONFIG is not set."""
        monkeypatch.delenv("LUCYD_CONFIG", raising=False)
        with pytest.raises(SystemExit):
            email_mod.load_config()

    def test_exits_when_config_file_missing(self, monkeypatch: Any) -> None:
        """load_config() exits when LUCYD_CONFIG points to a non-existent file."""
        monkeypatch.setenv("LUCYD_CONFIG", "/nonexistent/lucyd.toml")
        with pytest.raises(SystemExit):
            email_mod.load_config()

    def test_exits_when_no_email_section(self, tmp_path: Any, monkeypatch: Any) -> None:
        """load_config() exits when lucyd.toml has no [email] section."""
        toml = tmp_path / "lucyd.toml"
        toml.write_text('[agent]\nname = "Test"\n')
        monkeypatch.setenv("LUCYD_CONFIG", str(toml))
        with pytest.raises(SystemExit):
            email_mod.load_config()


# ─── 2. IMAP fetch and mark ──────────────────────────────────────


class TestFetchAndMark:
    def test_fetches_unread_emails(self) -> None:
        """fetch_and_mark() returns a list of dicts with uid/from/subject/body."""
        imap = _mock_imap(
            search_uids=b"10",
            messages={b"10": _build_plain_email(body="Hi there")},
        )
        with patch.object(email_mod, "_imap_connect", return_value=imap):
            result = email_mod.fetch_and_mark([])

        assert len(result) == 1
        assert result[0]["uid"] == b"10"
        assert result[0]["from"] == "sender@example.com"
        assert result[0]["subject"] == "Test Subject"
        assert result[0]["body"] == "Hi there"

    def test_marks_previously_processed_uids_as_read(self) -> None:
        """Previously processed UIDs are flagged \\Seen."""
        imap = _mock_imap(search_uids=b"")
        with patch.object(email_mod, "_imap_connect", return_value=imap):
            email_mod.fetch_and_mark([b"5", b"6"])

        imap.store.assert_any_call("5", "+FLAGS", "\\Seen")
        imap.store.assert_any_call("6", "+FLAGS", "\\Seen")

    def test_multipart_email_extracts_text_plain_body(self) -> None:
        """For multipart emails, fetch_and_mark extracts text/plain."""
        raw = _build_multipart_email(text_body="Plain content")
        imap = _mock_imap(search_uids=b"7", messages={b"7": raw})
        with patch.object(email_mod, "_imap_connect", return_value=imap):
            result = email_mod.fetch_and_mark([])

        assert result[0]["body"] == "Plain content"

    def test_html_only_multipart_returns_empty_body(self) -> None:
        """Multipart email with no text/plain part returns empty body."""
        msg = MIMEMultipart("alternative")
        msg["From"] = "sender@example.com"
        msg["To"] = "me@example.com"
        msg["Subject"] = "HTML Only"
        msg.attach(MIMEText("<p>Only HTML</p>", "html", "utf-8"))
        raw = msg.as_bytes()
        imap = _mock_imap(search_uids=b"20", messages={b"20": raw})
        with patch.object(email_mod, "_imap_connect", return_value=imap):
            result = email_mod.fetch_and_mark([])
        assert result[0]["body"] == ""

    def test_multipart_mixed_finds_text_plain_among_attachments(self) -> None:
        """multipart/mixed with text/plain + binary attachment extracts text."""
        from email.mime.base import MIMEBase
        from email import encoders
        msg = MIMEMultipart("mixed")
        msg["From"] = "sender@example.com"
        msg["To"] = "me@example.com"
        msg["Subject"] = "With Attachment"
        msg.attach(MIMEText("Body text here", "plain", "utf-8"))
        att = MIMEBase("application", "octet-stream")
        att.set_payload(b"\x00\x01\x02")
        encoders.encode_base64(att)
        att.add_header("Content-Disposition", "attachment", filename="data.bin")
        msg.attach(att)
        raw = msg.as_bytes()
        imap = _mock_imap(search_uids=b"21", messages={b"21": raw})
        with patch.object(email_mod, "_imap_connect", return_value=imap):
            result = email_mod.fetch_and_mark([])
        assert result[0]["body"] == "Body text here"

    def test_extracts_attachments_from_multipart_email(self) -> None:
        """fetch_and_mark() extracts MIME attachments as base64-encoded dicts."""
        raw = _build_email_with_attachment(
            text_body="Check this",
            att_filename="report.pdf",
            att_data=b"fake-pdf-bytes",
        )
        imap = _mock_imap(search_uids=b"30", messages={b"30": raw})
        with patch.object(email_mod, "_imap_connect", return_value=imap):
            result = email_mod.fetch_and_mark([])

        assert len(result) == 1
        assert result[0]["body"] == "Check this"
        atts = result[0]["attachments"]
        assert len(atts) == 1
        assert atts[0]["filename"] == "report.pdf"
        assert atts[0]["content_type"] == "application/pdf"
        assert base64.b64decode(atts[0]["data"]) == b"fake-pdf-bytes"

    def test_plain_email_has_empty_attachments_list(self) -> None:
        """Non-multipart emails have an empty attachments list."""
        raw = _build_plain_email(body="No attachments here")
        imap = _mock_imap(search_uids=b"31", messages={b"31": raw})
        with patch.object(email_mod, "_imap_connect", return_value=imap):
            result = email_mod.fetch_and_mark([])

        assert result[0]["attachments"] == []

    def test_text_body_parts_not_extracted_as_attachments(self) -> None:
        """text/plain and text/html body parts are not treated as attachments."""
        raw = _build_multipart_email(
            text_body="Plain text",
            html_body="<p>HTML</p>",
        )
        imap = _mock_imap(search_uids=b"32", messages={b"32": raw})
        with patch.object(email_mod, "_imap_connect", return_value=imap):
            result = email_mod.fetch_and_mark([])

        assert result[0]["attachments"] == []

    def test_non_utf8_body_decoded_with_replacement(self) -> None:
        """Non-UTF8 bytes in body are decoded with errors='replace'."""
        # Build raw email with Latin-1 encoded body but claim UTF-8
        raw_body = "Caf\xe9 na\xefve".encode("latin-1")
        # Hand-craft a minimal RFC822 message with raw bytes
        raw = (
            b"From: sender@example.com\r\n"
            b"To: me@example.com\r\n"
            b"Subject: Encoding Test\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Content-Transfer-Encoding: 8bit\r\n"
            b"\r\n"
        ) + raw_body
        imap = _mock_imap(search_uids=b"22", messages={b"22": raw})
        with patch.object(email_mod, "_imap_connect", return_value=imap):
            result = email_mod.fetch_and_mark([])
        # Should not crash; replacement chars appear for invalid UTF-8 bytes
        assert len(result) == 1
        assert "\ufffd" in result[0]["body"] or "Caf" in result[0]["body"]

    def test_non_multipart_email_extracts_body(self) -> None:
        """Non-multipart emails have their body extracted directly."""
        raw = _build_plain_email(body="Simple body")
        imap = _mock_imap(search_uids=b"8", messages={b"8": raw})
        with patch.object(email_mod, "_imap_connect", return_value=imap):
            result = email_mod.fetch_and_mark([])

        assert result[0]["body"] == "Simple body"

    def test_empty_body_is_returned(self) -> None:
        """An email with empty body is returned (poll_loop decides to skip)."""
        raw = _build_plain_email(body="")
        imap = _mock_imap(search_uids=b"9", messages={b"9": raw})
        with patch.object(email_mod, "_imap_connect", return_value=imap):
            result = email_mod.fetch_and_mark([])

        assert len(result) == 1
        assert result[0]["body"] == ""

    def test_imap_error_returns_empty_list(self) -> None:
        """IMAP connection error returns an empty list."""
        with patch.object(
            email_mod, "_imap_connect",
            side_effect=imaplib.IMAP4.error("connection refused"),
        ):
            result = email_mod.fetch_and_mark([])

        assert result == []

    def test_empty_fetch_result_is_skipped(self) -> None:
        """Emails with empty msg_data from IMAP fetch are skipped."""
        imap = MagicMock(spec=imaplib.IMAP4_SSL)
        imap.search.return_value = ("OK", [b"1"])
        imap.fetch.return_value = ("OK", [None])  # no data for this UID
        imap.store.return_value = ("OK", [])
        with patch.object(email_mod, "_imap_connect", return_value=imap):
            result = email_mod.fetch_and_mark([])
        assert result == []

    def test_imap_connect_starttls_mode(self) -> None:
        """_imap_connect() uses IMAP4 + starttls() when SECURITY is 'starttls'."""
        email_mod.IMAP_HOST = "127.0.0.1"
        email_mod.IMAP_PORT = 1143
        email_mod.SECURITY = "starttls"
        email_mod.USER = "user"
        email_mod.PASSWORD = "pass"
        email_mod.FOLDER = "INBOX"

        mock_imap = MagicMock(spec=imaplib.IMAP4)
        with patch("channels.email.imaplib.IMAP4", return_value=mock_imap) as mock_cls:
            result = email_mod._imap_connect()

        mock_cls.assert_called_once_with("127.0.0.1", 1143)
        mock_imap.starttls.assert_called_once()
        mock_imap.login.assert_called_once_with("user", "pass")
        mock_imap.select.assert_called_once_with("INBOX")
        assert result is mock_imap

    def test_imap_connect_ssl_mode(self) -> None:
        """_imap_connect() uses IMAP4_SSL when SECURITY is 'ssl' (default)."""
        email_mod.IMAP_HOST = "imap.example.com"
        email_mod.IMAP_PORT = 0
        email_mod.SECURITY = "ssl"
        email_mod.USER = "user"
        email_mod.PASSWORD = "pass"
        email_mod.FOLDER = "INBOX"

        mock_imap = MagicMock(spec=imaplib.IMAP4_SSL)
        with patch("channels.email.imaplib.IMAP4_SSL", return_value=mock_imap) as mock_cls:
            result = email_mod._imap_connect()

        mock_cls.assert_called_once_with("imap.example.com")
        mock_imap.login.assert_called_once_with("user", "pass")
        assert result is mock_imap

    def test_failed_mark_as_read_logs_warning_but_continues(self) -> None:
        """If marking a UID as read fails, processing continues."""
        raw = _build_plain_email(body="After mark error")
        imap = _mock_imap(search_uids=b"11", messages={b"11": raw})
        imap.store.side_effect = imaplib.IMAP4.error("store failed")
        with patch.object(email_mod, "_imap_connect", return_value=imap):
            result = email_mod.fetch_and_mark([b"99"])

        # Despite the store error, new messages are still fetched
        assert len(result) == 1
        assert result[0]["body"] == "After mark error"


# ─── 3. SMTP send ────────────────────────────────────────────────


class TestSendReply:
    def test_sends_email_via_smtp_ssl(self) -> None:
        """send_reply() sends via SMTP_SSL with correct headers."""
        email_mod.FROM_ADDR = "bot@example.com"
        email_mod.USER = "bot@example.com"
        email_mod.PASSWORD = "pass"
        email_mod.SMTP_HOST = "smtp.example.com"

        mock_smtp = MagicMock(spec=smtplib.SMTP_SSL)
        mock_smtp.__enter__ = MagicMock(return_value=mock_smtp)
        mock_smtp.__exit__ = MagicMock(return_value=False)

        with patch("channels.email.smtplib.SMTP_SSL", return_value=mock_smtp):
            email_mod.send_reply("user@example.com", "Re: Hello", "Reply body")

        mock_smtp.login.assert_called_once_with("bot@example.com", "pass")
        mock_smtp.send_message.assert_called_once()
        sent_msg = mock_smtp.send_message.call_args[0][0]
        assert sent_msg["From"] == "bot@example.com"
        assert sent_msg["To"] == "user@example.com"
        assert sent_msg["Subject"] == "Re: Hello"

    def test_sends_email_via_smtp_starttls(self) -> None:
        """send_reply() uses SMTP + starttls() when SECURITY is 'starttls'."""
        email_mod.FROM_ADDR = "bot@example.com"
        email_mod.USER = "bot@example.com"
        email_mod.PASSWORD = "pass"
        email_mod.SMTP_HOST = "127.0.0.1"
        email_mod.SMTP_PORT = 1025
        email_mod.SECURITY = "starttls"

        mock_smtp = MagicMock(spec=smtplib.SMTP)
        mock_smtp.__enter__ = MagicMock(return_value=mock_smtp)
        mock_smtp.__exit__ = MagicMock(return_value=False)

        with patch("channels.email.smtplib.SMTP", return_value=mock_smtp) as mock_cls:
            email_mod.send_reply("user@example.com", "Re: Hello", "Reply body")

        mock_cls.assert_called_once_with("127.0.0.1", 1025)
        mock_smtp.starttls.assert_called_once()
        mock_smtp.login.assert_called_once_with("bot@example.com", "pass")
        mock_smtp.send_message.assert_called_once()

    def test_sends_reply_with_attachments_as_multipart(self) -> None:
        """send_reply() builds multipart/mixed when attachments are provided."""
        email_mod.FROM_ADDR = "bot@example.com"
        email_mod.USER = "bot@example.com"
        email_mod.PASSWORD = "pass"
        email_mod.SMTP_HOST = "smtp.example.com"
        email_mod.SECURITY = "ssl"

        mock_smtp = MagicMock(spec=smtplib.SMTP_SSL)
        mock_smtp.__enter__ = MagicMock(return_value=mock_smtp)
        mock_smtp.__exit__ = MagicMock(return_value=False)

        attachments = [{
            "content_type": "application/pdf",
            "data": base64.b64encode(b"pdf-bytes").decode(),
            "filename": "report.pdf",
        }]

        with patch("channels.email.smtplib.SMTP_SSL", return_value=mock_smtp):
            email_mod.send_reply("user@example.com", "Re: Report", "Here you go", attachments)

        mock_smtp.send_message.assert_called_once()
        sent_msg = mock_smtp.send_message.call_args[0][0]
        assert sent_msg.get_content_type() == "multipart/mixed"
        parts = list(sent_msg.walk())
        # multipart/mixed container, text/plain body, application/pdf attachment
        content_types = [p.get_content_type() for p in parts]
        assert "text/plain" in content_types
        assert "application/pdf" in content_types

    def test_smtp_error_is_logged_and_reraised(self) -> None:
        """SMTP errors are logged and re-raised so callers can handle them."""
        email_mod.SMTP_HOST = "smtp.example.com"
        email_mod.USER = "bot@example.com"
        email_mod.PASSWORD = "pass"
        email_mod.FROM_ADDR = "bot@example.com"

        with patch(
            "channels.email.smtplib.SMTP_SSL",
            side_effect=smtplib.SMTPException("connection failed"),
        ):
            with pytest.raises(smtplib.SMTPException, match="connection failed"):
                email_mod.send_reply("user@example.com", "Re: Hi", "Body")


# ─── 4. Poll loop ────────────────────────────────────────────────


class TestPollLoop:
    """Tests for poll_loop — one iteration, then break via CancelledError."""

    def _setup_globals(self) -> None:
        email_mod.URL = "http://test-daemon:8100"
        email_mod.USER = "bot@example.com"
        email_mod.IMAP_HOST = "imap.example.com"
        email_mod.SMTP_HOST = "smtp.example.com"
        email_mod.PASSWORD = "pass"
        email_mod.POLL_INTERVAL = 10
        email_mod.FROM_ADDR = "bot@example.com"

    async def test_normal_flow_fetches_posts_and_replies(self) -> None:
        """Normal flow: fetch email, POST to daemon, send reply via SMTP."""
        self._setup_globals()

        fetched: list[dict[str, Any]] = [{
            "uid": b"1",
            "from": "sender@example.com",
            "subject": "Hello",
            "body": "Question?",
        }]

        mock_response = MagicMock()
        mock_response.json.return_value = {"reply": "Answer!"}

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        with (
            patch.object(
                email_mod, "fetch_and_mark",
                side_effect=[fetched, []],  # second call after sleep raises
            ),
            patch.object(email_mod, "send_reply") as mock_send,
            patch("channels.email.httpx.AsyncClient") as mock_ac,
            patch("channels.email.asyncio.sleep", side_effect=asyncio.CancelledError),
            patch(
                "channels.email.asyncio.get_event_loop",
            ) as mock_loop,
        ):
            mock_ac.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_ac.return_value.__aexit__ = AsyncMock(return_value=False)
            # run_in_executor calls the sync functions directly
            async def _run_in_executor(
                _pool: Any, fn: Any, *args: Any,
            ) -> Any:
                return fn(*args)
            mock_loop.return_value.run_in_executor = AsyncMock(
                side_effect=_run_in_executor,
            )

            with pytest.raises(asyncio.CancelledError):
                await email_mod.poll_loop()

        mock_client.post.assert_called_once()
        post_kwargs = mock_client.post.call_args
        assert post_kwargs[0][0].endswith("/api/v1/inbound/email")
        assert post_kwargs[1]["json"]["message"] == "Question?"

        mock_send.assert_called_once_with("sender@example.com", "Re: Hello", "Answer!", None)

    async def test_empty_body_email_marked_read_without_posting(self) -> None:
        """Emails with empty body are marked as read, but not posted to daemon."""
        self._setup_globals()

        fetched: list[dict[str, Any]] = [{
            "uid": b"2",
            "from": "sender@example.com",
            "subject": "Empty",
            "body": "",
        }]

        mock_client = AsyncMock()

        # Let two iterations run: first sleep succeeds, second raises
        sleep_effects: list[Any] = [None, asyncio.CancelledError()]

        with (
            patch.object(
                email_mod, "fetch_and_mark",
                side_effect=[fetched, []],
            ) as mock_fetch,
            patch.object(email_mod, "send_reply") as mock_send,
            patch("channels.email.httpx.AsyncClient") as mock_ac,
            patch("channels.email.asyncio.sleep", side_effect=sleep_effects),
            patch("channels.email.asyncio.get_event_loop") as mock_loop,
        ):
            mock_ac.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_ac.return_value.__aexit__ = AsyncMock(return_value=False)

            async def _run_in_executor(
                _pool: Any, fn: Any, *args: Any,
            ) -> Any:
                return fn(*args)
            mock_loop.return_value.run_in_executor = AsyncMock(
                side_effect=_run_in_executor,
            )

            with pytest.raises(asyncio.CancelledError):
                await email_mod.poll_loop()

        mock_client.post.assert_not_called()
        mock_send.assert_not_called()
        # On second iteration, UID b"2" should have been passed as processed
        assert mock_fetch.call_args_list[1] == call([b"2"])

    async def test_unauthorized_sender_ignored_and_marked_read(self) -> None:
        """Emails from senders not in allowed_senders are skipped."""
        self._setup_globals()
        email_mod.ALLOWED_SENDERS = ["trusted@example.com"]

        fetched: list[dict[str, Any]] = [{
            "uid": b"50",
            "from": "stranger@evil.com",
            "subject": "Hello",
            "body": "I want in",
        }]

        mock_client = AsyncMock()
        sleep_effects: list[Any] = [None, asyncio.CancelledError()]

        with (
            patch.object(
                email_mod, "fetch_and_mark",
                side_effect=[fetched, []],
            ) as mock_fetch,
            patch.object(email_mod, "send_reply") as mock_send,
            patch("channels.email.httpx.AsyncClient") as mock_ac,
            patch("channels.email.asyncio.sleep", side_effect=sleep_effects),
            patch("channels.email.asyncio.get_event_loop") as mock_loop,
        ):
            mock_ac.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_ac.return_value.__aexit__ = AsyncMock(return_value=False)

            async def _run_in_executor(
                _pool: Any, fn: Any, *args: Any,
            ) -> Any:
                return fn(*args)
            mock_loop.return_value.run_in_executor = AsyncMock(
                side_effect=_run_in_executor,
            )

            with pytest.raises(asyncio.CancelledError):
                await email_mod.poll_loop()

        mock_client.post.assert_not_called()
        mock_send.assert_not_called()
        # UID should still be marked as processed (read)
        assert mock_fetch.call_args_list[1] == call([b"50"])

    async def test_inbound_attachments_forwarded_to_daemon(self) -> None:
        """Inbound email attachments are included in the POST to daemon."""
        self._setup_globals()

        inbound_att = {
            "content_type": "image/png",
            "data": base64.b64encode(b"png-bytes").decode(),
            "filename": "photo.png",
        }
        fetched: list[dict[str, Any]] = [{
            "uid": b"60",
            "from": "sender@example.com",
            "subject": "Photo",
            "body": "See attached",
            "attachments": [inbound_att],
        }]

        mock_response = MagicMock()
        mock_response.json.return_value = {"reply": "Got it!", "attachments": []}

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        with (
            patch.object(email_mod, "fetch_and_mark", side_effect=[fetched, []]),
            patch.object(email_mod, "send_reply") as mock_send,
            patch("channels.email.httpx.AsyncClient") as mock_ac,
            patch("channels.email.asyncio.sleep", side_effect=asyncio.CancelledError),
            patch("channels.email.asyncio.get_event_loop") as mock_loop,
        ):
            mock_ac.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_ac.return_value.__aexit__ = AsyncMock(return_value=False)

            async def _run_in_executor(
                _pool: Any, fn: Any, *args: Any,
            ) -> Any:
                return fn(*args)
            mock_loop.return_value.run_in_executor = AsyncMock(
                side_effect=_run_in_executor,
            )

            with pytest.raises(asyncio.CancelledError):
                await email_mod.poll_loop()

        # Verify attachments were included in POST
        post_kwargs = mock_client.post.call_args
        assert post_kwargs[1]["json"]["attachments"] == [inbound_att]

        # Verify reply was sent (no outbound attachments)
        mock_send.assert_called_once_with(
            "sender@example.com", "Re: Photo", "Got it!", None,
        )

    async def test_outbound_attachments_passed_to_send_reply(self) -> None:
        """Outbound attachments from daemon are passed to send_reply."""
        self._setup_globals()

        fetched: list[dict[str, Any]] = [{
            "uid": b"61",
            "from": "sender@example.com",
            "subject": "Generate PDF",
            "body": "Make me a report",
            "attachments": [],
        }]

        outbound_att = {
            "content_type": "application/pdf",
            "data": base64.b64encode(b"pdf-content").decode(),
            "filename": "report.pdf",
        }
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "reply": "Here's your report",
            "attachments": [outbound_att],
        }

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        with (
            patch.object(email_mod, "fetch_and_mark", side_effect=[fetched, []]),
            patch.object(email_mod, "send_reply") as mock_send,
            patch("channels.email.httpx.AsyncClient") as mock_ac,
            patch("channels.email.asyncio.sleep", side_effect=asyncio.CancelledError),
            patch("channels.email.asyncio.get_event_loop") as mock_loop,
        ):
            mock_ac.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_ac.return_value.__aexit__ = AsyncMock(return_value=False)

            async def _run_in_executor(
                _pool: Any, fn: Any, *args: Any,
            ) -> Any:
                return fn(*args)
            mock_loop.return_value.run_in_executor = AsyncMock(
                side_effect=_run_in_executor,
            )

            with pytest.raises(asyncio.CancelledError):
                await email_mod.poll_loop()

        mock_send.assert_called_once_with(
            "sender@example.com", "Re: Generate PDF",
            "Here's your report", [outbound_att],
        )

    async def test_reply_subject_gets_re_prefix(self) -> None:
        """Reply subject gets 'Re: ' prepended when not already present."""
        self._setup_globals()

        fetched: list[dict[str, Any]] = [{
            "uid": b"3",
            "from": "a@b.com",
            "subject": "Hello",
            "body": "Hi",
        }]

        mock_response = MagicMock()
        mock_response.json.return_value = {"reply": "World"}

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        with (
            patch.object(email_mod, "fetch_and_mark", side_effect=[fetched, []]),
            patch.object(email_mod, "send_reply") as mock_send,
            patch("channels.email.httpx.AsyncClient") as mock_ac,
            patch("channels.email.asyncio.sleep", side_effect=asyncio.CancelledError),
            patch("channels.email.asyncio.get_event_loop") as mock_loop,
        ):
            mock_ac.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_ac.return_value.__aexit__ = AsyncMock(return_value=False)

            async def _run_in_executor(
                _pool: Any, fn: Any, *args: Any,
            ) -> Any:
                return fn(*args)
            mock_loop.return_value.run_in_executor = AsyncMock(
                side_effect=_run_in_executor,
            )

            with pytest.raises(asyncio.CancelledError):
                await email_mod.poll_loop()

        assert mock_send.call_args[0][1] == "Re: Hello"

    async def test_reply_subject_keeps_existing_re_prefix(self) -> None:
        """Subject already starting with 'Re:' is not double-prefixed."""
        self._setup_globals()

        fetched: list[dict[str, Any]] = [{
            "uid": b"4",
            "from": "a@b.com",
            "subject": "RE: Already replied",
            "body": "Again",
        }]

        mock_response = MagicMock()
        mock_response.json.return_value = {"reply": "Ack"}

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        with (
            patch.object(email_mod, "fetch_and_mark", side_effect=[fetched, []]),
            patch.object(email_mod, "send_reply") as mock_send,
            patch("channels.email.httpx.AsyncClient") as mock_ac,
            patch("channels.email.asyncio.sleep", side_effect=asyncio.CancelledError),
            patch("channels.email.asyncio.get_event_loop") as mock_loop,
        ):
            mock_ac.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_ac.return_value.__aexit__ = AsyncMock(return_value=False)

            async def _run_in_executor(
                _pool: Any, fn: Any, *args: Any,
            ) -> Any:
                return fn(*args)
            mock_loop.return_value.run_in_executor = AsyncMock(
                side_effect=_run_in_executor,
            )

            with pytest.raises(asyncio.CancelledError):
                await email_mod.poll_loop()

        assert mock_send.call_args[0][1] == "RE: Already replied"

    async def test_daemon_error_uid_not_marked_as_read(self) -> None:
        """When daemon POST fails, the UID is NOT added to processed list."""
        self._setup_globals()

        fetched: list[dict[str, Any]] = [{
            "uid": b"5",
            "from": "a@b.com",
            "subject": "Boom",
            "body": "Crash",
        }]

        mock_client = AsyncMock()
        mock_client.post.side_effect = Exception("daemon down")

        # Let two iterations run so we can inspect the second fetch_and_mark call
        sleep_effects: list[Any] = [None, asyncio.CancelledError()]

        with (
            patch.object(
                email_mod, "fetch_and_mark",
                side_effect=[fetched, []],
            ) as mock_fetch,
            patch.object(email_mod, "send_reply") as mock_send,
            patch("channels.email.httpx.AsyncClient") as mock_ac,
            patch("channels.email.asyncio.sleep", side_effect=sleep_effects),
            patch("channels.email.asyncio.get_event_loop") as mock_loop,
        ):
            mock_ac.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_ac.return_value.__aexit__ = AsyncMock(return_value=False)

            async def _run_in_executor(
                _pool: Any, fn: Any, *args: Any,
            ) -> Any:
                return fn(*args)
            mock_loop.return_value.run_in_executor = AsyncMock(
                side_effect=_run_in_executor,
            )

            with pytest.raises(asyncio.CancelledError):
                await email_mod.poll_loop()

        mock_send.assert_not_called()
        # Second call to fetch_and_mark should get empty processed list
        assert mock_fetch.call_args_list[1] == call([])

    async def test_daemon_returns_empty_reply_no_smtp_send(self) -> None:
        """When daemon reply is empty, no SMTP send occurs."""
        self._setup_globals()

        fetched: list[dict[str, Any]] = [{
            "uid": b"6",
            "from": "a@b.com",
            "subject": "NoReply",
            "body": "Ping",
        }]

        mock_response = MagicMock()
        mock_response.json.return_value = {"reply": ""}

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        with (
            patch.object(email_mod, "fetch_and_mark", side_effect=[fetched, []]),
            patch.object(email_mod, "send_reply") as mock_send,
            patch("channels.email.httpx.AsyncClient") as mock_ac,
            patch("channels.email.asyncio.sleep", side_effect=asyncio.CancelledError),
            patch("channels.email.asyncio.get_event_loop") as mock_loop,
        ):
            mock_ac.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_ac.return_value.__aexit__ = AsyncMock(return_value=False)

            async def _run_in_executor(
                _pool: Any, fn: Any, *args: Any,
            ) -> Any:
                return fn(*args)
            mock_loop.return_value.run_in_executor = AsyncMock(
                side_effect=_run_in_executor,
            )

            with pytest.raises(asyncio.CancelledError):
                await email_mod.poll_loop()

        mock_send.assert_not_called()


# ─── 5. Main function ────────────────────────────────────────────


class TestMain:
    async def test_exits_with_error_when_config_missing(self) -> None:
        """main() exits when required config (imap/smtp/user/password) is missing."""
        email_mod.IMAP_HOST = ""
        email_mod.SMTP_HOST = ""
        email_mod.USER = ""
        email_mod.PASSWORD = ""

        with (
            patch.object(email_mod, "load_config"),  # no-op, globals stay empty
            pytest.raises(SystemExit),
        ):
            await email_mod.main()

    async def test_calls_load_config_then_poll_loop(self) -> None:
        """main() calls load_config(), checks config, then calls poll_loop()."""
        email_mod.IMAP_HOST = "imap.example.com"
        email_mod.SMTP_HOST = "smtp.example.com"
        email_mod.USER = "user@example.com"
        email_mod.PASSWORD = "pass"

        with (
            patch.object(email_mod, "load_config") as mock_load,
            patch.object(email_mod, "poll_loop", new_callable=AsyncMock) as mock_poll,
        ):
            await email_mod.main()

        mock_load.assert_called_once()
        mock_poll.assert_called_once()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 6. Delivery failure notification
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDeliveryFailureNotification:
    @pytest.mark.asyncio
    async def test_smtp_failure_notifies_daemon(self) -> None:
        """SMTP failure POSTs to /api/v1/system/event with sender=error."""
        email_mod.URL = "http://daemon:8100"
        mock_client = AsyncMock()
        mock_client.post.return_value = MagicMock(status_code=202)

        err = smtplib.SMTPException("relay denied")
        await email_mod._notify_delivery_failure(mock_client, "user@x.com", err)

        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert "/api/v1/system/event" in call_args.args[0]
        body = call_args.kwargs["json"]
        assert "user@x.com" in body["message"]
        assert body["sender"] == "error"

    @pytest.mark.asyncio
    async def test_notify_failure_does_not_raise(self) -> None:
        """If notify POST fails, it logs but doesn't raise."""
        email_mod.URL = "http://daemon:8100"
        mock_client = AsyncMock()
        mock_client.post.side_effect = Exception("daemon down")

        # Must not raise
        await email_mod._notify_delivery_failure(
            mock_client, "user@x.com", Exception("smtp fail"),
        )

    @pytest.mark.asyncio
    async def test_smtp_failure_uid_not_marked_processed(self) -> None:
        """When SMTP send fails, UID is not marked as processed for retry."""
        email_mod.URL = "http://test-daemon:8100"
        email_mod.USER = "bot@example.com"
        email_mod.IMAP_HOST = "imap.example.com"
        email_mod.SMTP_HOST = "smtp.example.com"
        email_mod.PASSWORD = "pass"
        email_mod.POLL_INTERVAL = 10
        email_mod.FROM_ADDR = "bot@example.com"

        fetched: list[dict[str, Any]] = [{
            "uid": b"7",
            "from": "sender@x.com",
            "subject": "Fail",
            "body": "test",
        }]

        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"reply": "hi"}
        mock_client.post.return_value = mock_resp

        sleep_effects: list[Any] = [None, asyncio.CancelledError()]

        def _smtp_fail(*_args: Any, **_kw: Any) -> None:
            raise smtplib.SMTPException("relay denied")

        with (
            patch.object(
                email_mod, "fetch_and_mark",
                side_effect=[fetched, []],
            ) as mock_fetch,
            patch.object(email_mod, "send_reply", side_effect=_smtp_fail),
            patch("channels.email.httpx.AsyncClient") as mock_ac,
            patch("channels.email.asyncio.sleep", side_effect=sleep_effects),
            patch("channels.email.asyncio.get_event_loop") as mock_loop,
            patch.object(email_mod, "_notify_delivery_failure", new_callable=AsyncMock),
        ):
            mock_ac.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_ac.return_value.__aexit__ = AsyncMock(return_value=False)

            async def _run_in_executor(
                _pool: Any, fn: Any, *args: Any,
            ) -> Any:
                return fn(*args)
            mock_loop.return_value.run_in_executor = AsyncMock(
                side_effect=_run_in_executor,
            )

            with pytest.raises(asyncio.CancelledError):
                await email_mod.poll_loop()

        # UID 7 should NOT be in the processed list on the second fetch call
        assert mock_fetch.call_args_list[1] == call([])
