"""Tests for channels/telegram.py — target resolution, chunking, message parsing,
send/receive/react with mocked httpx, reconnect behavior, attachment extraction."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from channels import InboundMessage
from channels.telegram import (
    TelegramChannel,
    _guess_mime,
)


def _make_channel(**overrides):
    defaults = {
        "token": "fake-token",
        "allow_from": [111, 222],
        "chunk_limit": 100,
        "contacts": {"Nicolas": 111, "Alice": 222},
        "download_dir": "/tmp/lucyd-telegram-test",
    }
    defaults.update(overrides)
    return TelegramChannel(**defaults)


def _mock_response(json_data, status_code=200):
    """Create a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    resp.content = b""
    return resp


# ─── Target Resolution ────────────────────────────────────────────


class TestResolveTarget:
    def test_resolve_by_name(self):
        ch = _make_channel()
        assert ch._resolve_target("Nicolas") == 111

    def test_resolve_case_insensitive(self):
        ch = _make_channel()
        assert ch._resolve_target("nicolas") == 111
        assert ch._resolve_target("NICOLAS") == 111

    def test_resolve_by_numeric_string(self):
        ch = _make_channel()
        assert ch._resolve_target("999") == 999

    def test_resolve_unknown_name_raises(self):
        ch = _make_channel()
        with pytest.raises(ValueError, match="Unknown contact"):
            ch._resolve_target("Bob")

    def test_resolve_unknown_lists_available(self):
        ch = _make_channel()
        with pytest.raises(ValueError, match="Nicolas"):
            ch._resolve_target("Bob")

    def test_self_send_blocked(self):
        ch = _make_channel()
        ch._bot_id = 111
        with pytest.raises(ValueError, match="Self-send blocked"):
            ch._resolve_target("Nicolas")

    def test_self_send_blocked_by_numeric(self):
        ch = _make_channel()
        ch._bot_id = 999
        with pytest.raises(ValueError, match="Self-send blocked"):
            ch._resolve_target("999")

    def test_negative_chat_id(self):
        """Group chats have negative IDs."""
        ch = _make_channel()
        assert ch._resolve_target("-100123456") == -100123456


# ─── Contact Mapping ──────────────────────────────────────────────


class TestContactMapping:
    def test_id_to_name_populated(self):
        ch = _make_channel()
        assert ch._id_to_name[111] == "Nicolas"
        assert ch._id_to_name[222] == "Alice"

    def test_contacts_stored_lowercase(self):
        ch = _make_channel(contacts={"TestUser": 333})
        assert "testuser" in ch._contacts
        assert ch._contacts["testuser"] == 333

    def test_no_contacts(self):
        ch = _make_channel(contacts=None)
        assert ch._contacts == {}
        assert ch._id_to_name == {}


# ─── Text Chunking ────────────────────────────────────────────────


class TestChunkText:
    def test_short_text_single_chunk(self):
        ch = _make_channel(chunk_limit=100)
        assert ch._chunk_text("hello") == ["hello"]

    def test_exact_limit_single_chunk(self):
        ch = _make_channel(chunk_limit=5)
        assert ch._chunk_text("hello") == ["hello"]

    def test_split_on_newlines(self):
        ch = _make_channel(chunk_limit=10)
        result = ch._chunk_text("aaa\nbbb\nccc\nddd")
        for chunk in result:
            assert len(chunk) <= 10

    def test_hard_split_long_line(self):
        ch = _make_channel(chunk_limit=5)
        result = ch._chunk_text("abcdefghij")
        assert result == ["abcde", "fghij"]

    def test_preserves_all_content(self):
        ch = _make_channel(chunk_limit=10)
        text = "line one\nline two\nline three"
        result = ch._chunk_text(text)
        reassembled = "\n".join(result)
        assert "line one" in reassembled
        assert "line two" in reassembled
        assert "line three" in reassembled


# ─── Message Parsing ──────────────────────────────────────────────


class TestParseMessage:
    @pytest.mark.asyncio
    async def test_basic_text_message(self):
        ch = _make_channel()
        msg = await ch._parse_message({
            "from": {"id": 111, "username": "nico"},
            "chat": {"id": 111},
            "message_id": 42,
            "text": "hello lucy",
        })
        assert msg is not None
        assert msg.text == "hello lucy"
        assert msg.sender == "Nicolas"
        assert msg.source == "telegram"
        assert msg.timestamp == 42.0

    @pytest.mark.asyncio
    async def test_caption_used_when_no_text(self):
        ch = _make_channel()
        msg = await ch._parse_message({
            "from": {"id": 111},
            "chat": {"id": 111},
            "message_id": 1,
            "caption": "look at this",
        })
        assert msg is not None
        assert msg.text == "look at this"

    @pytest.mark.asyncio
    async def test_skip_bot_own_messages(self):
        ch = _make_channel()
        ch._bot_id = 999
        msg = await ch._parse_message({
            "from": {"id": 999},
            "chat": {"id": 111},
            "message_id": 1,
            "text": "echo",
        })
        assert msg is None

    @pytest.mark.asyncio
    async def test_skip_non_allowed_user(self):
        ch = _make_channel(allow_from=[111])
        msg = await ch._parse_message({
            "from": {"id": 999, "username": "stranger"},
            "chat": {"id": 999},
            "message_id": 1,
            "text": "hello",
        })
        assert msg is None

    @pytest.mark.asyncio
    async def test_allow_from_empty_allows_all(self):
        ch = _make_channel(allow_from=None)
        msg = await ch._parse_message({
            "from": {"id": 999, "username": "anyone"},
            "chat": {"id": 999},
            "message_id": 1,
            "text": "hello",
        })
        assert msg is not None

    @pytest.mark.asyncio
    async def test_skip_empty_message(self):
        ch = _make_channel()
        msg = await ch._parse_message({
            "from": {"id": 111},
            "chat": {"id": 111},
            "message_id": 1,
        })
        assert msg is None

    @pytest.mark.asyncio
    async def test_sender_fallback_to_username_when_not_in_contacts(self):
        ch = _make_channel(contacts={})
        msg = await ch._parse_message({
            "from": {"id": 111, "username": "nico_tg"},
            "chat": {"id": 111},
            "message_id": 1,
            "text": "hi",
        })
        assert msg.sender == "nico_tg"

    @pytest.mark.asyncio
    async def test_sender_fallback_to_first_name(self):
        ch = _make_channel(contacts={})
        msg = await ch._parse_message({
            "from": {"id": 111, "first_name": "Nicolas"},
            "chat": {"id": 111},
            "message_id": 1,
            "text": "hi",
        })
        assert msg.sender == "Nicolas"

    @pytest.mark.asyncio
    async def test_sender_fallback_to_user_id_string(self):
        ch = _make_channel(contacts={})
        msg = await ch._parse_message({
            "from": {"id": 111},
            "chat": {"id": 111},
            "message_id": 1,
            "text": "hi",
        })
        assert msg.sender == "111"

    @pytest.mark.asyncio
    async def test_message_id_tracked(self):
        ch = _make_channel()
        msg = await ch._parse_message({
            "from": {"id": 111},
            "chat": {"id": 111},
            "message_id": 42,
            "text": "hi",
        })
        assert msg.text == "hi"

    # ─── Quote / Reply extraction ─────────────────────────────────

    @pytest.mark.asyncio
    async def test_reply_to_text_message_extracts_quote(self):
        ch = _make_channel()
        msg = await ch._parse_message({
            "from": {"id": 111}, "chat": {"id": 111},
            "message_id": 50, "text": "totally agree",
            "reply_to_message": {
                "message_id": 49, "from": {"id": 999, "is_bot": True},
                "text": "here is my take on the situation",
            },
        })
        assert msg is not None
        assert msg.text == "totally agree"
        assert msg.quote == "here is my take on the situation"

    @pytest.mark.asyncio
    async def test_reply_to_caption_message(self):
        ch = _make_channel()
        msg = await ch._parse_message({
            "from": {"id": 111}, "chat": {"id": 111},
            "message_id": 50, "text": "nice",
            "reply_to_message": {
                "message_id": 49, "caption": "sunset photo",
            },
        })
        assert msg is not None
        assert msg.quote == "sunset photo"

    @pytest.mark.asyncio
    async def test_telegram_quote_selection_preferred(self):
        """Telegram's partial text selection (quote object) takes priority."""
        ch = _make_channel()
        msg = await ch._parse_message({
            "from": {"id": 111}, "chat": {"id": 111},
            "message_id": 50, "text": "exactly this",
            "reply_to_message": {
                "message_id": 49, "text": "long message with many sentences in it",
            },
            "quote": {"text": "many sentences"},
        })
        assert msg is not None
        assert msg.quote == "many sentences"

    @pytest.mark.asyncio
    async def test_reply_to_voice_message(self):
        ch = _make_channel()
        msg = await ch._parse_message({
            "from": {"id": 111}, "chat": {"id": 111},
            "message_id": 50, "text": "haha yeah",
            "reply_to_message": {
                "message_id": 49,
                "voice": {"file_id": "v1", "duration": 5},
            },
        })
        assert msg is not None
        assert msg.quote == "[voice message]"

    @pytest.mark.asyncio
    async def test_reply_to_photo(self):
        ch = _make_channel()
        msg = await ch._parse_message({
            "from": {"id": 111}, "chat": {"id": 111},
            "message_id": 50, "text": "wow",
            "reply_to_message": {
                "message_id": 49,
                "photo": [{"file_id": "p1", "file_size": 100}],
            },
        })
        assert msg is not None
        assert msg.quote == "[photo]"

    @pytest.mark.asyncio
    async def test_reply_to_sticker_with_emoji(self):
        ch = _make_channel()
        msg = await ch._parse_message({
            "from": {"id": 111}, "chat": {"id": 111},
            "message_id": 50, "text": "lol",
            "reply_to_message": {
                "message_id": 49,
                "sticker": {"file_id": "s1", "emoji": "😂"},
            },
        })
        assert msg is not None
        assert msg.quote == "[sticker 😂]"

    @pytest.mark.asyncio
    async def test_reply_to_document(self):
        ch = _make_channel()
        msg = await ch._parse_message({
            "from": {"id": 111}, "chat": {"id": 111},
            "message_id": 50, "text": "got it",
            "reply_to_message": {
                "message_id": 49,
                "document": {"file_id": "d1", "file_name": "report.pdf"},
            },
        })
        assert msg is not None
        assert msg.quote == "[document: report.pdf]"

    @pytest.mark.asyncio
    async def test_no_reply_no_quote(self):
        ch = _make_channel()
        msg = await ch._parse_message({
            "from": {"id": 111}, "chat": {"id": 111},
            "message_id": 50, "text": "just a message",
        })
        assert msg is not None
        assert msg.quote is None


