"""Tests for bridge_client.py — channel-agnostic outbound delivery."""
from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from bridge_client import (
    BRIDGE_LIMITS,
    BridgeDeliveryError,
    OutboundAttachment,
    send_to_user,
)


def test_bridge_limits_has_known_bridges():
    """telegram and email are present with port + max_attachment_bytes."""
    assert "telegram" in BRIDGE_LIMITS
    assert "email" in BRIDGE_LIMITS
    for name, info in BRIDGE_LIMITS.items():
        assert "port" in info
        assert "max_attachment_bytes" in info
        assert info["max_attachment_bytes"] > 0


@pytest.mark.asyncio
async def test_send_to_user_posts_to_primary_bridge_port():
    client = AsyncMock(spec=httpx.AsyncClient)
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value={"delivered": True})
    client.post = AsyncMock(return_value=response)

    await send_to_user(
        text="hello",
        attachments=[],
        primary="telegram",
        token="secret",
        http_client=client,
    )
    client.post.assert_awaited_once()
    args, kwargs = client.post.call_args
    url = args[0] if args else kwargs.get("url", "")
    assert url == "http://127.0.0.1:8101/send"
    assert kwargs["headers"]["Authorization"] == "Bearer secret"
    assert kwargs["json"]["text"] == "hello"
    assert kwargs["json"]["attachments"] == []


@pytest.mark.asyncio
async def test_send_to_user_unknown_bridge_raises():
    client = AsyncMock(spec=httpx.AsyncClient)
    with pytest.raises(BridgeDeliveryError, match="unknown bridge"):
        await send_to_user(
            text="x",
            attachments=[],
            primary="not_a_bridge",
            token="secret",
            http_client=client,
        )


@pytest.mark.asyncio
async def test_send_to_user_empty_primary_raises():
    client = AsyncMock(spec=httpx.AsyncClient)
    with pytest.raises(BridgeDeliveryError, match="no primary bridge"):
        await send_to_user(
            text="x",
            attachments=[],
            primary="",
            token="secret",
            http_client=client,
        )


@pytest.mark.asyncio
async def test_send_to_user_http_error_raises_bridge_delivery_error():
    client = AsyncMock(spec=httpx.AsyncClient)
    client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(BridgeDeliveryError, match="telegram"):
        await send_to_user(
            text="x",
            attachments=[],
            primary="telegram",
            token="secret",
            http_client=client,
        )


@pytest.mark.asyncio
async def test_send_to_user_passes_through_attachments():
    client = AsyncMock(spec=httpx.AsyncClient)
    response = MagicMock()
    response.raise_for_status = MagicMock()
    client.post = AsyncMock(return_value=response)

    att: OutboundAttachment = {
        "filename": "x.pdf",
        "content_type": "application/pdf",
        "data_b64": base64.b64encode(b"hello").decode("ascii"),
    }
    await send_to_user(
        text="x",
        attachments=[att],
        primary="telegram",
        token="t",
        http_client=client,
    )
    _, kwargs = client.post.call_args
    assert kwargs["json"]["attachments"] == [att]
