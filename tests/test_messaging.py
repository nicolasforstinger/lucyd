"""Tests for messaging tools — message sending and reactions."""

from unittest.mock import AsyncMock

import pytest

from tools.messaging import (
    set_channel,
    set_timestamp_getter,
    tool_message,
    tool_react,
)


@pytest.fixture(autouse=True)
def reset_module_state():
    """Reset module-level state between tests."""
    import tools.messaging as mod
    original_channel = mod._channel
    original_getter = mod._get_timestamp
    yield
    mod._channel = original_channel
    mod._get_timestamp = original_getter


# ─── tool_message ────────────────────────────────────────────────


class TestToolMessage:
    @pytest.mark.asyncio
    async def test_no_channel(self):
        set_channel(None)
        result = await tool_message("Nicolas", text="hi")
        assert "No channel configured" in result

    @pytest.mark.asyncio
    async def test_no_text_or_attachments(self):
        set_channel(AsyncMock())
        result = await tool_message("Nicolas")
        assert "Must provide text or attachments" in result

    @pytest.mark.asyncio
    async def test_send_text(self):
        ch = AsyncMock()
        set_channel(ch)
        result = await tool_message("Nicolas", text="hello")
        ch.send.assert_awaited_once_with("Nicolas", "hello", None)
        assert "Sent text to Nicolas" in result

    @pytest.mark.asyncio
    async def test_send_attachments(self):
        from tools import filesystem
        filesystem.configure(["/tmp"])
        ch = AsyncMock()
        set_channel(ch)
        result = await tool_message("Nicolas", attachments=["/tmp/file.png"])
        assert "1 attachment(s)" in result
        filesystem.configure([])

    @pytest.mark.asyncio
    async def test_send_error(self):
        ch = AsyncMock()
        ch.send.side_effect = RuntimeError("connection lost")
        set_channel(ch)
        result = await tool_message("Nicolas", text="hi")
        assert "Error: Message delivery failed" in result


# ─── Attachment Path Validation ──────────────────────────────────


class TestAttachmentValidation:
    @pytest.fixture(autouse=True)
    def setup_allowlist(self, tmp_path):
        from tools import filesystem
        filesystem.configure([str(tmp_path)])
        yield
        filesystem.configure([])

    @pytest.mark.asyncio
    async def test_attachment_path_allowed(self, tmp_path):
        ch = AsyncMock()
        set_channel(ch)
        allowed = str(tmp_path / "photo.png")
        result = await tool_message("Nicolas", text="check this", attachments=[allowed])
        assert "Sent" in result
        ch.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_attachment_path_blocked(self):
        ch = AsyncMock()
        set_channel(ch)
        result = await tool_message("Nicolas", text="leak", attachments=["/etc/shadow"])
        assert "Attachment path not allowed" in result
        ch.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_attachment_path_traversal(self, tmp_path):
        ch = AsyncMock()
        set_channel(ch)
        evil = str(tmp_path / "sub" / ".." / ".." / "etc" / "passwd")
        result = await tool_message("Nicolas", text="leak", attachments=[evil])
        assert "Attachment path not allowed" in result
        ch.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_attachment_none(self):
        ch = AsyncMock()
        set_channel(ch)
        result = await tool_message("Nicolas", text="just text")
        assert "Sent text to Nicolas" in result
        ch.send.assert_awaited_once()


# ─── tool_react ──────────────────────────────────────────────────


class TestToolReact:
    @pytest.mark.asyncio
    async def test_no_channel(self):
        set_channel(None)
        set_timestamp_getter(lambda s: 12345)
        result = await tool_react("Nicolas", "❤️")
        assert "No channel configured" in result

    @pytest.mark.asyncio
    async def test_no_timestamp_getter(self):
        set_channel(AsyncMock())
        set_timestamp_getter(None)
        import tools.messaging as mod
        mod._get_timestamp = None
        result = await tool_react("Nicolas", "❤️")
        assert "Timestamp tracking not configured" in result

    @pytest.mark.asyncio
    async def test_no_timestamp_for_sender(self):
        set_channel(AsyncMock())
        set_timestamp_getter(lambda s: None)
        result = await tool_react("Nicolas", "❤️")
        assert "No recent message timestamp" in result

    @pytest.mark.asyncio
    async def test_successful_reaction(self):
        ch = AsyncMock()
        set_channel(ch)
        set_timestamp_getter(lambda s: 1707700000000)
        result = await tool_react("Nicolas", "🦇")
        ch.send_reaction.assert_awaited_once_with("Nicolas", "🦇", 1707700000000)
        assert "Reacted with 🦇" in result

    @pytest.mark.asyncio
    async def test_sender_override(self):
        """When sender is provided, use it for timestamp lookup."""
        ch = AsyncMock()
        set_channel(ch)
        timestamps = {"+431234": 111, "+435678": 222}
        set_timestamp_getter(lambda s: timestamps.get(s))
        await tool_react("Nicolas", "👍", sender="+435678")
        ch.send_reaction.assert_awaited_once_with("Nicolas", "👍", 222)

    @pytest.mark.asyncio
    async def test_reaction_error(self):
        ch = AsyncMock()
        ch.send_reaction.side_effect = RuntimeError("channel error")
        set_channel(ch)
        set_timestamp_getter(lambda s: 12345)
        result = await tool_react("Nicolas", "❤️")
        assert "Error: Reaction failed" in result
        assert "channel error" in result