# ─── Connect ──────────────────────────────────────────────────────


class TestConnect:
    @pytest.mark.asyncio
    async def test_connect_sets_bot_identity(self):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True,
            "result": {"id": 12345, "username": "lucybot"},
        })
        ch._client = mock_client

        await ch.connect()

        assert ch._bot_id == 12345

    @pytest.mark.asyncio
    async def test_connect_calls_getMe(self):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True,
            "result": {"id": 1, "username": "bot"},
        })
        ch._client = mock_client

        await ch.connect()

        call_url = mock_client.post.call_args[0][0]
        assert "getMe" in call_url

    @pytest.mark.asyncio
    async def test_connect_failure_raises(self):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.side_effect = httpx.ConnectError("refused")
        ch._client = mock_client

        with pytest.raises(ConnectionError, match="unreachable"):
            await ch.connect()


# ─── Send ─────────────────────────────────────────────────────────


class TestSend:
    @pytest.mark.asyncio
    async def test_send_text_calls_sendMessage(self):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        await ch.send("Nicolas", "hello from tests")

        call_url = mock_client.post.call_args[0][0]
        assert "sendMessage" in call_url
        call_json = mock_client.post.call_args[1]["json"]
        assert call_json["chat_id"] == 111
        assert call_json["text"] == "hello from tests"

    @pytest.mark.asyncio
    async def test_send_chunks_long_text(self):
        ch = _make_channel(chunk_limit=10)
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        await ch.send("Nicolas", "aaaa\nbbbb\ncccc\ndddd")

        # Should be multiple calls (text doesn't fit in 10 chars)
        assert mock_client.post.call_count > 1
        # All calls should be sendMessage
        for call in mock_client.post.call_args_list:
            assert "sendMessage" in call[0][0]

    @pytest.mark.asyncio
    async def test_send_unknown_target_raises(self):
        ch = _make_channel()
        with pytest.raises(ValueError, match="Unknown contact"):
            await ch.send("Nobody", "hello")

    @pytest.mark.asyncio
    async def test_send_empty_text_no_call(self):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        ch._client = mock_client

        await ch.send("Nicolas", "")

        mock_client.post.assert_not_called()


# ─── Send Typing ──────────────────────────────────────────────────


class TestSendTyping:
    @pytest.mark.asyncio
    async def test_typing_calls_sendChatAction(self):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": True})
        ch._client = mock_client

        await ch.send_typing("Nicolas")

        call_url = mock_client.post.call_args[0][0]
        assert "sendChatAction" in call_url
        call_json = mock_client.post.call_args[1]["json"]
        assert call_json["chat_id"] == 111
        assert call_json["action"] == "typing"

    @pytest.mark.asyncio
    async def test_typing_failure_silent(self):
        """Typing errors should not propagate."""
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.side_effect = httpx.ConnectError("timeout")
        ch._client = mock_client

        # Should not raise
        await ch.send_typing("Nicolas")


# ─── Send Reaction ────────────────────────────────────────────────


class TestSendReaction:
    @pytest.mark.asyncio
    async def test_reaction_calls_setMessageReaction(self):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": True})
        ch._client = mock_client

        # ts = message_id * 1000 (as the daemon would pass it)
        await ch.send_reaction("Nicolas", "👍", 42000)

        call_url = mock_client.post.call_args[0][0]
        assert "setMessageReaction" in call_url
        call_json = mock_client.post.call_args[1]["json"]
        assert call_json["chat_id"] == 111
        assert call_json["message_id"] == 42
        assert call_json["reaction"] == [{"type": "emoji", "emoji": "👍"}]

    @pytest.mark.asyncio
    async def test_reaction_recovers_message_id(self):
        """Verify the ts // 1000 round-trip recovers the original message_id."""
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": True})
        ch._client = mock_client

        await ch.send_reaction("Nicolas", "🔥", 99999000)

        call_json = mock_client.post.call_args[1]["json"]
        assert call_json["message_id"] == 99999

    @pytest.mark.asyncio
    async def test_reaction_invalid_emoji_raises(self):
        """Emoji not in Telegram's allowed set should raise ValueError."""
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        ch._client = mock_client

        with pytest.raises(ValueError, match="not a valid Telegram reaction"):
            await ch.send_reaction("Nicolas", "🦇", 42000)

        mock_client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_reaction_invalid_message_id_raises(self):
        """ts=0 should raise ValueError, not call the API."""
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        ch._client = mock_client

        with pytest.raises(ValueError, match="Invalid message_id"):
            await ch.send_reaction("Nicolas", "👍", 0)

        mock_client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_reaction_failure_propagates(self):
        """Reaction errors propagate so tool_react can report them to Lucy."""
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.side_effect = RuntimeError("API error")
        ch._client = mock_client

        with pytest.raises(RuntimeError, match="API error"):
            await ch.send_reaction("Nicolas", "👍", 42000)


# ─── API Error Handling ───────────────────────────────────────────


class TestApiErrors:
    @pytest.mark.asyncio
    async def test_api_not_ok_raises(self):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": False,
            "description": "Unauthorized",
        })
        ch._client = mock_client

        with pytest.raises(RuntimeError, match="Unauthorized"):
            await ch._api("getMe")

    @pytest.mark.asyncio
    async def test_api_http_error_propagates(self):
        """Non-JSON HTTP error (e.g. proxy 502) falls back to raise_for_status."""
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        resp = _mock_response({})
        resp.json.side_effect = ValueError("not JSON")
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "502", request=MagicMock(), response=resp
        )
        mock_client.post.return_value = resp
        ch._client = mock_client

        with pytest.raises(httpx.HTTPStatusError):
            await ch._api("getMe")


# ─── Reaction Message ID Round-Trip ───────────────────────────────


class TestReactionRoundTrip:
    def test_message_id_survives_daemon_conversion(self):
        """message_id -> float timestamp -> int(ts * 1000) -> ts // 1000 -> message_id."""
        message_id = 12345
        timestamp = float(message_id)
        ts = int(timestamp * 1000)
        assert ts == 12345000
        recovered = ts // 1000
        assert recovered == message_id

    def test_round_trip_large_message_id(self):
        message_id = 9999999
        timestamp = float(message_id)
        ts = int(timestamp * 1000)
        recovered = ts // 1000
        assert recovered == message_id


# ─── MIME Guessing ─────────────────────────────────────────────────


class TestGuessMime:
    def test_common_types(self):
        assert _guess_mime(Path("photo.jpg")) == "image/jpeg"
        assert _guess_mime(Path("voice.ogg")) == "audio/ogg"
        assert _guess_mime(Path("video.mp4")) == "video/mp4"
        assert _guess_mime(Path("doc.pdf")) == "application/pdf"

    def test_unknown_extension(self):
        assert _guess_mime(Path("data.xyz")) == "application/octet-stream"

    def test_case_insensitive(self):
        assert _guess_mime(Path("PHOTO.JPG")) == "image/jpeg"


# ─── Download File ─────────────────────────────────────────────────


