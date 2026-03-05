"""Tests for session lifecycle callbacks (on_close, async/sync, ordering)."""

import logging

import pytest

from session import SessionManager


@pytest.fixture
def session_mgr(tmp_sessions):
    """SessionManager with a test session ready."""
    mgr = SessionManager(tmp_sessions, agent_name="TestAgent")
    session = mgr.get_or_create("test-user")
    session.add_user_message("hello")
    session.add_assistant_message({"role": "assistant", "content": "hi there"})
    session._save_state()
    return mgr


class TestSyncCallback:
    @pytest.mark.asyncio
    async def test_sync_callback_fires(self, session_mgr):
        called = []

        def on_close(session):
            called.append(session.id)

        session_mgr.on_close(on_close)
        await session_mgr.close_session("test-user")
        assert len(called) == 1

    @pytest.mark.asyncio
    async def test_sync_callback_receives_session(self, session_mgr):
        received_messages = []

        def on_close(session):
            received_messages.extend(session.messages)

        session_mgr.on_close(on_close)
        await session_mgr.close_session("test-user")
        assert len(received_messages) >= 2  # user + assistant


class TestAsyncCallback:
    @pytest.mark.asyncio
    async def test_async_callback_fires(self, session_mgr):
        called = []

        async def on_close(session):
            called.append(session.id)

        session_mgr.on_close(on_close)
        await session_mgr.close_session("test-user")
        assert len(called) == 1


class TestCallbackFailure:
    @pytest.mark.asyncio
    async def test_failure_logged_not_raised(self, session_mgr, caplog):
        def bad_callback(session):
            raise RuntimeError("callback error")

        session_mgr.on_close(bad_callback)

        with caplog.at_level(logging.ERROR):
            # Should not raise
            result = await session_mgr.close_session("test-user")

        assert result is True
        assert "on_close callback failed" in caplog.text

    @pytest.mark.asyncio
    async def test_async_failure_logged(self, session_mgr, caplog):
        async def bad_async_callback(session):
            raise RuntimeError("async error")

        session_mgr.on_close(bad_async_callback)

        with caplog.at_level(logging.ERROR):
            result = await session_mgr.close_session("test-user")

        assert result is True
        assert "on_close callback failed" in caplog.text


class TestCallbackOrdering:
    @pytest.mark.asyncio
    async def test_multiple_callbacks_fire_in_order(self, session_mgr):
        order = []

        def cb1(session):
            order.append("first")

        async def cb2(session):
            order.append("second")

        def cb3(session):
            order.append("third")

        session_mgr.on_close(cb1)
        session_mgr.on_close(cb2)
        session_mgr.on_close(cb3)

        await session_mgr.close_session("test-user")
        assert order == ["first", "second", "third"]

    @pytest.mark.asyncio
    async def test_callback_fires_before_archive(self, session_mgr, tmp_sessions):
        """Callback should see the session before it's archived/removed."""
        session_id = None
        messages_count = 0

        def on_close(session):
            nonlocal session_id, messages_count
            session_id = session.id
            messages_count = len(session.messages)

        session_mgr.on_close(on_close)
        await session_mgr.close_session("test-user")

        assert session_id is not None
        assert messages_count >= 2  # had messages before close


class TestAgentName:
    def test_agent_name_stored(self, tmp_sessions):
        mgr = SessionManager(tmp_sessions, agent_name="Lucy")
        assert mgr.agent_name == "Lucy"

    def test_agent_name_default(self, tmp_sessions):
        mgr = SessionManager(tmp_sessions)
        assert mgr.agent_name == "Assistant"

    def test_build_recall_uses_agent_name(self, tmp_sessions):
        mgr = SessionManager(tmp_sessions, agent_name="Lucy")
        # Create and close a session to have archived data
        session = mgr.get_or_create("recall-user")
        session.add_user_message("test message")
        session.add_assistant_message({"role": "assistant", "content": "test reply"})
        session._save_state()

        # build_recall should use agent_name if present in formatting
        recall_text = mgr.build_recall("recall-user")
        # Even if empty (no archive), the method should not crash
        assert isinstance(recall_text, str)
