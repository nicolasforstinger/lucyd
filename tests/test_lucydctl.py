"""Tests for bin/lucydctl — thin HTTP client CLI.

The CLI is a thin wrapper over httpx. Endpoint logic is tested in
test_api.py. These tests verify arg parsing, HTTP routing, and
error handling.
"""

from __future__ import annotations

import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Load the script as a module
_BIN_DIR = Path(__file__).resolve().parent.parent / "bin"

_loader = SourceFileLoader("lucydctl", str(_BIN_DIR / "lucydctl"))
_spec = importlib.util.spec_from_loader("lucydctl", _loader)
lucydctl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lucydctl)

_http_get = lucydctl._http_get
_http_post = lucydctl._http_post
_output = lucydctl._output
_encode_attachments = lucydctl._encode_attachments


# ─── Transport Tests ─────────────────────────────────────────────


class TestHttpGet:
    def test_calls_httpx_get(self):
        """_http_get calls httpx.get with correct URL and auth."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok"}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(lucydctl, "httpx") as mock_httpx:
            mock_httpx.get.return_value = mock_resp
            with patch.dict("os.environ", {"LUCYD_URL": "http://test:9999", "LUCYD_HTTP_TOKEN": "tok123"}):
                result = _http_get("status")

        mock_httpx.get.assert_called_once()
        call_args = mock_httpx.get.call_args
        assert "http://test:9999/api/v1/status" == call_args[0][0]
        assert call_args[1]["headers"]["Authorization"] == "Bearer tok123"
        assert result == {"status": "ok"}

    def test_passes_params(self):
        """Query parameters are forwarded."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(lucydctl, "httpx") as mock_httpx:
            mock_httpx.get.return_value = mock_resp
            with patch.dict("os.environ", {"LUCYD_HTTP_TOKEN": "t"}):
                _http_get("cost", {"period": "2026-03"})

        assert mock_httpx.get.call_args[1]["params"] == {"period": "2026-03"}


class TestHttpPost:
    def test_calls_httpx_post(self):
        """_http_post calls httpx.post with JSON body."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "hi"}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(lucydctl, "httpx") as mock_httpx:
            mock_httpx.post.return_value = mock_resp
            with patch.dict("os.environ", {"LUCYD_HTTP_TOKEN": "t"}):
                result = _http_post("chat", {"message": "hello"})

        call_args = mock_httpx.post.call_args
        assert call_args[1]["json"] == {"message": "hello"}
        assert result == {"response": "hi"}


class TestDefaultUrl:
    def test_defaults_to_localhost(self):
        """Without LUCYD_URL, defaults to http://127.0.0.1:8100."""
        with patch.dict("os.environ", {}, clear=False):
            # Remove LUCYD_URL if set
            import os
            os.environ.pop("LUCYD_URL", None)
            assert lucydctl._url() == "http://127.0.0.1:8100"


# ─── Attachment Encoding ─────────────────────────────────────────


class TestEncodeAttachments:
    def test_encodes_file_as_base64(self, tmp_path):
        """Local file is base64-encoded with content type."""
        f = tmp_path / "test.txt"
        f.write_text("hello")
        result = _encode_attachments([str(f)])
        assert len(result) == 1
        assert result[0]["filename"] == "test.txt"
        assert result[0]["content_type"] == "text/plain"
        import base64
        assert base64.b64decode(result[0]["data"]) == b"hello"

    def test_missing_file_exits(self, tmp_path):
        """Non-existent file exits with code 1."""
        with pytest.raises(SystemExit) as exc_info:
            _encode_attachments([str(tmp_path / "nope.txt")])
        assert exc_info.value.code == 1

    def test_directory_exits(self, tmp_path):
        """Directory (not file) exits with code 1."""
        d = tmp_path / "subdir"
        d.mkdir()
        with pytest.raises(SystemExit) as exc_info:
            _encode_attachments([str(d)])
        assert exc_info.value.code == 1


# ─── Output ──────────────────────────────────────────────────────


class TestOutput:
    def test_prints_json(self, capsys):
        """_output prints indented JSON to stdout."""
        _output({"key": "value"})
        data = json.loads(capsys.readouterr().out)
        assert data == {"key": "value"}