class TestDownloadFile:
    @pytest.mark.asyncio
    async def test_download_returns_attachment(self, tmp_path):
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        # First call: getFile
        get_file_resp = _mock_response({
            "ok": True,
            "result": {"file_path": "photos/file_1.jpg"},
        })
        # Second call: actual download
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"\xff\xd8\xff\xe0fake-jpeg-data"

        mock_client.post.return_value = get_file_resp
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        att = await ch._download_file("file_id_123", content_type="image/jpeg", size=1024)

        assert att is not None
        assert att.content_type == "image/jpeg"
        assert att.size == 1024
        assert att.filename == "file_1.jpg"
        assert Path(att.local_path).exists()
        assert Path(att.local_path).read_bytes() == b"\xff\xd8\xff\xe0fake-jpeg-data"

    @pytest.mark.asyncio
    async def test_download_empty_file_id_returns_none(self):
        ch = _make_channel()
        att = await ch._download_file("")
        assert att is None

    @pytest.mark.asyncio
    async def test_download_failure_returns_none(self, tmp_path):
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.side_effect = httpx.ConnectError("timeout")
        ch._client = mock_client

        att = await ch._download_file("file_id_123")
        assert att is None

    @pytest.mark.asyncio
    async def test_download_traversal_filename_sanitized(self, tmp_path):
        """Filename with path traversal components saves as basename only."""
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        get_file_resp = _mock_response({
            "ok": True,
            "result": {"file_path": "docs/file_1.pdf"},
        })
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"fake-content"
        mock_client.post.return_value = get_file_resp
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        att = await ch._download_file(
            "file_id_456", content_type="application/pdf",
            filename="../../evil.pdf", size=100,
        )
        assert att is not None
        local = Path(att.local_path)
        assert local.parent == tmp_path
        # Basename must not contain directory separators
        assert "/" not in local.name.split("_", 1)[1]
        assert local.name.endswith("evil.pdf")
        # Attachment.filename is sanitized — no traversal components
        assert att.filename == "evil.pdf"


# ─── Poll Loop / getUpdates ────────────────────────────────────────


class TestPollLoop:
    @pytest.mark.asyncio
    async def test_poll_loop_yields_messages(self):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False

        # First call: return one update. Second call: raise to break the loop.
        call_count = 0

        async def mock_post(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_response({
                    "ok": True,
                    "result": [{
                        "update_id": 100,
                        "message": {
                            "from": {"id": 111, "username": "nico"},
                            "chat": {"id": 111},
                            "message_id": 7,
                            "text": "from poll",
                        },
                    }],
                })
            raise httpx.ConnectError("stop")

        mock_client.post = mock_post
        ch._client = mock_client

        messages = []
        with pytest.raises(httpx.ConnectError):
            async for msg in ch._poll_loop():
                messages.append(msg)

        assert len(messages) == 1
        assert messages[0].text == "from poll"
        assert messages[0].sender == "Nicolas"

    @pytest.mark.asyncio
    async def test_poll_loop_advances_offset(self):
        ch = _make_channel()
        assert ch._offset == 0

        mock_client = AsyncMock()
        mock_client.is_closed = False
        call_count = 0

        async def mock_post(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_response({
                    "ok": True,
                    "result": [{"update_id": 500, "message": {
                        "from": {"id": 111}, "chat": {"id": 111},
                        "message_id": 1, "text": "x",
                    }}],
                })
            raise httpx.ConnectError("stop")

        mock_client.post = mock_post
        ch._client = mock_client

        with pytest.raises(httpx.ConnectError):
            async for _ in ch._poll_loop():
                pass

        assert ch._offset == 501

    @pytest.mark.asyncio
    async def test_poll_loop_skips_updates_without_message(self):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        call_count = 0

        async def mock_post(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_response({
                    "ok": True,
                    "result": [
                        {"update_id": 1},  # No message key
                        {"update_id": 2, "message": {
                            "from": {"id": 111}, "chat": {"id": 111},
                            "message_id": 1, "text": "real",
                        }},
                    ],
                })
            raise httpx.ConnectError("stop")

        mock_client.post = mock_post
        ch._client = mock_client

        messages = []
        with pytest.raises(httpx.ConnectError):
            async for msg in ch._poll_loop():
                messages.append(msg)

        assert len(messages) == 1
        assert messages[0].text == "real"


# ─── Init / Constructor ─────────────────────────────────────────


class TestInit:
    def test_base_url_from_token(self):
        ch = _make_channel(token="ABC123")
        assert ch.base_url == "https://api.telegram.org/botABC123"

    def test_allow_from_stored_as_set(self):
        ch = _make_channel(allow_from=[111, 222])
        assert ch.allow_from == {111, 222}

    def test_allow_from_none_empty_set(self):
        ch = _make_channel(allow_from=None)
        assert ch.allow_from == set()

    def test_chunk_limit_stored(self):
        ch = _make_channel(chunk_limit=5000)
        assert ch.chunk_limit == 5000

    def test_download_dir_is_path(self, tmp_path):
        ch = _make_channel(download_dir=str(tmp_path / "downloads"))
        assert isinstance(ch.download_dir, Path)
        assert ch.download_dir == tmp_path / "downloads"
        assert ch.download_dir.exists()

    def test_bot_id_initially_zero(self):
        ch = _make_channel()
        assert ch._bot_id == 0

    def test_offset_initially_zero(self):
        ch = _make_channel()
        assert ch._offset == 0

    def test_client_initially_none(self):
        ch = _make_channel()
        assert ch._client is None

    def test_token_stored(self):
        ch = _make_channel(token="my-secret-token")
        assert ch.token == "my-secret-token"


# ─── Get Client ─────────────────────────────────────────────────


class TestGetClient:
    @pytest.mark.asyncio
    async def test_creates_client_when_none(self):
        ch = _make_channel()
        assert ch._client is None
        client = await ch._get_client()
        assert client is not None
        assert isinstance(client, httpx.AsyncClient)
        await client.aclose()

    @pytest.mark.asyncio
    async def test_reuses_open_client(self):
        ch = _make_channel()
        c1 = await ch._get_client()
        c2 = await ch._get_client()
        assert c1 is c2
        await c1.aclose()

    @pytest.mark.asyncio
    async def test_creates_new_when_closed(self):
        ch = _make_channel()
        c1 = await ch._get_client()
        await c1.aclose()
        c2 = await ch._get_client()
        assert c2 is not c1
        assert not c2.is_closed
        await c2.aclose()


# ─── API Method ──────────────────────────────────────────────────


class TestApi:
    @pytest.mark.asyncio
    async def test_url_includes_method_name(self):
        ch = _make_channel(token="tok123")
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        await ch._api("getMe")

        call_url = mock_client.post.call_args[0][0]
        assert call_url == "https://api.telegram.org/bottok123/getMe"

    @pytest.mark.asyncio
    async def test_params_sent_as_json(self):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        await ch._api("sendMessage", chat_id=111, text="hello")

        kwargs = mock_client.post.call_args[1]
        assert kwargs["json"] == {"chat_id": 111, "text": "hello"}
        assert "data" not in kwargs
        assert "files" not in kwargs

    @pytest.mark.asyncio
    async def test_files_sent_as_data_with_files(self):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        fake_files = {"photo": ("name.jpg", b"data", "image/jpeg")}
        await ch._api("sendPhoto", chat_id=111, _files=fake_files)

        kwargs = mock_client.post.call_args[1]
        assert kwargs["files"] == fake_files
        assert kwargs["data"] == {"chat_id": 111}
        assert "json" not in kwargs

    @pytest.mark.asyncio
    async def test_returns_result_dict(self):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True,
            "result": {"id": 42, "name": "test"},
        })
        ch._client = mock_client

        result = await ch._api("getMe")
        assert result == {"id": 42, "name": "test"}

    @pytest.mark.asyncio
    async def test_returns_empty_dict_when_no_result(self):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True})
        ch._client = mock_client

        result = await ch._api("getMe")
        assert result == {}

    @pytest.mark.asyncio
    async def test_error_includes_method_name(self):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": False, "description": "Bad Request"
        })
        ch._client = mock_client

        with pytest.raises(RuntimeError, match="getMe"):
            await ch._api("getMe")

    @pytest.mark.asyncio
    async def test_error_includes_description(self):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": False, "description": "Unauthorized"
        })
        ch._client = mock_client

        with pytest.raises(RuntimeError, match="Unauthorized"):
            await ch._api("getMe")

    @pytest.mark.asyncio
    async def test_error_fallback_to_status_code(self):
        """When no description, error message includes HTTP status code."""
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": False}, status_code=403)
        ch._client = mock_client

        with pytest.raises(RuntimeError, match="HTTP 403"):
            await ch._api("getMe")

    @pytest.mark.asyncio
    async def test_non_json_response_raises(self):
        """Non-JSON + successful raise_for_status → RuntimeError."""
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        resp = _mock_response({})
        resp.json.side_effect = ValueError("not JSON")
        resp.raise_for_status = MagicMock()  # doesn't raise
        resp.status_code = 200
        mock_client.post.return_value = resp
        ch._client = mock_client

        with pytest.raises(RuntimeError, match="non-JSON response"):
            await ch._api("badMethod")


# ─── Connect (Stronger Assertions) ──────────────────────────────


