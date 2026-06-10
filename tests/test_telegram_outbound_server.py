"""Telegram bridge /send endpoint tests."""
from __future__ import annotations

import base64

import pytest

aiohttp = pytest.importorskip("aiohttp")
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402


@pytest.fixture
def auth_token() -> str:
    return "test-token-123"


@pytest.fixture
def app_factory(auth_token):
    """Build the aiohttp app via the shared bridge_outbound_server helper.

    Wires fake send_text / send_attachment functions so we can assert
    they were called rather than touching real Telegram.
    """
    from channels.bridge_outbound_server import build_outbound_app

    sent_texts: list[tuple[int, str]] = []
    sent_attachments: list[tuple[int, str, str]] = []

    async def fake_send_text(chat_id: int, text: str) -> None:
        sent_texts.append((chat_id, text))

    async def fake_send_attachment(chat_id: int, path: str, *, caption: str = "") -> None:
        sent_attachments.append((chat_id, path, caption))

    def build(max_attachment_bytes: int = 52_428_800):
        app = build_outbound_app(
            token=auth_token,
            recipient=12345,
            send_text=fake_send_text,
            send_attachment=fake_send_attachment,
            max_attachment_bytes=max_attachment_bytes,
        )
        app["sent_texts"] = sent_texts
        app["sent_attachments"] = sent_attachments
        return app

    return build


@pytest.mark.asyncio
async def test_send_endpoint_rejects_missing_auth(app_factory):
    app = app_factory()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/send", json={"text": "hi"})
        assert resp.status == 401


@pytest.mark.asyncio
async def test_send_endpoint_rejects_wrong_auth(app_factory, auth_token):
    app = app_factory()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/send", json={"text": "hi"},
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status == 401


@pytest.mark.asyncio
async def test_send_endpoint_text_only(app_factory, auth_token):
    app = app_factory()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/send", json={"text": "hello"},
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert resp.status == 200
        assert app["sent_texts"] == [(12345, "hello")]
        assert app["sent_attachments"] == []


@pytest.mark.asyncio
async def test_send_endpoint_with_attachment(app_factory, auth_token):
    payload = b"PDF DATA"
    app = app_factory()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/send",
            json={
                "text": "see attached",
                "attachments": [{
                    "filename": "x.pdf",
                    "content_type": "application/pdf",
                    "data_b64": base64.b64encode(payload).decode("ascii"),
                }],
            },
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert resp.status == 200
        assert len(app["sent_attachments"]) == 1
        chat_id, path, caption = app["sent_attachments"][0]
        assert chat_id == 12345
        assert caption == "see attached"
        # Bridge wrote the payload to a tempfile and passed the path —
        # tempfile is unlinked after dispatch, so we cannot read it back.
        # Verify the path was inside the tempdir prefix instead.
        assert "lucyd-outbound-" in path


@pytest.mark.asyncio
async def test_send_endpoint_requires_text_or_attachments(app_factory, auth_token):
    app = app_factory()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/send", json={},
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert resp.status == 400


@pytest.mark.asyncio
async def test_send_endpoint_accepts_attachment_under_advertised_limit(
    app_factory, auth_token,
):
    """The aiohttp client_max_size must scale with max_attachment_bytes —
    otherwise aiohttp's 1 MB default 413s requests well under the bridge's
    advertised cap. Empirically: a 916 KB voice file (~1.22 MB after b64)
    got 413'd on 2026-04-29 12:01 UTC because the app was built without
    client_max_size."""
    # 2 MB raw payload — fits the default Telegram bridge cap (50 MB),
    # exceeds aiohttp's default 1 MB client_max_size.
    payload = b"\x00" * (2 * 1024 * 1024)
    app = app_factory()  # uses default 50 MB cap
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/send",
            json={
                "text": "voice message",
                "attachments": [{
                    "filename": "voice.mp3",
                    "content_type": "audio/mpeg",
                    "data_b64": base64.b64encode(payload).decode("ascii"),
                }],
            },
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert resp.status == 200, f"got {resp.status}, expected 200"
        assert len(app["sent_attachments"]) == 1


@pytest.mark.asyncio
async def test_send_endpoint_413s_attachment_over_configured_limit(
    app_factory, auth_token,
):
    """The cap is real — bodies above (max × 4/3 + 64KB) still get 413.
    Wires the fixture with a 100 KB cap and posts ~200 KB of base64."""
    payload = b"\x00" * (200 * 1024)  # 200 KB raw → ~267 KB base64 + envelope
    app = app_factory(max_attachment_bytes=100 * 1024)  # 100 KB cap
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/send",
            json={
                "text": "too big",
                "attachments": [{
                    "filename": "x.bin",
                    "content_type": "application/octet-stream",
                    "data_b64": base64.b64encode(payload).decode("ascii"),
                }],
            },
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert resp.status == 413
