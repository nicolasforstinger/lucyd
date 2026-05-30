"""Tests for the inbound-reply bridge fallback path.

When a user-channel bridge (Telegram, email) POSTs to ``/api/v1/inbound/...``
but disconnects before the daemon writes the reply (e.g. the bridge's
client-side request timeout fired during a long agentic turn), the daemon
falls back to ``bridge_client.send_to_user`` so the user still receives
the message.

Regression: 2026-05-07 silent-drop incident — Telegram bridge's 300s
timeout fired during a 293s thinking turn; subsequent reply text and
``send_file`` artefact were lost (no Telegram delivery, no system:error).
"""
from __future__ import annotations

import asyncio
import base64
from typing import Any
from unittest.mock import AsyncMock

import pytest

aiohttp = pytest.importorskip("aiohttp")

import bridge_client  # noqa: E402
from api import HTTPApi  # noqa: E402
from tests.test_api import _HTTP_DEFAULTS, _async_val  # noqa: E402


def _build_api(*, bridges_primary: str = "telegram") -> HTTPApi:
    return HTTPApi(
        queue=asyncio.Queue(),
        host="127.0.0.1",
        port=0,
        auth_token="t",
        agent_timeout=5.0,
        get_status=_async_val({"status": "ok"}),
        trust_localhost=True,
        bridges_primary=bridges_primary,
        outbound_http_client=AsyncMock(),
        **_HTTP_DEFAULTS,
    )


@pytest.mark.asyncio
async def test_fallback_delivers_text_via_bridge(monkeypatch):
    """A reply with text gets posted via bridge_client.send_to_user."""
    sent: dict[str, Any] = {}

    async def fake_send_to_user(text, attachments, primary, token, http_client):
        sent["text"] = text
        sent["attachments"] = attachments
        sent["primary"] = primary

    monkeypatch.setattr(bridge_client, "send_to_user", fake_send_to_user)

    api = _build_api()
    result: dict[str, Any] = {"reply": "hello after long think", "session_id": "s1",
                              "tokens": {"input": 1, "output": 1}, "attachments": []}
    await api._fallback_to_bridge("telegram", result, [])

    assert sent["text"] == "hello after long think"
    assert sent["attachments"] == []
    assert sent["primary"] == "telegram"


@pytest.mark.asyncio
async def test_fallback_delivers_attachments_with_data_b64(monkeypatch, tmp_path):
    """Attachment paths get re-read and encoded as data_b64 for the bridge."""
    sent: dict[str, Any] = {}

    async def fake_send_to_user(text, attachments, primary, token, http_client):
        sent["text"] = text
        sent["attachments"] = list(attachments)

    monkeypatch.setattr(bridge_client, "send_to_user", fake_send_to_user)

    p = tmp_path / "voice.mp3"
    p.write_bytes(b"\x00\x01\x02\x03fake-mp3-bytes")

    api = _build_api()
    # _encode_outbound_attachments has already mutated result["attachments"]
    # to its HTTP shape; the fallback re-reads from the original on-disk paths.
    result: dict[str, Any] = {"reply": "see attached", "session_id": "s1",
                              "tokens": {"input": 1, "output": 1},
                              "attachments": [{"filename": "voice.mp3",
                                               "content_type": "audio/mpeg",
                                               "data": "aWdub3JlZA=="}]}
    await api._fallback_to_bridge("telegram", result, [str(p)])

    assert sent["text"] == "see attached"
    assert len(sent["attachments"]) == 1
    att = sent["attachments"][0]
    assert att["filename"] == "voice.mp3"
    assert att["content_type"] == "audio/mpeg"
    assert base64.b64decode(att["data_b64"]) == b"\x00\x01\x02\x03fake-mp3-bytes"