class TestConnectStrong:
    @pytest.mark.asyncio
    async def test_connect_calls_getMe_exact_url(self):
        ch = _make_channel(token="tok")
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {"id": 1, "username": "bot"},
        })
        ch._client = mock_client

        await ch.connect()

        call_url = mock_client.post.call_args[0][0]
        assert call_url == "https://api.telegram.org/bottok/getMe"

    @pytest.mark.asyncio
    async def test_connect_default_bot_id_when_missing(self):
        """When getMe returns no id, bot_id stays at default 0."""
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {},
        })
        ch._client = mock_client

        await ch.connect()
        assert ch._bot_id == 0

    @pytest.mark.asyncio
    async def test_connect_wraps_as_connection_error(self):
        """Any exception during connect is wrapped as ConnectionError."""
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.side_effect = RuntimeError("boom")
        ch._client = mock_client

        with pytest.raises(ConnectionError):
            await ch.connect()


# ─── Extract Attachments ─────────────────────────────────────────


class TestExtractAttachments:
    @pytest.mark.asyncio
    async def test_photo_extracted(self, tmp_path):
        """Photo message yields one image/jpeg attachment."""
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {"file_path": "photos/pic.jpg"},
        })
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"jpegdata"
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        atts = await ch._extract_attachments({
            "photo": [
                {"file_id": "small", "file_size": 100},
                {"file_id": "large", "file_size": 500},
            ],
        })

        assert len(atts) == 1
        assert atts[0].content_type == "image/jpeg"
        assert atts[0].size == 500
        # Should use the largest (last) photo
        post_call = mock_client.post.call_args
        assert "getFile" in post_call[0][0]
        assert post_call[1]["json"]["file_id"] == "large"

    @pytest.mark.asyncio
    async def test_voice_extracted(self, tmp_path):
        """Voice message yields attachment with voice's mime_type."""
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {"file_path": "voice/msg.ogg"},
        })
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"oggdata"
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        atts = await ch._extract_attachments({
            "voice": {"file_id": "v1", "file_size": 200, "mime_type": "audio/ogg"},
        })

        assert len(atts) == 1
        assert atts[0].content_type == "audio/ogg"
        assert atts[0].size == 200
        assert atts[0].is_voice is True

    @pytest.mark.asyncio
    async def test_document_extracted_with_filename(self, tmp_path):
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {"file_path": "docs/report.pdf"},
        })
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"pdfdata"
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        atts = await ch._extract_attachments({
            "document": {
                "file_id": "d1", "file_size": 300,
                "mime_type": "application/pdf", "file_name": "report.pdf",
            },
        })

        assert len(atts) == 1
        assert atts[0].content_type == "application/pdf"
        assert atts[0].filename == "report.pdf"
        assert atts[0].size == 300

    @pytest.mark.asyncio
    async def test_video_extracted(self, tmp_path):
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {"file_path": "vids/clip.mp4"},
        })
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"mp4data"
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        atts = await ch._extract_attachments({
            "video": {"file_id": "vid1", "file_size": 1000, "mime_type": "video/mp4"},
        })

        assert len(atts) == 1
        assert atts[0].content_type == "video/mp4"
        assert atts[0].size == 1000

    @pytest.mark.asyncio
    async def test_audio_extracted(self, tmp_path):
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {"file_path": "audio/song.mp3"},
        })
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"mp3data"
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        atts = await ch._extract_attachments({
            "audio": {
                "file_id": "a1", "file_size": 400,
                "mime_type": "audio/mpeg", "file_name": "song.mp3",
            },
        })

        assert len(atts) == 1
        assert atts[0].content_type == "audio/mpeg"
        assert atts[0].filename == "song.mp3"
        assert atts[0].is_voice is False

    @pytest.mark.asyncio
    async def test_sticker_extracted(self, tmp_path):
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {"file_path": "stickers/s.webp"},
        })
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"webpdata"
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        atts = await ch._extract_attachments({
            "sticker": {"file_id": "st1", "file_size": 50},
        })

        assert len(atts) == 1
        assert atts[0].content_type == "image/webp"

    @pytest.mark.asyncio
    async def test_no_attachments_returns_empty_list(self):
        ch = _make_channel()
        atts = await ch._extract_attachments({"text": "just text"})
        assert atts == []

    @pytest.mark.asyncio
    async def test_multiple_attachment_types(self, tmp_path):
        """Message with photo + document yields two attachments."""
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {"file_path": "file.dat"},
        })
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"data"
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        atts = await ch._extract_attachments({
            "photo": [{"file_id": "p1", "file_size": 10}],
            "document": {"file_id": "d1", "file_size": 20, "mime_type": "text/plain", "file_name": "f.txt"},
        })

        assert len(atts) == 2
        types = {a.content_type for a in atts}
        assert "image/jpeg" in types
        assert "text/plain" in types

    @pytest.mark.asyncio
    async def test_failed_download_excluded(self, tmp_path):
        """If download fails, that attachment is excluded, not the whole list."""
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.side_effect = httpx.ConnectError("fail")
        ch._client = mock_client

        atts = await ch._extract_attachments({
            "photo": [{"file_id": "p1", "file_size": 10}],
        })
        assert atts == []

    @pytest.mark.asyncio
    async def test_voice_default_mime(self, tmp_path):
        """Voice without mime_type defaults to audio/ogg."""
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {"file_path": "voice/v.ogg"},
        })
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"ogg"
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        atts = await ch._extract_attachments({
            "voice": {"file_id": "v1", "file_size": 10},
        })
        assert len(atts) == 1
        assert atts[0].content_type == "audio/ogg"
        assert atts[0].is_voice is True

    @pytest.mark.asyncio
    async def test_document_default_mime(self, tmp_path):
        """Document without mime_type defaults to application/octet-stream."""
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {"file_path": "docs/f"},
        })
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"data"
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        atts = await ch._extract_attachments({
            "document": {"file_id": "d1", "file_size": 10},
        })
        assert len(atts) == 1
        assert atts[0].content_type == "application/octet-stream"

    @pytest.mark.asyncio
    async def test_video_default_mime(self, tmp_path):
        """Video without mime_type defaults to video/mp4."""
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {"file_path": "vids/v"},
        })
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"data"
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        atts = await ch._extract_attachments({
            "video": {"file_id": "v1", "file_size": 10},
        })
        assert len(atts) == 1
        assert atts[0].content_type == "video/mp4"

    @pytest.mark.asyncio
    async def test_audio_default_mime(self, tmp_path):
        """Audio without mime_type defaults to audio/mpeg."""
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {"file_path": "audio/a"},
        })
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"data"
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        atts = await ch._extract_attachments({
            "audio": {"file_id": "a1", "file_size": 10},
        })
        assert len(atts) == 1
        assert atts[0].content_type == "audio/mpeg"
        assert atts[0].is_voice is False


# ─── MIME Guessing (Complete) ────────────────────────────────────


class TestGuessMimeComplete:
    """Cover all 11 extensions in the _guess_mime dict."""
    def test_jpg(self):
        assert _guess_mime(Path("f.jpg")) == "image/jpeg"

    def test_jpeg(self):
        assert _guess_mime(Path("f.jpeg")) == "image/jpeg"

    def test_png(self):
        assert _guess_mime(Path("f.png")) == "image/png"

    def test_gif(self):
        assert _guess_mime(Path("f.gif")) == "image/gif"

    def test_webp(self):
        assert _guess_mime(Path("f.webp")) == "image/webp"

    def test_mp4(self):
        assert _guess_mime(Path("f.mp4")) == "video/mp4"

    def test_ogg(self):
        assert _guess_mime(Path("f.ogg")) == "audio/ogg"

    def test_mp3(self):
        assert _guess_mime(Path("f.mp3")) == "audio/mpeg"

    def test_m4a(self):
        assert _guess_mime(Path("f.m4a")) == "audio/mp4"

    def test_pdf(self):
        assert _guess_mime(Path("f.pdf")) == "application/pdf"

    def test_txt(self):
        assert _guess_mime(Path("f.txt")) == "text/plain"

    def test_unknown_fallback(self):
        assert _guess_mime(Path("f.xyz")) == "application/octet-stream"

    def test_suffix_lowered(self):
        assert _guess_mime(Path("F.PNG")) == "image/png"


# ─── Send With Attachments ───────────────────────────────────────


