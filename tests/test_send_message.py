"""tools/send_message.py — proactive outbound tool, gated to talker=agent."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_send_message_validates_text_or_attachments_required():
    from tools.send_message import tool_send_message, configure
    configure(
        bridges_primary="telegram",
        http_auth_token="t",
        user_session_key="user:n",
        allowed_paths=[],
        http_client=AsyncMock(),
        session_mgr=AsyncMock(),
        pipeline_lock_factory=lambda key: AsyncMock(),
    )
    result = await tool_send_message(text="", attachments=[])
    assert "Error" in result["text"]
    assert "text or attachments" in result["text"].lower()


@pytest.mark.asyncio
async def test_send_message_rejects_attachment_outside_allowed_paths(tmp_path):
    """Path validation reuses the filesystem allowed_paths convention."""
    from tools.send_message import tool_send_message, configure
    configure(
        bridges_primary="telegram",
        http_auth_token="t",
        user_session_key="user:n",
        allowed_paths=[str(tmp_path)],
        http_client=AsyncMock(),
        session_mgr=AsyncMock(),
        pipeline_lock_factory=lambda key: AsyncMock(),
    )
    result = await tool_send_message(text="x", attachments=["/etc/passwd"])
    assert "Error" in result["text"]
    assert "permission" in result["text"].lower() or "allowed" in result["text"].lower()


@pytest.mark.asyncio
async def test_send_message_rejects_oversize_attachment(tmp_path):
    """Files larger than BRIDGE_LIMITS[primary]['max_attachment_bytes'] fail with actionable error."""
    from tools.send_message import tool_send_message, configure

    big = tmp_path / "big.bin"
    big.write_bytes(b"\0" * (60 * 1024 * 1024))  # 60 MB > 50 MB telegram cap

    configure(
        bridges_primary="telegram",
        http_auth_token="t",
        user_session_key="user:n",
        allowed_paths=[str(tmp_path)],
        http_client=AsyncMock(),
        session_mgr=AsyncMock(),
        pipeline_lock_factory=lambda key: AsyncMock(),
    )
    result = await tool_send_message(text="x", attachments=[str(big)])
    assert "50 MB" in result["text"] or "52428800" in result["text"]
    assert "/mnt/share/" in result["text"]


@pytest.mark.asyncio
async def test_send_message_happy_path_calls_bridge_client_and_appends_to_user_session(tmp_path, monkeypatch):
    import bridge_client
    from tools.send_message import tool_send_message, configure

    f = tmp_path / "small.pdf"
    f.write_bytes(b"PDF data")

    bridge_calls: list[dict] = []
    async def fake_bridge_send(text, attachments, primary, token, http_client):
        bridge_calls.append({"text": text, "attachments": attachments, "primary": primary})
    monkeypatch.setattr(bridge_client, "send_to_user", fake_bridge_send)

    session_appends: list[dict] = []
    session_mgr = MagicMock()
    async def fake_append(target_key, text, attachment_refs, source_metadata):
        session_appends.append({
            "target": target_key, "text": text,
            "refs": attachment_refs, "meta": source_metadata,
        })
    session_mgr.append_outbound_to_user = fake_append

    lock_acquired: list[str] = []
    class FakeLock:
        async def __aenter__(self):
            lock_acquired.append("acquired")
            return self
        async def __aexit__(self, *a):
            return False

    configure(
        bridges_primary="telegram",
        http_auth_token="t",
        user_session_key="user:n",
        allowed_paths=[str(tmp_path)],
        http_client=AsyncMock(),
        session_mgr=session_mgr,
        pipeline_lock_factory=lambda key: FakeLock(),
    )

    result = await tool_send_message(text="here you go", attachments=[str(f)])
    assert "sent" in result["text"].lower()
    assert len(bridge_calls) == 1
    assert bridge_calls[0]["text"] == "here you go"
    assert bridge_calls[0]["primary"] == "telegram"
    assert len(bridge_calls[0]["attachments"]) == 1
    assert bridge_calls[0]["attachments"][0]["filename"] == "small.pdf"
    assert lock_acquired == ["acquired"]
    assert len(session_appends) == 1
    assert session_appends[0]["target"] == "user:n"
    assert session_appends[0]["text"] == "here you go"
    assert session_appends[0]["meta"]["from"] == "agent_self_send_message"


@pytest.mark.asyncio
async def test_send_message_coerces_json_array_string_attachments(tmp_path, monkeypatch):
    """Models sometimes emit attachments as a JSON-encoded array string; coerce."""
    import bridge_client
    from tools.send_message import tool_send_message, configure

    f = tmp_path / "small.mp3"
    f.write_bytes(b"id3 data")

    bridge_calls: list[dict] = []
    async def fake_bridge_send(text, attachments, primary, token, http_client):
        bridge_calls.append({"text": text, "attachments": attachments})
    monkeypatch.setattr(bridge_client, "send_to_user", fake_bridge_send)

    class FakeLock:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    session_mgr = MagicMock()
    async def fake_append(target_key, text, attachment_refs, source_metadata):
        return None
    session_mgr.append_outbound_to_user = fake_append

    configure(
        bridges_primary="telegram",
        http_auth_token="t",
        user_session_key="user:n",
        allowed_paths=[str(tmp_path)],
        http_client=AsyncMock(),
        session_mgr=session_mgr,
        pipeline_lock_factory=lambda key: FakeLock(),
    )

    # Pass a JSON-encoded array string instead of a list. Tool should coerce.
    import json as _json
    json_str = _json.dumps([str(f)])
    result = await tool_send_message(text="here", attachments=json_str)  # type: ignore[arg-type]
    assert "sent" in result["text"].lower(), result
    assert len(bridge_calls) == 1
    assert bridge_calls[0]["attachments"][0]["filename"] == "small.mp3"


@pytest.mark.asyncio
async def test_send_message_rejects_nonjson_string_attachments(tmp_path):
    """A non-JSON string for attachments returns a clear typed error, not a path error."""
    from tools.send_message import tool_send_message, configure

    configure(
        bridges_primary="telegram",
        http_auth_token="t",
        user_session_key="user:n",
        allowed_paths=[str(tmp_path)],
        http_client=AsyncMock(),
        session_mgr=AsyncMock(),
        pipeline_lock_factory=lambda key: AsyncMock(),
    )
    result = await tool_send_message(text="x", attachments="/tmp/file.mp3")  # type: ignore[arg-type]
    assert "Error" in result["text"]
    assert "array" in result["text"].lower()
    # Make sure we didn't leak a path-allowlist rejection on the bare string.
    assert "permission denied" not in result["text"].lower()


@pytest.mark.asyncio
async def test_send_message_bridge_failure_returns_partial_error_to_agent(tmp_path, monkeypatch):
    """If bridge delivery fails, tool returns a structured error so the agent can fall back."""
    import bridge_client
    from tools.send_message import tool_send_message, configure
    from bridge_client import BridgeDeliveryError

    async def fake_bridge_fail(*args, **kwargs):
        raise BridgeDeliveryError("connection refused")
    monkeypatch.setattr(bridge_client, "send_to_user", fake_bridge_fail)

    configure(
        bridges_primary="telegram",
        http_auth_token="t",
        user_session_key="user:n",
        allowed_paths=[str(tmp_path)],
        http_client=AsyncMock(),
        session_mgr=AsyncMock(),
        pipeline_lock_factory=lambda key: AsyncMock(),
    )
    result = await tool_send_message(text="x", attachments=[])
    assert "Error" in result["text"]
    assert "connection refused" in result["text"]