@pytest.mark.asyncio
async def test_fallback_skips_error_replies(monkeypatch):
    """Error responses are never delivered via the bridge fallback."""
    called = False

    async def fake_send_to_user(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(bridge_client, "send_to_user", fake_send_to_user)

    api = _build_api()
    await api._fallback_to_bridge(
        "telegram",
        {"error": "request blocked by guardrail"},
        [],
    )
    assert called is False


@pytest.mark.asyncio
async def test_fallback_skips_silent_replies(monkeypatch):
    """Silent replies are framework-internal and never fall back to user."""
    called = False

    async def fake_send_to_user(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(bridge_client, "send_to_user", fake_send_to_user)

    api = _build_api()
    await api._fallback_to_bridge(
        "telegram",
        {"reply": "thinking out loud", "silent": True, "session_id": "s",
         "tokens": {"input": 1, "output": 1}, "attachments": []},
        [],
    )
    assert called is False


@pytest.mark.asyncio
async def test_fallback_skips_empty_replies(monkeypatch):
    """No text + no attachments = nothing to deliver, no fallback call."""
    called = False

    async def fake_send_to_user(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(bridge_client, "send_to_user", fake_send_to_user)

    api = _build_api()
    await api._fallback_to_bridge(
        "telegram",
        {"reply": "  ", "session_id": "s",
         "tokens": {"input": 1, "output": 1}, "attachments": []},
        [],
    )
    assert called is False


@pytest.mark.asyncio
async def test_fallback_skips_when_no_primary_bridge(monkeypatch):
    """If no bridge is configured, fallback no-ops rather than raising."""
    called = False

    async def fake_send_to_user(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(bridge_client, "send_to_user", fake_send_to_user)

    api = _build_api(bridges_primary="")
    await api._fallback_to_bridge(
        "telegram",
        {"reply": "hi", "session_id": "s",
         "tokens": {"input": 1, "output": 1}, "attachments": []},
        [],
    )
    assert called is False


@pytest.mark.asyncio
async def test_fallback_swallows_bridge_delivery_failure(monkeypatch):
    """Bridge delivery failure is logged, not re-raised — caller path is dead."""
    async def fake_send_to_user(*args, **kwargs):
        raise bridge_client.BridgeDeliveryError("boom")

    monkeypatch.setattr(bridge_client, "send_to_user", fake_send_to_user)

    api = _build_api()
    # Must not raise.
    await api._fallback_to_bridge(
        "telegram",
        {"reply": "important", "session_id": "s",
         "tokens": {"input": 1, "output": 1}, "attachments": []},
        [],
    )


@pytest.mark.asyncio
async def test_inbound_disconnect_triggers_fallback(monkeypatch):
    """If the response write raises ConnectionResetError, the fallback fires.

    Simulates the original bug: bridge POST connection has been closed by
    the time the daemon writes the reply. ``_write_inbound_response`` catches
    the connection error and re-routes via the bridge.
    """
    sent: dict[str, Any] = {}

    async def fake_send_to_user(text, attachments, primary, token, http_client):
        sent["text"] = text
        sent["attachments"] = list(attachments)

    monkeypatch.setattr(bridge_client, "send_to_user", fake_send_to_user)

    api = _build_api()

    # Build a fake StreamResponse whose .prepare() succeeds but .write()
    # raises ConnectionResetError, mimicking aiohttp behaviour when the
    # client has already closed the connection.
    class FakeRequest:
        pass

    request = FakeRequest()  # type: ignore[assignment]

    # Patch StreamResponse to raise on write.
    from aiohttp import web

    class DisconnectingResponse(web.StreamResponse):
        async def prepare(self, request):  # type: ignore[override]
            return None

        async def write(self, data):  # type: ignore[override]
            raise ConnectionResetError("client gone")

        async def write_eof(self, data=b""):  # type: ignore[override]
            return None

    monkeypatch.setattr("api.web.StreamResponse", DisconnectingResponse)

    result: dict[str, Any] = {"reply": "delayed reply", "session_id": "s1",
                              "tokens": {"input": 1, "output": 1}, "attachments": []}
    await api._write_inbound_response(request, "telegram", result, [])

    assert sent.get("text") == "delayed reply"
    assert sent.get("attachments") == []