class TestSendAttachments:
    @pytest.mark.asyncio
    async def test_send_audio_attachment_calls_send_voice(self, tmp_path):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        audio_file = tmp_path / "voice.ogg"
        audio_file.write_bytes(b"audiodata")

        await ch.send("Nicolas", "", attachments=[str(audio_file)])

        call_url = mock_client.post.call_args[0][0]
        assert "sendVoice" in call_url
        kwargs = mock_client.post.call_args[1]
        assert "files" in kwargs
        assert "voice" in kwargs["files"]

    @pytest.mark.asyncio
    async def test_send_image_attachment_calls_send_photo(self, tmp_path):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        img_file = tmp_path / "pic.jpg"
        img_file.write_bytes(b"imgdata")

        await ch.send("Nicolas", "", attachments=[str(img_file)])

        call_url = mock_client.post.call_args[0][0]
        assert "sendPhoto" in call_url

    @pytest.mark.asyncio
    async def test_send_image_with_caption(self, tmp_path):
        """Single image attachment with text sends text as caption."""
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        img_file = tmp_path / "pic.jpg"
        img_file.write_bytes(b"imgdata")

        await ch.send("Nicolas", "look at this", attachments=[str(img_file)])

        kwargs = mock_client.post.call_args[1]
        assert kwargs["data"]["caption"] == "look at this"

    @pytest.mark.asyncio
    async def test_send_document_attachment(self, tmp_path):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        doc_file = tmp_path / "data.csv"
        doc_file.write_bytes(b"csvdata")

        await ch.send("Nicolas", "", attachments=[str(doc_file)])

        call_url = mock_client.post.call_args[0][0]
        assert "sendDocument" in call_url

    @pytest.mark.asyncio
    async def test_send_missing_attachment_skipped(self, tmp_path):
        """Non-existent attachment file is silently skipped."""
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        ch._client = mock_client

        await ch.send("Nicolas", "", attachments=["/nonexistent/file.jpg"])

        mock_client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_multiple_attachments_sends_text_separately(self, tmp_path):
        """With >1 attachment and text, text is sent as separate sendMessage."""
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        f1 = tmp_path / "a.ogg"
        f1.write_bytes(b"a")
        f2 = tmp_path / "b.ogg"
        f2.write_bytes(b"b")

        await ch.send("Nicolas", "text here", attachments=[str(f1), str(f2)])

        urls = [c[0][0] for c in mock_client.post.call_args_list]
        assert any("sendVoice" in u for u in urls)
        assert any("sendMessage" in u for u in urls)

    @pytest.mark.asyncio
    async def test_send_mp3_routed_as_voice(self, tmp_path):
        """mp3 files should be sent via sendVoice."""
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        f = tmp_path / "song.mp3"
        f.write_bytes(b"mp3data")

        await ch.send("Nicolas", "", attachments=[str(f)])

        call_url = mock_client.post.call_args[0][0]
        assert "sendVoice" in call_url


# ─── Download File (Stronger) ────────────────────────────────────


class TestDownloadFileStrong:
    @pytest.mark.asyncio
    async def test_download_calls_getFile_with_file_id(self, tmp_path):
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {"file_path": "f/test.bin"},
        })
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"data"
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        await ch._download_file("my_file_id", content_type="application/octet-stream")

        post_kwargs = mock_client.post.call_args[1]
        assert post_kwargs["json"]["file_id"] == "my_file_id"

    @pytest.mark.asyncio
    async def test_download_url_includes_token_and_path(self, tmp_path):
        ch = _make_channel(token="MYTOKEN", download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {"file_path": "photos/pic.jpg"},
        })
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"data"
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        await ch._download_file("fid")

        get_url = mock_client.get.call_args[0][0]
        assert get_url == "https://api.telegram.org/file/botMYTOKEN/photos/pic.jpg"

    @pytest.mark.asyncio
    async def test_download_no_file_path_returns_none(self, tmp_path):
        """When getFile returns no file_path, return None."""
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {},
        })
        ch._client = mock_client

        att = await ch._download_file("fid")
        assert att is None

    @pytest.mark.asyncio
    async def test_download_uses_filename_param(self, tmp_path):
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {"file_path": "docs/internal.bin"},
        })
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"data"
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        att = await ch._download_file("fid", filename="report.pdf")
        assert att.filename == "report.pdf"

    @pytest.mark.asyncio
    async def test_download_falls_back_to_path_name(self, tmp_path):
        """When no filename param, extract from file_path."""
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {"file_path": "photos/pic_42.jpg"},
        })
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"data"
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        att = await ch._download_file("fid")
        assert att.filename == "pic_42.jpg"

    @pytest.mark.asyncio
    async def test_download_size_fallback_to_content_len(self, tmp_path):
        """When size=0, use len(response.content)."""
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {"file_path": "f/f.bin"},
        })
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"1234567890"
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        att = await ch._download_file("fid", size=0)
        assert att.size == 10

    @pytest.mark.asyncio
    async def test_download_local_path_in_download_dir(self, tmp_path):
        """Downloaded file is saved inside the channel's download_dir."""
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {"file_path": "f/test.bin"},
        })
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"data"
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        att = await ch._download_file("fid")
        assert Path(att.local_path).parent == tmp_path


# ─── Parse Message (Stronger) ────────────────────────────────────


class TestParseMessageStrong:
    @pytest.mark.asyncio
    async def test_attachments_included_in_message(self, tmp_path):
        """Parsed message includes attachment objects."""
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {"file_path": "f/p.jpg"},
        })
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"jpgdata"
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        msg = await ch._parse_message({
            "from": {"id": 111}, "chat": {"id": 111},
            "message_id": 1, "text": "look",
            "photo": [{"file_id": "p1", "file_size": 10}],
        })
        assert msg is not None
        assert msg.attachments is not None
        assert len(msg.attachments) == 1
        assert msg.attachments[0].content_type == "image/jpeg"

    @pytest.mark.asyncio
    async def test_attachment_only_message_not_skipped(self, tmp_path):
        """Message with attachment but no text is still returned."""
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {"file_path": "f/p.jpg"},
        })
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"data"
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        msg = await ch._parse_message({
            "from": {"id": 111}, "chat": {"id": 111},
            "message_id": 1,
            "photo": [{"file_id": "p1", "file_size": 10}],
        })
        assert msg is not None
        assert msg.text == ""
        assert msg.attachments is not None

    @pytest.mark.asyncio
    async def test_no_attachments_field_is_none(self):
        """Message with text but no attachments has attachments=None."""
        ch = _make_channel()
        msg = await ch._parse_message({
            "from": {"id": 111}, "chat": {"id": 111},
            "message_id": 1, "text": "just text",
        })
        assert msg is not None
        assert msg.attachments is None

    @pytest.mark.asyncio
    async def test_chat_id_extracted(self):
        ch = _make_channel()
        msg = await ch._parse_message({
            "from": {"id": 111}, "chat": {"id": 555},
            "message_id": 7, "text": "hi",
        })
        assert msg is not None

    @pytest.mark.asyncio
    async def test_message_returns_inbound_message_type(self):
        ch = _make_channel()
        msg = await ch._parse_message({
            "from": {"id": 111}, "chat": {"id": 111},
            "message_id": 1, "text": "test",
        })
        assert isinstance(msg, InboundMessage)

    @pytest.mark.asyncio
    async def test_photo_falls_back_to_smaller_variant(self, tmp_path):
        """If largest photo variant fails, smaller variants are tried."""
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False

        # getFile calls: first fails (large), second succeeds (small)
        mock_client.post.side_effect = [
            # Large variant: getFile fails
            _mock_response({"ok": False, "description": "Bad Request: file is too big"}),
            # Small variant: getFile succeeds
            _mock_response({"ok": True, "result": {"file_path": "photos/small.jpg"}}),
        ]
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"smalljpeg"
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        atts = await ch._extract_attachments({
            "photo": [
                {"file_id": "small", "file_size": 100},
                {"file_id": "large", "file_size": 50000},
            ],
        })
        assert len(atts) == 1
        assert atts[0].content_type == "image/jpeg"

    @pytest.mark.asyncio
    async def test_document_falls_back_to_thumbnail(self, tmp_path):
        """Image document too large falls back to thumbnail download."""
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False

        # First getFile (full doc) fails, second getFile (thumbnail) succeeds
        mock_client.post.side_effect = [
            _mock_response({"ok": False, "description": "Bad Request: file is too big"}),
            _mock_response({"ok": True, "result": {"file_path": "thumbs/t.jpg"}}),
        ]
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"thumbdata"
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        atts = await ch._extract_attachments({
            "document": {
                "file_id": "d1", "file_size": 30_000_000,
                "mime_type": "image/png", "file_name": "huge.png",
                "thumbnail": {"file_id": "t1", "file_size": 5000},
            },
        })
        assert len(atts) == 1
        assert atts[0].content_type == "image/jpeg"

    @pytest.mark.asyncio
    async def test_document_thumbnail_not_tried_for_non_images(self, tmp_path):
        """Non-image documents don't attempt thumbnail fallback."""
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.side_effect = httpx.ConnectError("fail")
        ch._client = mock_client

        atts = await ch._extract_attachments({
            "document": {
                "file_id": "d1", "file_size": 30_000_000,
                "mime_type": "application/pdf", "file_name": "big.pdf",
                "thumbnail": {"file_id": "t1", "file_size": 5000},
            },
        })
        assert atts == []


# ─── Send Typing (Stronger) ─────────────────────────────────────


class TestSendTypingStrong:
    @pytest.mark.asyncio
    async def test_typing_url_exact(self):
        ch = _make_channel(token="tok")
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": True})
        ch._client = mock_client

        await ch.send_typing("Nicolas")

        url = mock_client.post.call_args[0][0]
        assert url == "https://api.telegram.org/bottok/sendChatAction"

    @pytest.mark.asyncio
    async def test_typing_action_value_exact(self):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": True})
        ch._client = mock_client

        await ch.send_typing("Nicolas")

        kwargs = mock_client.post.call_args[1]["json"]
        assert kwargs["action"] == "typing"


