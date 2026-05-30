"""Tests for POST /api/v1/outbound/send — at-job target for remind_user."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

aiohttp = pytest.importorskip("aiohttp")
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from api import HTTPApi  # noqa: E402
from tests.test_api import _HTTP_DEFAULTS, _async_val, _make_app  # noqa: E402


def _build_api(*, auth_token: str = "t", bridges_primary: str = "telegram",
               http_client: object | None = None,
               session_mgr: object | None = None,
               pipeline_lock_factory: object | None = None) -> HTTPApi:
    """Construct an HTTPApi for outbound tests."""
    return HTTPApi(
        queue=asyncio.Queue(),
        host="127.0.0.1",
        port=0,
        auth_token=auth_token,
        agent_timeout=5.0,
        get_status=_async_val({"status": "ok"}),
        trust_localhost=False,  # disable so auth is exercised
        bridges_primary=bridges_primary,
        outbound_http_client=http_client or AsyncMock(),
        session_mgr=session_mgr,
        pipeline_lock_factory=pipeline_lock_factory,
        **_HTTP_DEFAULTS,
    )


@pytest.mark.asyncio
async def test_outbound_send_calls_bridge_client(monkeypatch):
    sent: dict[str, object] = {}

    async def fake_send_to_user(text, attachments, primary, token, http_client):
        sent["text"] = text
        sent["attachments"] = attachments
        sent["primary"] = primary
        sent["token"] = token

    import bridge_client
    monkeypatch.setattr(bridge_client, "send_to_user", fake_send_to_user)

    api = _build_api(auth_token="t", bridges_primary="telegram")
    app = _make_app(api)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/v1/outbound/send",
            json={"text": "hello"},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status == 200
    assert sent["text"] == "hello"
    assert sent["primary"] == "telegram"


@pytest.mark.asyncio
async def test_outbound_send_rejects_unauth():
    api = _build_api(auth_token="t")
    app = _make_app(api)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/v1/outbound/send", json={"text": "x"},
        )
    assert resp.status == 401


@pytest.mark.asyncio
async def test_outbound_send_validation_errors():
    api = _build_api(auth_token="t")
    app = _make_app(api)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/v1/outbound/send", json={},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_outbound_send_503_when_no_primary():
    api = _build_api(auth_token="t", bridges_primary="")
    app = _make_app(api)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/v1/outbound/send", json={"text": "x"},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status == 503


@pytest.mark.asyncio
async def test_outbound_send_appends_to_user_session(monkeypatch):
    """Successful delivery also appends an AssistantMessage to user:<user_name>."""
    async def fake_send_to_user(text, attachments, primary, token, http_client):
        return None

    import bridge_client
    monkeypatch.setattr(bridge_client, "send_to_user", fake_send_to_user)

    appended: list[dict] = []
    session_mgr = AsyncMock()
    async def fake_append(target_key, text, attachment_refs, source_metadata):
        appended.append({
            "target": target_key, "text": text,
            "refs": attachment_refs, "meta": source_metadata,
        })
    session_mgr.append_outbound_to_user = fake_append

    class FakeLock:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    api = _build_api(
        auth_token="t", bridges_primary="telegram",
        session_mgr=session_mgr,
        pipeline_lock_factory=lambda key: FakeLock(),
    )
    app = _make_app(api)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/v1/outbound/send", json={"text": "hi from endpoint"},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status == 200
    assert len(appended) == 1
    assert appended[0]["target"] == "user:testuser"
    assert appended[0]["text"] == "hi from endpoint"
    assert appended[0]["meta"]["from"] == "outbound_endpoint"
