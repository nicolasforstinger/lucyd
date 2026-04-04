"""Comprehensive tests for channels/telegram.py.

Covers: pure functions, config loading, API/HTTP calls, attachment handling,
send helpers, message processing, and the poll loop.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import channels.telegram as tg

# ─── Globals snapshot / restore ──────────────────────────────────

_GLOBALS_KEYS = [
    "TOKEN", "DAEMON_URL", "API_BASE", "CHUNK_LIMIT", "POLL_TIMEOUT",
    "HTTP_TIMEOUT", "CONNECT_TIMEOUT", "RECONNECT_INITIAL", "RECONNECT_MAX",
    "RECONNECT_FACTOR", "RECONNECT_JITTER", "MEDIA_GROUP_DELAY",
    "MAX_ATTACHMENT_BYTES", "ID_TO_NAME", "ALLOW_FROM",
    "_client", "_bot_id", "_offset",
]


@pytest.fixture(autouse=True)
def _restore_globals() -> Any:
    """Save every module-level global before each test and restore after."""
    saved: dict[str, Any] = {}
    for k in _GLOBALS_KEYS:
        val = getattr(tg, k)
        # copy mutable containers so the snapshot is isolated
        if isinstance(val, dict):
            saved[k] = dict(val)
        elif isinstance(val, set):
            saved[k] = set(val)
        else:
            saved[k] = val
    yield
    for k, v in saved.items():
        setattr(tg, k, v)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. Pure functions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestExtractLinks:
    def test_extracts_single_markdown_link(self) -> None:
        text = "See [Google](https://google.com) for info"
        cleaned, links = tg.extract_links(text)
        assert links == [("Google", "https://google.com")]
        assert "Google [1]" in cleaned
        assert "(https://google.com)" not in cleaned

    def test_extracts_multiple_links_numbered_sequentially(self) -> None:
        text = "[A](https://a.com) and [B](http://b.com)"
        cleaned, links = tg.extract_links(text)
        assert links == [("A", "https://a.com"), ("B", "http://b.com")]
        assert "A [1]" in cleaned
        assert "B [2]" in cleaned

    def test_no_links_returns_unchanged_text(self) -> None:
        text = "Nothing to see here"
        cleaned, links = tg.extract_links(text)
        assert cleaned == text
        assert links == []


class TestBuildKeyboard:
    def test_empty_links_returns_none(self) -> None:
        assert tg.build_keyboard([]) is None

    def test_builds_rows_of_four(self) -> None:
        links = [(f"L{i}", f"https://x.com/{i}") for i in range(5)]
        kb = tg.build_keyboard(links)
        assert kb is not None
        rows = kb["inline_keyboard"]
        assert len(rows) == 2
        assert len(rows[0]) == 4
        assert len(rows[1]) == 1
        assert rows[0][0] == {"text": "1", "url": "https://x.com/0"}

    def test_single_link_single_row(self) -> None:
        kb = tg.build_keyboard([("Foo", "https://foo.com")])
        assert kb is not None
        assert len(kb["inline_keyboard"]) == 1
        assert kb["inline_keyboard"][0][0]["url"] == "https://foo.com"


class TestChunkText:
    def test_short_text_returned_as_single_chunk(self) -> None:
        tg.CHUNK_LIMIT = 100
        assert tg.chunk_text("hello") == ["hello"]

    def test_splits_at_newline_boundary(self) -> None:
        tg.CHUNK_LIMIT = 10
        text = "aaa\nbbb\ncccc"
        chunks = tg.chunk_text(text)
        assert all(len(c) <= 10 for c in chunks)
        assert "\n".join(chunks) == text

    def test_line_longer_than_limit_is_force_split(self) -> None:
        tg.CHUNK_LIMIT = 5
        text = "abcdefghij"  # 10 chars, limit 5
        chunks = tg.chunk_text(text)
        assert chunks == ["abcde", "fghij"]

    def test_exact_limit_stays_single_chunk(self) -> None:
        tg.CHUNK_LIMIT = 5
        assert tg.chunk_text("abcde") == ["abcde"]


class TestExtractQuote:
    def test_returns_none_when_no_reply(self) -> None:
        assert tg.extract_quote({"text": "hi"}) is None

    def test_prefers_native_telegram_quote(self) -> None:
        msg: dict[str, Any] = {
            "reply_to_message": {"text": "full reply"},
            "quote": {"text": "selected portion"},
        }
        assert tg.extract_quote(msg) == "selected portion"

    def test_falls_back_to_reply_text(self) -> None:
        msg: dict[str, Any] = {"reply_to_message": {"text": "full reply"}}
        assert tg.extract_quote(msg) == "full reply"

    def test_falls_back_to_caption(self) -> None:
        msg: dict[str, Any] = {"reply_to_message": {"caption": "caption text"}}
        assert tg.extract_quote(msg) == "caption text"

    def test_returns_none_when_reply_has_no_text_or_caption(self) -> None:
        msg: dict[str, Any] = {"reply_to_message": {}}
        assert tg.extract_quote(msg) is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. Config loading
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestLoadConfig:
    def test_loads_config_from_toml_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = tmp_path / "telegram.toml"
        cfg.write_text(
            '[telegram]\n'
            'token_env = "MY_TG_TOKEN"\n'
            'text_chunk_limit = 2000\n'
            'poll_timeout = 15\n'
            '\n'
            '[daemon]\n'
            'url = "http://daemon:9000"\n'
        )
        monkeypatch.setenv("LUCYD_TELEGRAM_CONFIG", str(cfg))
        monkeypatch.setenv("MY_TG_TOKEN", "tok123")
        tg.load_config()
        assert tg.TOKEN == "tok123"
        assert tg.DAEMON_URL == "http://daemon:9000"
        assert tg.CHUNK_LIMIT == 2000
        assert tg.POLL_TIMEOUT == 15

    def test_fallback_to_env_vars_when_no_config_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LUCYD_TELEGRAM_TOKEN", "env-tok")
        monkeypatch.setenv("LUCYD_URL", "http://env-daemon:8200")
        monkeypatch.setenv("LUCYD_TELEGRAM_CONFIG", "")
        # Ensure no config files found in default locations
        monkeypatch.chdir("/tmp")
        tg.load_config()
        assert tg.TOKEN == "env-tok"
        assert tg.DAEMON_URL == "http://env-daemon:8200"
        assert tg.API_BASE == "https://api.telegram.org/botenv-tok"

    def test_contacts_section_populates_id_to_name(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = tmp_path / "telegram.toml"
        cfg.write_text(
            '[telegram]\n'
            'token_env = "TG_TOK"\n'
            '\n'
            '[telegram.contacts]\n'
            'Alice = 111\n'
            'Bob = 222\n'
        )
        monkeypatch.setenv("LUCYD_TELEGRAM_CONFIG", str(cfg))
        monkeypatch.setenv("TG_TOK", "x")
        tg.ID_TO_NAME = {}
        tg.load_config()
        assert tg.ID_TO_NAME == {111: "Alice", 222: "Bob"}

    def test_allow_from_populates_set(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = tmp_path / "telegram.toml"
        cfg.write_text(
            '[telegram]\n'
            'token_env = "TG_TOK"\n'
            'allow_from = [111, 222]\n'
        )
        monkeypatch.setenv("LUCYD_TELEGRAM_CONFIG", str(cfg))
        monkeypatch.setenv("TG_TOK", "x")
        tg.ALLOW_FROM = set()
        tg.load_config()
        assert tg.ALLOW_FROM == {111, 222}

    def test_malformed_toml_falls_back_to_env_vars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Malformed TOML file logs warning and falls back to env vars."""
        cfg = tmp_path / "telegram.toml"
        cfg.write_text("this is not valid toml [[[")
        monkeypatch.setenv("LUCYD_TELEGRAM_CONFIG", str(cfg))
        monkeypatch.setenv("LUCYD_TELEGRAM_TOKEN", "fallback-tok")
        monkeypatch.setenv("LUCYD_URL", "http://fallback:8200")
        tg.load_config()
        assert tg.TOKEN == "fallback-tok"
        assert tg.DAEMON_URL == "http://fallback:8200"

    def test_daemon_token_env_sets_lucyd_http_token(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = tmp_path / "telegram.toml"
        cfg.write_text(
            '[telegram]\n'
            'token_env = "TG_TOK"\n'
            '\n'
            '[daemon]\n'
            'token_env = "MY_DAEMON_TOKEN"\n'
        )
        monkeypatch.setenv("LUCYD_TELEGRAM_CONFIG", str(cfg))
        monkeypatch.setenv("TG_TOK", "x")
        monkeypatch.setenv("MY_DAEMON_TOKEN", "secret-daemon-tok")
        monkeypatch.delenv("LUCYD_HTTP_TOKEN", raising=False)
        tg.load_config()
        assert os.environ.get("LUCYD_HTTP_TOKEN") == "secret-daemon-tok"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. API and HTTP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTgApi:
    @pytest.mark.asyncio
    async def test_successful_call_returns_result(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True, "result": {"id": 42}}
        mock_client.post.return_value = mock_resp

        tg.API_BASE = "https://api.telegram.org/botTOK"
        with patch.object(tg, "_get_client", return_value=mock_client):
            result = await tg.tg_api("getMe")
        assert result == {"id": 42}

    @pytest.mark.asyncio
    async def test_error_response_raises_runtime_error(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": False, "description": "Bad Request"}
        mock_resp.status_code = 400
        mock_client.post.return_value = mock_resp

        tg.API_BASE = "https://api.telegram.org/botTOK"
        with patch.object(tg, "_get_client", return_value=mock_client):
            with pytest.raises(RuntimeError, match="Bad Request"):
                await tg.tg_api("sendMessage", chat_id=1, text="hi")

    @pytest.mark.asyncio
    async def test_files_param_uses_data_form_post(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True, "result": {}}
        mock_client.post.return_value = mock_resp

        tg.API_BASE = "https://api.telegram.org/botTOK"
        with patch.object(tg, "_get_client", return_value=mock_client):
            await tg.tg_api("sendPhoto", chat_id=1, _files={"photo": ("f.jpg", b"data", "image/jpeg")})
        # When _files is provided, it should use data= not json=
        call_kwargs = mock_client.post.call_args
        assert "files" in call_kwargs.kwargs
        assert "data" in call_kwargs.kwargs


class TestDaemonAuthHeaders:
    def test_with_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LUCYD_HTTP_TOKEN", "secret123")
        assert tg._daemon_auth_headers() == {"Authorization": "Bearer secret123"}

    def test_without_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LUCYD_HTTP_TOKEN", raising=False)
        assert tg._daemon_auth_headers() == {}


class TestDownloadFile:
    @pytest.mark.asyncio
    async def test_successful_download(self, tmp_path: Path) -> None:
        tg.TOKEN = "TESTTOKEN"
        tg.MAX_ATTACHMENT_BYTES = 0

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        # tg_api("getFile") response
        mock_tg_resp = MagicMock()
        mock_tg_resp.json.return_value = {
            "ok": True,
            "result": {"file_path": "photos/file_1.jpg", "file_size": 100},
        }
        # file download response
        mock_dl_resp = MagicMock()
        mock_dl_resp.content = b"JPEGDATA"
        mock_dl_resp.raise_for_status = MagicMock()

        mock_client.post.return_value = mock_tg_resp
        mock_client.get.return_value = mock_dl_resp

        with patch.object(tg, "_get_client", return_value=mock_client):
            result = await tg.download_file("file123", tmp_path)

        assert result is not None
        local_path, ct, size = result
        assert ct == "image/jpeg"
        assert size == 8
        assert Path(local_path).exists()

    @pytest.mark.asyncio
    async def test_file_size_limit_exceeded_returns_none(self, tmp_path: Path) -> None:
        tg.TOKEN = "TESTTOKEN"
        tg.MAX_ATTACHMENT_BYTES = 50

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_tg_resp = MagicMock()
        mock_tg_resp.json.return_value = {
            "ok": True,
            "result": {"file_path": "photos/big.jpg", "file_size": 100},
        }
        mock_client.post.return_value = mock_tg_resp

        with patch.object(tg, "_get_client", return_value=mock_client):
            result = await tg.download_file("file123", tmp_path)

        assert result is None

    @pytest.mark.asyncio
    async def test_content_size_exceeds_limit_returns_none(self, tmp_path: Path) -> None:
        tg.TOKEN = "TESTTOKEN"
        tg.MAX_ATTACHMENT_BYTES = 5

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_tg_resp = MagicMock()
        mock_tg_resp.json.return_value = {
            "ok": True,
            "result": {"file_path": "photos/big.jpg", "file_size": 3},
        }
        mock_dl_resp = MagicMock()
        mock_dl_resp.content = b"TOOLARGE!"  # 9 bytes > limit of 5
        mock_dl_resp.raise_for_status = MagicMock()

        mock_client.post.return_value = mock_tg_resp
        mock_client.get.return_value = mock_dl_resp

        with patch.object(tg, "_get_client", return_value=mock_client):
            result = await tg.download_file("file123", tmp_path)

        assert result is None

    @pytest.mark.asyncio
    async def test_download_failure_returns_none(self, tmp_path: Path) -> None:
        tg.TOKEN = "TESTTOKEN"
        tg.MAX_ATTACHMENT_BYTES = 0

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_tg_resp = MagicMock()
        mock_tg_resp.json.return_value = {
            "ok": True,
            "result": {"file_path": "photos/file_1.jpg", "file_size": 100},
        }
        mock_dl_resp = MagicMock()
        mock_dl_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=MagicMock(),
        )

        mock_client.post.return_value = mock_tg_resp
        mock_client.get.return_value = mock_dl_resp

        with patch.object(tg, "_get_client", return_value=mock_client):
            result = await tg.download_file("file123", tmp_path)

        assert result is None

    @pytest.mark.asyncio
    async def test_empty_file_path_returns_none(self, tmp_path: Path) -> None:
        """When Telegram API returns no file_path, download_file returns None."""
        tg.TOKEN = "TESTTOKEN"
        tg.MAX_ATTACHMENT_BYTES = 0

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_tg_resp = MagicMock()
        mock_tg_resp.json.return_value = {
            "ok": True,
            "result": {"file_size": 100},  # no file_path key
        }
        mock_client.post.return_value = mock_tg_resp

        with patch.object(tg, "_get_client", return_value=mock_client):
            result = await tg.download_file("file123", tmp_path)

        assert result is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. Attachment handling
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _make_download_result(tmp_path: Path, content: bytes = b"binary") -> tuple[str, str, int]:
    """Helper: write a temp file and return a download_file-shaped tuple."""
    fpath = tmp_path / "dl_file"
    fpath.write_bytes(content)
    return str(fpath), "application/octet-stream", len(content)


class TestExtractAttachments:
    @pytest.mark.asyncio
    async def test_photo_picks_highest_res(self, tmp_path: Path) -> None:
        content = b"photodata"
        dl = _make_download_result(tmp_path, content)

        msg: dict[str, Any] = {
            "photo": [
                {"file_id": "low", "width": 90},
                {"file_id": "mid", "width": 320},
                {"file_id": "high", "width": 800},
            ],
        }
        with patch.object(tg, "download_file", new_callable=AsyncMock, return_value=dl) as mock_dl:
            atts = await tg.extract_attachments(msg, tmp_path)
        assert len(atts) == 1
        # Should have called download_file with "high" (last element)
        mock_dl.assert_called_once_with("high", tmp_path)

    @pytest.mark.asyncio
    async def test_voice_sets_is_voice(self, tmp_path: Path) -> None:
        content = b"voicedata"
        dl = _make_download_result(tmp_path, content)

        msg: dict[str, Any] = {"voice": {"file_id": "v1", "mime_type": "audio/ogg"}}
        with patch.object(tg, "download_file", new_callable=AsyncMock, return_value=dl):
            atts = await tg.extract_attachments(msg, tmp_path)
        assert len(atts) == 1
        assert atts[0]["is_voice"] is True

    @pytest.mark.asyncio
    async def test_document_attachment(self, tmp_path: Path) -> None:
        content = b"docdata"
        dl = _make_download_result(tmp_path, content)

        msg: dict[str, Any] = {
            "document": {"file_id": "d1", "file_name": "report.pdf", "mime_type": "application/pdf"},
        }
        with patch.object(tg, "download_file", new_callable=AsyncMock, return_value=dl):
            atts = await tg.extract_attachments(msg, tmp_path)
        assert len(atts) == 1
        assert atts[0]["filename"] == "report.pdf"
        assert atts[0]["content_type"] == "application/pdf"
        assert "is_voice" not in atts[0]

    @pytest.mark.asyncio
    async def test_video_attachment(self, tmp_path: Path) -> None:
        content = b"videodata"
        dl = _make_download_result(tmp_path, content)

        msg: dict[str, Any] = {"video": {"file_id": "vid1", "mime_type": "video/mp4"}}
        with patch.object(tg, "download_file", new_callable=AsyncMock, return_value=dl):
            atts = await tg.extract_attachments(msg, tmp_path)
        assert len(atts) == 1
        assert atts[0]["content_type"] == "video/mp4"

    @pytest.mark.asyncio
    async def test_sticker_attachment(self, tmp_path: Path) -> None:
        content = b"stickerdata"
        # download_file returns ct from extension map; sticker default_ct is
        # "image/webp" but only used when download_file ct is falsy. With our
        # helper returning "application/octet-stream" (truthy), the fallback
        # chain `ct or default_ct` keeps it.  Provide mime_type on the message
        # to exercise the mime_type-override path (what real stickers do).
        dl = _make_download_result(tmp_path, content)

        msg: dict[str, Any] = {"sticker": {"file_id": "st1", "mime_type": "image/webp"}}
        with patch.object(tg, "download_file", new_callable=AsyncMock, return_value=dl):
            atts = await tg.extract_attachments(msg, tmp_path)
        assert len(atts) == 1
        assert atts[0]["content_type"] == "image/webp"

    @pytest.mark.asyncio
    async def test_no_media_returns_empty(self, tmp_path: Path) -> None:
        msg: dict[str, Any] = {"text": "just text"}
        with patch.object(tg, "download_file", new_callable=AsyncMock):
            atts = await tg.extract_attachments(msg, tmp_path)
        assert atts == []

    @pytest.mark.asyncio
    async def test_download_failure_skips_attachment(self, tmp_path: Path) -> None:
        msg: dict[str, Any] = {"voice": {"file_id": "v1"}}
        with patch.object(tg, "download_file", new_callable=AsyncMock, return_value=None):
            atts = await tg.extract_attachments(msg, tmp_path)
        assert atts == []

    @pytest.mark.asyncio
    async def test_media_with_no_file_id_is_skipped(self, tmp_path: Path) -> None:
        """Media object present but missing file_id is skipped."""
        msg: dict[str, Any] = {"document": {"file_name": "test.pdf"}}  # no file_id
        with patch.object(tg, "download_file", new_callable=AsyncMock) as mock_dl:
            atts = await tg.extract_attachments(msg, tmp_path)
        assert atts == []
        mock_dl.assert_not_called()

    @pytest.mark.asyncio
    async def test_data_is_base64_encoded(self, tmp_path: Path) -> None:
        content = b"rawbytes"
        dl = _make_download_result(tmp_path, content)

        msg: dict[str, Any] = {"document": {"file_id": "d1"}}
        with patch.object(tg, "download_file", new_callable=AsyncMock, return_value=dl):
            atts = await tg.extract_attachments(msg, tmp_path)
        assert base64.b64decode(atts[0]["data"]) == content


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5. Send helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSendText:
    @pytest.mark.asyncio
    async def test_single_chunk_no_links(self) -> None:
        tg.CHUNK_LIMIT = 4000
        with patch.object(tg, "tg_api", new_callable=AsyncMock) as mock_api:
            await tg.send_text(123, "Hello world")
        mock_api.assert_called_once_with("sendMessage", chat_id=123, text="Hello world")

    @pytest.mark.asyncio
    async def test_multiple_chunks(self) -> None:
        tg.CHUNK_LIMIT = 10
        text = "aaaa\nbbbb\ncccc"
        with patch.object(tg, "tg_api", new_callable=AsyncMock) as mock_api:
            await tg.send_text(123, text)
        assert mock_api.call_count >= 2

    @pytest.mark.asyncio
    async def test_links_keyboard_attached_to_last_chunk(self) -> None:
        tg.CHUNK_LIMIT = 4000
        text = "Check [Docs](https://docs.example.com) and [API](https://api.example.com)"
        with patch.object(tg, "tg_api", new_callable=AsyncMock) as mock_api:
            await tg.send_text(123, text)
        call_kwargs = mock_api.call_args.kwargs
        assert "reply_markup" in call_kwargs
        kb = call_kwargs["reply_markup"]
        assert len(kb["inline_keyboard"]) == 1
        assert len(kb["inline_keyboard"][0]) == 2


class TestSendAttachment:
    @pytest.mark.asyncio
    async def test_audio_sends_voice(self, tmp_path: Path) -> None:
        f = tmp_path / "audio.ogg"
        f.write_bytes(b"oggdata")
        with patch.object(tg, "tg_api", new_callable=AsyncMock) as mock_api:
            await tg.send_attachment(123, str(f))
        mock_api.assert_called_once()
        assert mock_api.call_args.args[0] == "sendVoice"

    @pytest.mark.asyncio
    async def test_image_sends_photo(self, tmp_path: Path) -> None:
        f = tmp_path / "pic.jpg"
        f.write_bytes(b"jpgdata")
        with patch.object(tg, "tg_api", new_callable=AsyncMock) as mock_api:
            await tg.send_attachment(123, str(f))
        mock_api.assert_called_once()
        assert mock_api.call_args.args[0] == "sendPhoto"

    @pytest.mark.asyncio
    async def test_unknown_ext_sends_document(self, tmp_path: Path) -> None:
        f = tmp_path / "data.xyz"
        f.write_bytes(b"somedata")
        with patch.object(tg, "tg_api", new_callable=AsyncMock) as mock_api:
            await tg.send_attachment(123, str(f))
        mock_api.assert_called_once()
        assert mock_api.call_args.args[0] == "sendDocument"

    @pytest.mark.asyncio
    async def test_missing_file_raises(self) -> None:
        with pytest.raises(FileNotFoundError, match="Attachment not found"):
            await tg.send_attachment(123, "/no/such/file.txt")

    @pytest.mark.asyncio
    async def test_mp3_sends_voice(self, tmp_path: Path) -> None:
        f = tmp_path / "song.mp3"
        f.write_bytes(b"mp3data")
        with patch.object(tg, "tg_api", new_callable=AsyncMock) as mock_api:
            await tg.send_attachment(123, str(f))
        assert mock_api.call_args.args[0] == "sendVoice"

    @pytest.mark.asyncio
    async def test_png_sends_photo(self, tmp_path: Path) -> None:
        f = tmp_path / "img.png"
        f.write_bytes(b"pngdata")
        with patch.object(tg, "tg_api", new_callable=AsyncMock) as mock_api:
            await tg.send_attachment(123, str(f))
        assert mock_api.call_args.args[0] == "sendPhoto"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 6. Message processing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _msg(
    text: str = "hello",
    user_id: int = 1001,
    chat_id: int = 9999,
    username: str = "testuser",
    **extra: Any,
) -> dict[str, Any]:
    """Helper to build a minimal Telegram message dict."""
    m: dict[str, Any] = {
        "from": {"id": user_id, "username": username},
        "chat": {"id": chat_id},
        "text": text,
    }
    m.update(extra)
    return m


def _daemon_response(
    reply: str = "OK",
    attachments: list[dict[str, str]] | None = None,
    status_code: int = 200,
) -> MagicMock:
    """Build a mock httpx response for the daemon POST.

    Attachments are base64-encoded dicts (as returned by the API after encoding).
    """
    resp = MagicMock()
    resp.status_code = status_code
    body: dict[str, Any] = {"reply": reply}
    if attachments is not None:
        body["attachments"] = attachments
    resp.json.return_value = body
    resp.text = json.dumps(body)
    return resp


def _b64_attachment(filename: str = "audio.mp3", content: bytes = b"audiodata",
                    content_type: str = "audio/mpeg") -> dict[str, str]:
    """Build a base64-encoded outbound attachment dict."""
    return {
        "filename": filename,
        "content_type": content_type,
        "data": base64.b64encode(content).decode(),
    }


class TestProcessMessage:
    @pytest.mark.asyncio
    async def test_text_message_forwarded_and_reply_sent(self, tmp_path: Path) -> None:
        tg.ALLOW_FROM = set()
        tg._bot_id = 0
        tg.DAEMON_URL = "http://daemon:8100"

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _daemon_response("Reply text")

        with (
            patch.object(tg, "_get_client", return_value=mock_client),
            patch.object(tg, "tg_api", new_callable=AsyncMock),
            patch.object(tg, "send_text", new_callable=AsyncMock) as mock_send,
            patch.object(tg, "extract_attachments", new_callable=AsyncMock, return_value=[]),
        ):
            await tg.process_message(_msg(), tmp_path)

        mock_send.assert_called_once_with(9999, "Reply text")
        # daemon was called with correct URL
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert call_args.args[0] == "http://daemon:8100/api/v1/chat"

    @pytest.mark.asyncio
    async def test_bot_own_message_is_ignored(self, tmp_path: Path) -> None:
        tg._bot_id = 42
        tg.ALLOW_FROM = set()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        with (
            patch.object(tg, "_get_client", return_value=mock_client),
            patch.object(tg, "tg_api", new_callable=AsyncMock),
            patch.object(tg, "send_text", new_callable=AsyncMock) as mock_send,
        ):
            await tg.process_message(_msg(user_id=42), tmp_path)

        mock_send.assert_not_called()
        mock_client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_allowed_user_filtered(self, tmp_path: Path) -> None:
        tg._bot_id = 0
        tg.ALLOW_FROM = {5555}

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        with (
            patch.object(tg, "_get_client", return_value=mock_client),
            patch.object(tg, "tg_api", new_callable=AsyncMock),
            patch.object(tg, "send_text", new_callable=AsyncMock) as mock_send,
        ):
            await tg.process_message(_msg(user_id=1001), tmp_path)

        mock_send.assert_not_called()
        mock_client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_quote_prepended_to_text(self, tmp_path: Path) -> None:
        tg._bot_id = 0
        tg.ALLOW_FROM = set()
        tg.DAEMON_URL = "http://daemon:8100"

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _daemon_response("answer")

        msg = _msg(text="my reply", reply_to_message={"text": "original"})
        with (
            patch.object(tg, "_get_client", return_value=mock_client),
            patch.object(tg, "tg_api", new_callable=AsyncMock),
            patch.object(tg, "send_text", new_callable=AsyncMock),
            patch.object(tg, "extract_attachments", new_callable=AsyncMock, return_value=[]),
        ):
            await tg.process_message(msg, tmp_path)

        posted_body = mock_client.post.call_args.kwargs["json"]
        assert "[replying to: original]" in posted_body["message"]
        assert "my reply" in posted_body["message"]

    @pytest.mark.asyncio
    async def test_pre_attachments_used_instead_of_extracting(self, tmp_path: Path) -> None:
        tg._bot_id = 0
        tg.ALLOW_FROM = set()
        tg.DAEMON_URL = "http://daemon:8100"

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _daemon_response("ok")

        pre_atts = [{"content_type": "image/png", "data": "abc", "filename": "x.png"}]
        with (
            patch.object(tg, "_get_client", return_value=mock_client),
            patch.object(tg, "tg_api", new_callable=AsyncMock),
            patch.object(tg, "send_text", new_callable=AsyncMock),
            patch.object(tg, "extract_attachments", new_callable=AsyncMock) as mock_extract,
        ):
            await tg.process_message(_msg(), tmp_path, pre_attachments=pre_atts)

        mock_extract.assert_not_called()
        posted_body = mock_client.post.call_args.kwargs["json"]
        assert posted_body["attachments"] == pre_atts

    @pytest.mark.asyncio
    async def test_empty_text_and_no_attachments_skipped(self, tmp_path: Path) -> None:
        tg._bot_id = 0
        tg.ALLOW_FROM = set()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        with (
            patch.object(tg, "_get_client", return_value=mock_client),
            patch.object(tg, "tg_api", new_callable=AsyncMock),
            patch.object(tg, "send_text", new_callable=AsyncMock) as mock_send,
            patch.object(tg, "extract_attachments", new_callable=AsyncMock, return_value=[]),
        ):
            await tg.process_message(_msg(text=""), tmp_path)

        mock_send.assert_not_called()
        mock_client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_daemon_error_is_caught_and_logged(self, tmp_path: Path) -> None:
        tg._bot_id = 0
        tg.ALLOW_FROM = set()
        tg.DAEMON_URL = "http://daemon:8100"

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.side_effect = httpx.ConnectError("connection refused")

        with (
            patch.object(tg, "_get_client", return_value=mock_client),
            patch.object(tg, "tg_api", new_callable=AsyncMock),
            patch.object(tg, "send_text", new_callable=AsyncMock) as mock_send,
            patch.object(tg, "extract_attachments", new_callable=AsyncMock, return_value=[]),
        ):
            # Should not raise
            await tg.process_message(_msg(), tmp_path)

        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_daemon_returns_attachments_decoded_and_sent(self, tmp_path: Path) -> None:
        """Outbound base64 attachments are decoded to temp files and sent."""
        tg._bot_id = 0
        tg.ALLOW_FROM = set()
        tg.DAEMON_URL = "http://daemon:8100"

        atts = [
            _b64_attachment("a.png", b"pngdata", "image/png"),
            _b64_attachment("b.ogg", b"oggdata", "audio/ogg"),
        ]
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _daemon_response("reply", attachments=atts)

        sent_paths: list[str] = []

        async def capture_send(_chat_id: int, path: str) -> None:
            sent_paths.append(path)

        with (
            patch.object(tg, "_get_client", return_value=mock_client),
            patch.object(tg, "tg_api", new_callable=AsyncMock),
            patch.object(tg, "send_text", new_callable=AsyncMock),
            patch.object(tg, "send_attachment", side_effect=capture_send) as mock_sa,
            patch.object(tg, "extract_attachments", new_callable=AsyncMock, return_value=[]),
        ):
            await tg.process_message(_msg(), tmp_path)

        assert mock_sa.call_count == 2
        # Verify files were decoded with correct content
        for path_str in sent_paths:
            assert "a.png" in path_str or "b.ogg" in path_str

    @pytest.mark.asyncio
    async def test_sender_resolved_from_id_to_name(self, tmp_path: Path) -> None:
        tg._bot_id = 0
        tg.ALLOW_FROM = set()
        tg.DAEMON_URL = "http://daemon:8100"
        tg.ID_TO_NAME = {1001: "Alice"}

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _daemon_response("ok")

        with (
            patch.object(tg, "_get_client", return_value=mock_client),
            patch.object(tg, "tg_api", new_callable=AsyncMock),
            patch.object(tg, "send_text", new_callable=AsyncMock),
            patch.object(tg, "extract_attachments", new_callable=AsyncMock, return_value=[]),
        ):
            await tg.process_message(_msg(user_id=1001), tmp_path)

        posted_body = mock_client.post.call_args.kwargs["json"]
        assert posted_body["sender"] == "Alice"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 6b. Send retry + client lifecycle
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSendWithRetry:
    @pytest.mark.asyncio
    async def test_succeeds_first_attempt(self) -> None:
        """No retry needed — send_fn called once."""
        mock_fn = AsyncMock()
        await tg._send_with_retry(mock_fn, 123, "hello", retries=2, delay=0.0)
        mock_fn.assert_called_once_with(123, "hello")

    @pytest.mark.asyncio
    async def test_retries_on_connect_error(self) -> None:
        """ConnectError → client reset + retry → success on second attempt."""
        mock_fn = AsyncMock(side_effect=[httpx.ConnectError("stale"), None])
        with patch.object(tg, "_reset_client", new_callable=AsyncMock) as mock_reset:
            await tg._send_with_retry(mock_fn, 123, "hello", retries=2, delay=0.0)
        assert mock_fn.call_count == 2
        mock_reset.assert_called_once()

    @pytest.mark.asyncio
    async def test_exhausts_retries_and_raises(self) -> None:
        """All attempts fail → error logged, last exception re-raised."""
        mock_fn = AsyncMock(side_effect=httpx.ConnectError("down"))
        with patch.object(tg, "_reset_client", new_callable=AsyncMock):
            with pytest.raises(httpx.ConnectError, match="down"):
                await tg._send_with_retry(mock_fn, 123, "hello", retries=2, delay=0.0)
        assert mock_fn.call_count == 3  # 1 + 2 retries

    @pytest.mark.asyncio
    async def test_exhausts_retries_generic_exception(self) -> None:
        """Non-ConnectError exceptions also re-raised after retries."""
        mock_fn = AsyncMock(side_effect=RuntimeError("api error"))
        with pytest.raises(RuntimeError, match="api error"):
            await tg._send_with_retry(mock_fn, 123, "hello", retries=1, delay=0.0)
        assert mock_fn.call_count == 2

    @pytest.mark.asyncio
    async def test_daemon_non_200_no_send(self, tmp_path: Path) -> None:
        """Daemon returns 500 → error logged, no Telegram send attempted."""
        tg.ALLOW_FROM = set()
        tg._bot_id = 0
        tg.DAEMON_URL = "http://daemon:8100"

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _daemon_response("error", status_code=500)

        with (
            patch.object(tg, "_get_client", return_value=mock_client),
            patch.object(tg, "tg_api", new_callable=AsyncMock),
            patch.object(tg, "send_text", new_callable=AsyncMock) as mock_send,
            patch.object(tg, "extract_attachments", new_callable=AsyncMock, return_value=[]),
        ):
            await tg.process_message(_msg(), tmp_path)

        mock_send.assert_not_called()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 7. Poll loop (inbound_loop)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestInboundLoop:
    @pytest.mark.asyncio
    async def test_media_group_buffered_and_processed_together(self) -> None:
        tg._offset = 0
        tg.POLL_TIMEOUT = 30
        tg.MEDIA_GROUP_DELAY = 0.0  # flush immediately
        tg.RECONNECT_INITIAL = 0.01

        call_count = 0

        async def mock_tg_api(method: str, **kwargs: Any) -> Any:
            nonlocal call_count
            if method == "getMe":
                return {"id": 999, "username": "bot"}
            if method == "getUpdates":
                call_count += 1
                if call_count == 1:
                    return [
                        {
                            "update_id": 1,
                            "message": {
                                "message_id": 10,
                                "media_group_id": "grp1",
                                "from": {"id": 1},
                                "chat": {"id": 100},
                                "caption": "first",
                                "photo": [{"file_id": "p1", "width": 100}],
                            },
                        },
                        {
                            "update_id": 2,
                            "message": {
                                "message_id": 11,
                                "media_group_id": "grp1",
                                "from": {"id": 1},
                                "chat": {"id": 100},
                                "caption": "second",
                                "photo": [{"file_id": "p2", "width": 100}],
                            },
                        },
                    ]
                # Second poll: cancel the loop
                raise asyncio.CancelledError

            return {}

        with (
            patch.object(tg, "tg_api", side_effect=mock_tg_api),
            patch.object(tg, "extract_attachments", new_callable=AsyncMock, return_value=[]),
            patch.object(tg, "process_message", new_callable=AsyncMock) as mock_pm,
        ):
            await tg.inbound_loop()

        # Should have been called once with combined text from both messages
        mock_pm.assert_called_once()
        call_kwargs = mock_pm.call_args.kwargs
        assert "pre_attachments" in call_kwargs

    @pytest.mark.asyncio
    async def test_offset_advances_past_each_update_id(self) -> None:
        tg._offset = 0
        tg.POLL_TIMEOUT = 30
        tg.RECONNECT_INITIAL = 0.01

        call_count = 0

        async def mock_tg_api(method: str, **kwargs: Any) -> Any:
            nonlocal call_count
            if method == "getMe":
                return {"id": 999, "username": "bot"}
            if method == "getUpdates":
                call_count += 1
                if call_count == 1:
                    return [
                        {"update_id": 100, "message": {"message_id": 1, "from": {"id": 1}, "chat": {"id": 1}, "text": "hi"}},
                        {"update_id": 101, "message": {"message_id": 2, "from": {"id": 1}, "chat": {"id": 1}, "text": "there"}},
                    ]
                raise asyncio.CancelledError
            return {}

        with (
            patch.object(tg, "tg_api", side_effect=mock_tg_api),
            patch.object(tg, "process_message", new_callable=AsyncMock),
        ):
            await tg.inbound_loop()

        assert tg._offset == 102

    @pytest.mark.asyncio
    async def test_poll_error_triggers_backoff_then_retry(self) -> None:
        tg._offset = 0
        tg.RECONNECT_INITIAL = 0.01
        tg.RECONNECT_MAX = 0.05
        tg.RECONNECT_JITTER = 0.0
        tg.RECONNECT_FACTOR = 2.0

        call_count = 0

        async def mock_tg_api(method: str, **kwargs: Any) -> Any:
            nonlocal call_count
            if method == "getMe":
                return {"id": 999, "username": "bot"}
            if method == "getUpdates":
                call_count += 1
                if call_count == 1:
                    raise ConnectionError("network down")
                raise asyncio.CancelledError
            return {}

        with (
            patch.object(tg, "tg_api", side_effect=mock_tg_api),
            patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            await tg.inbound_loop()

        mock_sleep.assert_called_once()
        slept = mock_sleep.call_args.args[0]
        assert slept >= 0.0

    @pytest.mark.asyncio
    async def test_update_without_message_is_skipped(self) -> None:
        """Updates with no 'message' key (e.g. edited_message) are skipped."""
        tg._offset = 0
        tg.POLL_TIMEOUT = 30

        call_count = 0

        async def mock_tg_api(method: str, **kwargs: Any) -> Any:
            nonlocal call_count
            if method == "getMe":
                return {"id": 999, "username": "bot"}
            if method == "getUpdates":
                call_count += 1
                if call_count == 1:
                    return [{"update_id": 50, "edited_message": {"text": "edited"}}]
                raise asyncio.CancelledError
            return {}

        with (
            patch.object(tg, "tg_api", side_effect=mock_tg_api),
            patch.object(tg, "process_message", new_callable=AsyncMock) as mock_pm,
        ):
            await tg.inbound_loop()

        mock_pm.assert_not_called()
        assert tg._offset == 51  # offset still advances

    @pytest.mark.asyncio
    async def test_cancelled_error_exits_cleanly(self) -> None:
        tg._offset = 0

        async def mock_tg_api(method: str, **kwargs: Any) -> Any:
            if method == "getMe":
                return {"id": 999, "username": "bot"}
            if method == "getUpdates":
                raise asyncio.CancelledError
            return {}

        with patch.object(tg, "tg_api", side_effect=mock_tg_api):
            # Should return without error
            await tg.inbound_loop()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 9. Outbound attachment decoding & delivery failure notification
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDecodeOutboundAttachment:
    def test_decodes_base64_to_file(self, tmp_path: Path) -> None:
        att = _b64_attachment("voice.mp3", b"mp3content", "audio/mpeg")
        local = tg._decode_outbound_attachment(att, tmp_path)
        assert Path(local).exists()
        assert Path(local).read_bytes() == b"mp3content"
        assert "voice.mp3" in local

    def test_missing_data_raises(self, tmp_path: Path) -> None:
        att = {"filename": "bad.mp3", "content_type": "audio/mpeg"}
        with pytest.raises(ValueError, match="missing 'data'"):
            tg._decode_outbound_attachment(att, tmp_path)

    def test_creates_download_dir(self, tmp_path: Path) -> None:
        subdir = tmp_path / "new" / "deep"
        att = _b64_attachment()
        local = tg._decode_outbound_attachment(att, subdir)
        assert subdir.exists()
        assert Path(local).exists()


class TestDeliveryFailureNotification:
    @pytest.mark.asyncio
    async def test_delivery_failure_notifies_daemon(self) -> None:
        """Failed delivery POSTs to /api/v1/notify."""
        tg.DAEMON_URL = "http://daemon:8100"
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = MagicMock(status_code=202)

        with patch.object(tg, "_get_client", return_value=mock_client):
            await tg._notify_delivery_failure("Alice", ["text: ConnectError"])

        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert "/api/v1/notify" in call_args.args[0]
        body = call_args.kwargs["json"]
        assert "Alice" in body["message"]
        assert body["source"] == "telegram"

    @pytest.mark.asyncio
    async def test_notify_failure_does_not_raise(self) -> None:
        """If the notify POST itself fails, it logs but doesn't raise."""
        tg.DAEMON_URL = "http://daemon:8100"
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.side_effect = httpx.ConnectError("daemon down")

        with patch.object(tg, "_get_client", return_value=mock_client):
            # Must not raise
            await tg._notify_delivery_failure("Alice", ["text: error"])

    @pytest.mark.asyncio
    async def test_partial_delivery_notifies_for_attachment_only(self, tmp_path: Path) -> None:
        """Text succeeds but attachment fails → notify includes only attachment."""
        tg._bot_id = 0
        tg.ALLOW_FROM = set()
        tg.DAEMON_URL = "http://daemon:8100"

        att = _b64_attachment("voice.mp3", b"data", "audio/mpeg")
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _daemon_response("hello", attachments=[att])

        with (
            patch.object(tg, "_get_client", return_value=mock_client),
            patch.object(tg, "tg_api", new_callable=AsyncMock),
            patch.object(tg, "send_text", new_callable=AsyncMock),
            patch.object(tg, "send_attachment", new_callable=AsyncMock,
                         side_effect=RuntimeError("Telegram API error")),
            patch.object(tg, "extract_attachments", new_callable=AsyncMock, return_value=[]),
            patch.object(tg, "_notify_delivery_failure", new_callable=AsyncMock) as mock_notify,
        ):
            await tg.process_message(_msg(), tmp_path)

        mock_notify.assert_called_once()
        failures = mock_notify.call_args.args[1]
        assert len(failures) == 1
        assert "voice.mp3" in failures[0]

    @pytest.mark.asyncio
    async def test_all_deliveries_succeed_no_notification(self, tmp_path: Path) -> None:
        """Successful delivery → no notify POST."""
        tg._bot_id = 0
        tg.ALLOW_FROM = set()
        tg.DAEMON_URL = "http://daemon:8100"

        att = _b64_attachment("voice.mp3", b"data", "audio/mpeg")
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _daemon_response("hello", attachments=[att])

        with (
            patch.object(tg, "_get_client", return_value=mock_client),
            patch.object(tg, "tg_api", new_callable=AsyncMock),
            patch.object(tg, "send_text", new_callable=AsyncMock),
            patch.object(tg, "send_attachment", new_callable=AsyncMock),
            patch.object(tg, "extract_attachments", new_callable=AsyncMock, return_value=[]),
            patch.object(tg, "_notify_delivery_failure", new_callable=AsyncMock) as mock_notify,
        ):
            await tg.process_message(_msg(), tmp_path)

        mock_notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_temp_files_cleaned_up_after_send(self, tmp_path: Path) -> None:
        """Decoded temp files are deleted after sending."""
        tg._bot_id = 0
        tg.ALLOW_FROM = set()
        tg.DAEMON_URL = "http://daemon:8100"

        att = _b64_attachment("voice.mp3", b"audiodata", "audio/mpeg")
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _daemon_response("", attachments=[att])

        decoded_paths: list[str] = []

        async def capture_and_check_send(_chat_id: int, path: str) -> None:
            decoded_paths.append(path)
            assert Path(path).exists(), "File should exist during send"

        with (
            patch.object(tg, "_get_client", return_value=mock_client),
            patch.object(tg, "tg_api", new_callable=AsyncMock),
            patch.object(tg, "send_text", new_callable=AsyncMock),
            patch.object(tg, "send_attachment", side_effect=capture_and_check_send),
            patch.object(tg, "extract_attachments", new_callable=AsyncMock, return_value=[]),
        ):
            await tg.process_message(_msg(), tmp_path)

        # After process_message returns, temp files should be cleaned up
        assert len(decoded_paths) == 1
        assert not Path(decoded_paths[0]).exists(), "Temp file should be deleted after send"