# ─── Chunk Text (Stronger) ──────────────────────────────────────


class TestChunkTextStrong:
    def test_exact_boundary_not_split(self):
        """Text exactly at chunk_limit stays as one chunk."""
        ch = _make_channel(chunk_limit=10)
        assert ch._chunk_text("a" * 10) == ["a" * 10]

    def test_one_over_boundary_splits(self):
        ch = _make_channel(chunk_limit=10)
        result = ch._chunk_text("a" * 11)
        assert len(result) == 2
        assert result[0] == "a" * 10
        assert result[1] == "a"

    def test_newline_split_exact_content(self):
        ch = _make_channel(chunk_limit=7)
        result = ch._chunk_text("aaa\nbbb\nccc")
        # "aaa\nbbb" = 7 chars, fits in one chunk. "ccc" in second.
        assert result == ["aaa\nbbb", "ccc"]

    def test_empty_text_returns_single_chunk(self):
        ch = _make_channel(chunk_limit=10)
        assert ch._chunk_text("") == [""]


# ─── Poll Loop (Stronger) ───────────────────────────────────────


class TestPollLoopStrong:
    @pytest.mark.asyncio
    async def test_poll_passes_correct_params(self):
        ch = _make_channel()
        ch._offset = 42
        mock_client = AsyncMock()
        mock_client.is_closed = False

        async def mock_post(url, **kwargs):
            # Capture params then stop the loop
            raise StopAsyncIteration()

        mock_client.post = mock_post
        ch._client = mock_client

        # Patch _api directly to capture the call
        calls = []

        async def capturing_api(method, **params):
            calls.append((method, params))
            return []  # empty updates

        ch._api = capturing_api

        # Run one iteration then break
        call_count = 0

        async def limited_api(method, **params):
            nonlocal call_count
            call_count += 1
            calls.append((method, params))
            if call_count > 1:
                raise httpx.ConnectError("stop")
            return []

        ch._api = limited_api

        with pytest.raises(httpx.ConnectError):
            async for _ in ch._poll_loop():
                pass

        assert calls[0][0] == "getUpdates"
        assert calls[0][1]["offset"] == 42
        assert calls[0][1]["timeout"] == 30
        assert calls[0][1]["allowed_updates"] == ["message"]


# ─── Remediation Round 2 ──────────────────────────────────────
# Kill behavioral survivors identified via mutmut analysis.
# Patterns fixed: exact URL == assertions, chat_id forwarding,
# _files dict key names, file open mode, suffix routing,
# continue-vs-break, receive() coverage, mime_type key mutations,
# send_reaction integer division.


class TestSendVoiceExact:
    """Kill _send_voice survivors: method name, chat_id, file key, open mode."""

    @pytest.mark.asyncio
    async def test_send_voice_exact_url(self, tmp_path):
        ch = _make_channel(token="tok")
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        f = tmp_path / "voice.ogg"
        f.write_bytes(b"oggdata")

        await ch._send_voice(111, f)

        url = mock_client.post.call_args[0][0]
        assert url == "https://api.telegram.org/bottok/sendVoice"

    @pytest.mark.asyncio
    async def test_send_voice_chat_id_forwarded(self, tmp_path):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        f = tmp_path / "voice.ogg"
        f.write_bytes(b"oggdata")

        await ch._send_voice(777, f)

        kwargs = mock_client.post.call_args[1]
        assert kwargs["data"]["chat_id"] == 777

    @pytest.mark.asyncio
    async def test_send_voice_files_key_is_voice(self, tmp_path):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        f = tmp_path / "voice.ogg"
        f.write_bytes(b"oggdata")

        await ch._send_voice(111, f)

        kwargs = mock_client.post.call_args[1]
        files = kwargs["files"]
        assert "voice" in files
        _, fh, mime = files["voice"]
        assert mime == "audio/ogg"

    @pytest.mark.asyncio
    async def test_send_voice_opens_binary(self, tmp_path):
        """File must be opened in binary mode ('rb'), not text mode."""
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        f = tmp_path / "voice.ogg"
        f.write_bytes(b"\x00\x01\x02binary")

        await ch._send_voice(111, f)

        kwargs = mock_client.post.call_args[1]
        _, fh, _ = kwargs["files"]["voice"]
        assert fh.mode == "rb"


class TestSendPhotoExact:
    """Kill _send_photo survivors: method name, chat_id key, file key, open mode."""

    @pytest.mark.asyncio
    async def test_send_photo_exact_url(self, tmp_path):
        ch = _make_channel(token="tok")
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        f = tmp_path / "pic.jpg"
        f.write_bytes(b"jpg")

        await ch._send_photo(111, f)

        url = mock_client.post.call_args[0][0]
        assert url == "https://api.telegram.org/bottok/sendPhoto"

    @pytest.mark.asyncio
    async def test_send_photo_chat_id_key_exact(self, tmp_path):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        f = tmp_path / "pic.jpg"
        f.write_bytes(b"jpg")

        await ch._send_photo(777, f)

        kwargs = mock_client.post.call_args[1]
        assert kwargs["data"]["chat_id"] == 777

    @pytest.mark.asyncio
    async def test_send_photo_files_key_is_photo(self, tmp_path):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        f = tmp_path / "pic.jpg"
        f.write_bytes(b"jpg")

        await ch._send_photo(111, f)

        kwargs = mock_client.post.call_args[1]
        files = kwargs["files"]
        assert "photo" in files
        _, fh, mime = files["photo"]
        assert mime == "image/jpeg"

    @pytest.mark.asyncio
    async def test_send_photo_opens_binary(self, tmp_path):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        f = tmp_path / "pic.jpg"
        f.write_bytes(b"\xff\xd8binary")

        await ch._send_photo(111, f)

        kwargs = mock_client.post.call_args[1]
        _, fh, _ = kwargs["files"]["photo"]
        assert fh.mode == "rb"

    @pytest.mark.asyncio
    async def test_send_photo_no_caption_default(self, tmp_path):
        """When no caption, params should not contain 'caption' key."""
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        f = tmp_path / "pic.jpg"
        f.write_bytes(b"jpg")

        await ch._send_photo(111, f)

        kwargs = mock_client.post.call_args[1]
        assert "caption" not in kwargs["data"]


class TestSendDocumentExact:
    """Kill _send_document survivors: method name, chat_id, file key, open mode, _files presence."""

    @pytest.mark.asyncio
    async def test_send_document_exact_url(self, tmp_path):
        ch = _make_channel(token="tok")
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        f = tmp_path / "data.csv"
        f.write_bytes(b"csv")

        await ch._send_document(111, f)

        url = mock_client.post.call_args[0][0]
        assert url == "https://api.telegram.org/bottok/sendDocument"

    @pytest.mark.asyncio
    async def test_send_document_chat_id_forwarded(self, tmp_path):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        f = tmp_path / "data.csv"
        f.write_bytes(b"csv")

        await ch._send_document(777, f)

        kwargs = mock_client.post.call_args[1]
        assert kwargs["data"]["chat_id"] == 777

    @pytest.mark.asyncio
    async def test_send_document_files_key_is_document(self, tmp_path):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        f = tmp_path / "data.csv"
        f.write_bytes(b"csv")

        await ch._send_document(111, f)

        kwargs = mock_client.post.call_args[1]
        files = kwargs["files"]
        assert "document" in files

    @pytest.mark.asyncio
    async def test_send_document_files_not_none(self, tmp_path):
        """_files must be a dict, not None — otherwise _api routes as JSON."""
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        f = tmp_path / "data.csv"
        f.write_bytes(b"csv")

        await ch._send_document(111, f)

        kwargs = mock_client.post.call_args[1]
        assert "files" in kwargs
        assert kwargs["files"] is not None

    @pytest.mark.asyncio
    async def test_send_document_opens_binary(self, tmp_path):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        f = tmp_path / "data.bin"
        f.write_bytes(b"\x00\x01\x02")

        await ch._send_document(111, f)

        kwargs = mock_client.post.call_args[1]
        _, fh, _ = kwargs["files"]["document"]
        assert fh.mode == "rb"


class TestSendRoutingExact:
    """Kill send() survivors: chat_id forwarding, continue-vs-break,
    or-vs-and routing, suffix mutations, method string mutations."""

    @pytest.mark.asyncio
    async def test_send_text_exact_url(self, tmp_path):
        """sendMessage URL must be exact, not substring match."""
        ch = _make_channel(token="tok")
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        await ch.send("Nicolas", "hello")

        url = mock_client.post.call_args[0][0]
        assert url == "https://api.telegram.org/bottok/sendMessage"

    @pytest.mark.asyncio
    async def test_send_text_exact_payload(self):
        """chat_id and text must be exactly correct in API call."""
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        await ch.send("Nicolas", "hello")

        kwargs = mock_client.post.call_args[1]
        assert kwargs["json"]["chat_id"] == 111
        assert kwargs["json"]["text"] == "hello"

    @pytest.mark.asyncio
    async def test_missing_attachment_continue_not_break(self, tmp_path):
        """Missing first attachment must not prevent sending second one."""
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        real_file = tmp_path / "real.ogg"
        real_file.write_bytes(b"audio")

        await ch.send("Nicolas", "", attachments=["/nonexistent.ogg", str(real_file)])

        # The real file should still be sent
        assert mock_client.post.call_count >= 1

    @pytest.mark.asyncio
    async def test_m4a_routed_as_voice(self, tmp_path):
        """m4a files match suffix list and should route to sendVoice."""
        ch = _make_channel(token="tok")
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        f = tmp_path / "audio.m4a"
        f.write_bytes(b"m4adata")

        await ch.send("Nicolas", "", attachments=[str(f)])

        url = mock_client.post.call_args[0][0]
        assert url == "https://api.telegram.org/bottok/sendVoice"

    @pytest.mark.asyncio
    async def test_send_voice_receives_correct_chat_id(self, tmp_path):
        """chat_id from _resolve_target must reach _send_voice, not None."""
        ch = _make_channel(token="tok")
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        f = tmp_path / "voice.ogg"
        f.write_bytes(b"data")

        await ch.send("Nicolas", "", attachments=[str(f)])

        kwargs = mock_client.post.call_args[1]
        assert kwargs["data"]["chat_id"] == 111

    @pytest.mark.asyncio
    async def test_send_photo_receives_correct_chat_id(self, tmp_path):
        ch = _make_channel(token="tok")
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        f = tmp_path / "pic.jpg"
        f.write_bytes(b"jpg")

        await ch.send("Nicolas", "", attachments=[str(f)])

        kwargs = mock_client.post.call_args[1]
        assert kwargs["data"]["chat_id"] == 111

    @pytest.mark.asyncio
    async def test_send_document_receives_correct_chat_id(self, tmp_path):
        ch = _make_channel(token="tok")
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        f = tmp_path / "data.csv"
        f.write_bytes(b"csv")

        await ch.send("Nicolas", "", attachments=[str(f)])

        kwargs = mock_client.post.call_args[1]
        assert kwargs["data"]["chat_id"] == 111

    @pytest.mark.asyncio
    async def test_multi_attachment_text_sent_separately_exact(self, tmp_path):
        """With >1 attachment and text, text sendMessage must have correct chat_id and text."""
        ch = _make_channel(token="tok")
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        f1 = tmp_path / "a.ogg"
        f1.write_bytes(b"a")
        f2 = tmp_path / "b.ogg"
        f2.write_bytes(b"b")

        await ch.send("Nicolas", "caption text", attachments=[str(f1), str(f2)])

        # Find the sendMessage call
        send_msg_calls = [c for c in mock_client.post.call_args_list
                         if "sendMessage" in c[0][0]]
        assert len(send_msg_calls) >= 1
        msg_kwargs = send_msg_calls[0][1]
        assert msg_kwargs["json"]["chat_id"] == 111
        assert msg_kwargs["json"]["text"] == "caption text"

    @pytest.mark.asyncio
    async def test_single_image_caption_not_empty_fallback(self, tmp_path):
        """Single image with text: caption must be the text, not 'XXXX' or other default."""
        ch = _make_channel(token="tok")
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        f = tmp_path / "pic.jpg"
        f.write_bytes(b"jpg")

        await ch.send("Nicolas", "my caption", attachments=[str(f)])

        kwargs = mock_client.post.call_args[1]
        assert kwargs["data"]["caption"] == "my caption"

    @pytest.mark.asyncio
    async def test_multi_attachment_image_no_caption(self, tmp_path):
        """With >1 attachment, image should NOT get text as caption (empty string)."""
        ch = _make_channel(token="tok")
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        f1 = tmp_path / "pic.jpg"
        f1.write_bytes(b"jpg")
        f2 = tmp_path / "doc.pdf"
        f2.write_bytes(b"pdf")

        await ch.send("Nicolas", "some text", attachments=[str(f1), str(f2)])

        # Find sendPhoto call
        photo_calls = [c for c in mock_client.post.call_args_list
                      if "sendPhoto" in c[0][0]]
        assert len(photo_calls) == 1
        photo_kwargs = photo_calls[0][1]
        # Caption should NOT be in data (empty string → not set)
        assert "caption" not in photo_kwargs["data"]


class TestSendReactionExact:
    """Kill send_reaction survivors: integer division, boundary, method name."""

    @pytest.mark.asyncio
    async def test_reaction_uses_integer_division(self):
        """ts // 1000 must produce int, not float from ts / 1000."""
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": True})
        ch._client = mock_client

        await ch.send_reaction("Nicolas", "👍", 42000)

        call_json = mock_client.post.call_args[1]["json"]
        assert call_json["message_id"] == 42
        assert isinstance(call_json["message_id"], int)

    @pytest.mark.asyncio
    async def test_reaction_message_id_1_is_valid(self):
        """message_id=1 (ts=1000) must be valid, not rejected by <= 1 check."""
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": True})
        ch._client = mock_client

        # ts=1000 → message_id = 1000 // 1000 = 1 → valid
        await ch.send_reaction("Nicolas", "👍", 1000)

        call_json = mock_client.post.call_args[1]["json"]
        assert call_json["message_id"] == 1

    @pytest.mark.asyncio
    async def test_reaction_exact_url(self):
        ch = _make_channel(token="tok")
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": True})
        ch._client = mock_client

        await ch.send_reaction("Nicolas", "👍", 42000)

        url = mock_client.post.call_args[0][0]
        assert url == "https://api.telegram.org/bottok/setMessageReaction"


class TestDownloadFileExact:
    """Kill _download_file survivors: getFile method name."""

    @pytest.mark.asyncio
    async def test_download_calls_getFile_exact_method(self, tmp_path):
        """The getFile API method name must be exact."""
        ch = _make_channel(token="tok", download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {"file_path": "f/test.bin"},
        })
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"data"
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        await ch._download_file("fid")

        url = mock_client.post.call_args[0][0]
        assert url == "https://api.telegram.org/bottok/getFile"


class TestExtractAttachmentsMimeKey:
    """Kill _extract_attachments mime_type key name mutations.
    Tests must provide a non-default mime_type so the key lookup matters."""

    @pytest.mark.asyncio
    async def test_voice_custom_mime_type(self, tmp_path):
        """Voice with non-default mime_type must use the provided value."""
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {"file_path": "voice/v.opus"},
        })
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"opus"
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        atts = await ch._extract_attachments({
            "voice": {"file_id": "v1", "file_size": 100, "mime_type": "audio/opus"},
        })

        assert len(atts) == 1
        assert atts[0].content_type == "audio/opus"

    @pytest.mark.asyncio
    async def test_video_custom_mime_type(self, tmp_path):
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {"file_path": "vids/v.webm"},
        })
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"webm"
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        atts = await ch._extract_attachments({
            "video": {"file_id": "v1", "file_size": 500, "mime_type": "video/webm"},
        })

        assert len(atts) == 1
        assert atts[0].content_type == "video/webm"

    @pytest.mark.asyncio
    async def test_document_custom_mime_type(self, tmp_path):
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {"file_path": "docs/f.json"},
        })
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"{}"
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        atts = await ch._extract_attachments({
            "document": {"file_id": "d1", "file_size": 50,
                         "mime_type": "application/json", "file_name": "data.json"},
        })

        assert len(atts) == 1
        assert atts[0].content_type == "application/json"

    @pytest.mark.asyncio
    async def test_audio_custom_mime_type(self, tmp_path):
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {"file_path": "audio/a.flac"},
        })
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"flac"
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        atts = await ch._extract_attachments({
            "audio": {"file_id": "a1", "file_size": 200,
                      "mime_type": "audio/flac", "file_name": "song.flac"},
        })

        assert len(atts) == 1
        assert atts[0].content_type == "audio/flac"

    @pytest.mark.asyncio
    async def test_document_filename_forwarded(self, tmp_path):
        """Document file_name key must be read correctly from the dict."""
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {"file_path": "docs/internal.bin"},
        })
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"data"
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        atts = await ch._extract_attachments({
            "document": {"file_id": "d1", "file_size": 10,
                         "mime_type": "text/plain", "file_name": "readme.txt"},
        })

        assert len(atts) == 1
        assert atts[0].filename == "readme.txt"

    @pytest.mark.asyncio
    async def test_audio_filename_forwarded(self, tmp_path):
        ch = _make_channel(download_dir=str(tmp_path))
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {"file_path": "audio/a"},
        })
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.raise_for_status = MagicMock()
        download_resp.content = b"data"
        mock_client.get.return_value = download_resp
        ch._client = mock_client

        atts = await ch._extract_attachments({
            "audio": {"file_id": "a1", "file_size": 10,
                      "mime_type": "audio/flac", "file_name": "track.flac"},
        })

        assert len(atts) == 1
        assert atts[0].filename == "track.flac"


class TestSendExactUrls:
    """Strengthen existing send() tests to use == instead of `in`."""

    @pytest.mark.asyncio
    async def test_send_attachment_voice_exact_url(self, tmp_path):
        ch = _make_channel(token="tok")
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        f = tmp_path / "voice.ogg"
        f.write_bytes(b"ogg")

        await ch.send("Nicolas", "", attachments=[str(f)])

        url = mock_client.post.call_args[0][0]
        assert url == "https://api.telegram.org/bottok/sendVoice"

    @pytest.mark.asyncio
    async def test_send_attachment_photo_exact_url(self, tmp_path):
        ch = _make_channel(token="tok")
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        f = tmp_path / "pic.jpg"
        f.write_bytes(b"jpg")

        await ch.send("Nicolas", "", attachments=[str(f)])

        url = mock_client.post.call_args[0][0]
        assert url == "https://api.telegram.org/bottok/sendPhoto"

    @pytest.mark.asyncio
    async def test_send_attachment_document_exact_url(self, tmp_path):
        ch = _make_channel(token="tok")
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({"ok": True, "result": {}})
        ch._client = mock_client

        f = tmp_path / "data.csv"
        f.write_bytes(b"csv")

        await ch.send("Nicolas", "", attachments=[str(f)])

        url = mock_client.post.call_args[0][0]
        assert url == "https://api.telegram.org/bottok/sendDocument"


class TestConnectExactUrls:
    """Strengthen connect test to use == not `in`."""

    @pytest.mark.asyncio
    async def test_connect_uses_getMe_exact(self):
        ch = _make_channel(token="tok")
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "ok": True, "result": {"id": 1, "username": "bot"},
        })
        ch._client = mock_client

        await ch.connect()

        url = mock_client.post.call_args[0][0]
        assert url == "https://api.telegram.org/bottok/getMe"


class TestPollLoopOffset:
    """Kill _poll_loop offset comparison mutation (>= → >)."""

    @pytest.mark.asyncio
    async def test_offset_updated_when_equal(self):
        """When update_id == offset (both 0), offset must still advance."""
        ch = _make_channel()
        ch._offset = 0

        call_count = 0
        calls = []

        async def mock_api(method, **params):
            nonlocal call_count
            call_count += 1
            calls.append((method, params))
            if call_count == 1:
                return [{"update_id": 0, "message": {
                    "from": {"id": 111}, "chat": {"id": 111},
                    "message_id": 1, "text": "hi",
                }}]
            raise httpx.ConnectError("stop")

        ch._api = mock_api

        with pytest.raises(httpx.ConnectError):
            async for _ in ch._poll_loop():
                pass

        assert ch._offset == 1
        # Second call should pass offset=1
        assert calls[1][1]["offset"] == 1


class TestReceive:
    """Cover receive() — the reconnect wrapper around _poll_loop().
    28 previously untested mutants."""

    @pytest.mark.asyncio
    async def test_receive_yields_messages_from_poll_loop(self):
        """Messages from _poll_loop are yielded through receive()."""
        ch = _make_channel()
        msg = InboundMessage(text="hello", sender="Nicolas", timestamp=1.0,
                            source="telegram")

        call_count = 0

        async def mock_poll_loop():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield msg
                return  # Normal exit triggers reconnect
            # Second call: CancelledError stops receive() cleanly (it returns)
            raise asyncio.CancelledError()

        ch._poll_loop = mock_poll_loop
        messages = []

        # receive() catches CancelledError and returns — no raise
        async for m in ch.receive():
            messages.append(m)

        assert len(messages) == 1
        assert messages[0].text == "hello"

    @pytest.mark.asyncio
    async def test_receive_reconnects_on_error(self):
        """After _poll_loop raises, receive() sleeps and retries."""
        import asyncio as aio
        ch = _make_channel()

        call_count = 0
        sleep_args = []

        async def mock_poll_loop():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise ConnectionError("disconnected")
            raise aio.CancelledError()

            # Need to yield to be async generator
            yield  # pragma: no cover

        ch._poll_loop = mock_poll_loop

        async def capture_sleep(duration):
            sleep_args.append(duration)

        with patch("asyncio.sleep", capture_sleep):
            try:
                async for _ in ch.receive():
                    pass
            except aio.CancelledError:
                pass

        # Should have reconnected at least once
        assert call_count >= 2
        assert len(sleep_args) >= 1

    @pytest.mark.asyncio
    async def test_receive_backoff_increases(self):
        """Backoff should increase after consecutive failures."""
        import asyncio as aio
        ch = _make_channel()

        call_count = 0
        sleep_args = []

        async def mock_poll_loop():
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                raise ConnectionError("fail")
            raise aio.CancelledError()
            yield  # pragma: no cover

        ch._poll_loop = mock_poll_loop

        async def capture_sleep(duration):
            sleep_args.append(duration)

        with patch("asyncio.sleep", capture_sleep), \
             patch("random.random", return_value=0.5):
            try:
                async for _ in ch.receive():
                    pass
            except aio.CancelledError:
                pass

        # 3 failures → 3 sleeps with increasing backoff
        assert len(sleep_args) == 3
        # Each sleep should be >= the previous (backoff)
        for i in range(1, len(sleep_args)):
            assert sleep_args[i] >= sleep_args[i - 1]

    @pytest.mark.asyncio
    async def test_receive_backoff_resets_on_message(self):
        """After successfully yielding a message, backoff resets."""
        import asyncio as aio
        ch = _make_channel()

        call_count = 0
        sleep_args = []

        async def mock_poll_loop():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("fail 1")
            if call_count == 2:
                raise ConnectionError("fail 2")
            if call_count == 3:
                yield InboundMessage(text="ok", sender="n", timestamp=1.0,
                                    source="telegram")
                return  # Successful exit after yield
            if call_count == 4:
                raise ConnectionError("fail after reset")
            raise aio.CancelledError()
            yield  # pragma: no cover

        ch._poll_loop = mock_poll_loop

        async def capture_sleep(duration):
            sleep_args.append(duration)

        with patch("asyncio.sleep", capture_sleep), \
             patch("random.random", return_value=0.5):
            messages = []
            try:
                async for m in ch.receive():
                    messages.append(m)
            except aio.CancelledError:
                pass

        assert len(messages) == 1
        # After successful message yield, the next failure's sleep should be
        # back to initial (small), not continued from the elevated backoff
        # sleep_args: [~1s, ~2s, ~1s(after reset), ~1s...]
        assert len(sleep_args) >= 3
        # After reset, the backoff should be close to initial
        assert sleep_args[2] < sleep_args[1]

    @pytest.mark.asyncio
    async def test_receive_cancelled_error_stops(self):
        """CancelledError in _poll_loop stops receive() cleanly."""
        import asyncio as aio
        ch = _make_channel()

        async def mock_poll_loop():
            raise aio.CancelledError()
            yield  # pragma: no cover

        ch._poll_loop = mock_poll_loop

        messages = []
        async for m in ch.receive():
            messages.append(m)

        assert messages == []

    @pytest.mark.asyncio
    async def test_receive_backoff_capped_at_max(self):
        """Backoff should not exceed _RECONNECT_MAX."""
        import asyncio as aio
        ch = _make_channel()

        call_count = 0
        sleep_args = []

        async def mock_poll_loop():
            nonlocal call_count
            call_count += 1
            if call_count <= 10:
                raise ConnectionError("fail")
            raise aio.CancelledError()
            yield  # pragma: no cover

        ch._poll_loop = mock_poll_loop

        async def capture_sleep(duration):
            sleep_args.append(duration)

        with patch("asyncio.sleep", capture_sleep), \
             patch("random.random", return_value=0.5):
            try:
                async for _ in ch.receive():
                    pass
            except aio.CancelledError:
                pass

        # After many failures, sleep should be capped around reconnect_max
        for s in sleep_args:
            # Max = 10.0, jitter = 20%, so max with jitter ≈ 12.0
            assert s <= ch._reconnect_max * (1 + ch._reconnect_jitter)


# ─── Disconnect Lifecycle ────────────────────────────────────────


class TestDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_closes_client(self):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        ch._client = mock_client

        await ch.disconnect()

        mock_client.aclose.assert_awaited_once()
        assert ch._client is None

    @pytest.mark.asyncio
    async def test_disconnect_idempotent(self):
        ch = _make_channel()
        # No client set
        await ch.disconnect()  # Should not raise
        await ch.disconnect()  # Still should not raise

    @pytest.mark.asyncio
    async def test_disconnect_cleans_download_dir(self, tmp_path):
        dl_dir = tmp_path / "downloads"
        dl_dir.mkdir()
        (dl_dir / "file1.jpg").write_bytes(b"fake")
        (dl_dir / "file2.ogg").write_bytes(b"audio")

        ch = _make_channel(download_dir=str(dl_dir))
        await ch.disconnect()

        remaining = list(dl_dir.iterdir())
        assert remaining == []

    @pytest.mark.asyncio
    async def test_disconnect_skips_closed_client(self):
        ch = _make_channel()
        mock_client = AsyncMock()
        mock_client.is_closed = True
        ch._client = mock_client

        await ch.disconnect()

        mock_client.aclose.assert_not_awaited()
