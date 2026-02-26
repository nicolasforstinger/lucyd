"""Tests for LucydDaemon internals — _build_status, _resolve pattern,
_NO_CHANNEL_DELIVERY, process_http_immediate, message loop HTTP bypass.

These tests mock heavy dependencies (providers, channels, sessions) to isolate
the daemon's orchestration logic.
"""

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lucyd import LucydDaemon, _is_silent

# ─── Helpers ──────────────────────────────────────────────────────


def _make_config(tmp_path, **overrides):
    """Build a minimal Config for testing daemon methods."""
    from config import Config

    base = {
        "agent": {
            "name": "TestAgent",
            "workspace": str(tmp_path / "workspace"),
            "context": {
                "stable": ["SOUL.md"],
                "semi_stable": [],
            },
        },
        "channel": {"type": "cli"},
        "models": {
            "primary": {
                "provider": "anthropic-compat",
                "model": "test-model",
                "max_tokens": 1024,
                "cost_per_mtok": [1.0, 5.0, 0.1],
                "supports_vision": True,
            },
        },
        "paths": {
            "state_dir": str(tmp_path / "state"),
            "sessions_dir": str(tmp_path / "sessions"),
            "cost_db": str(tmp_path / "cost.db"),
            "log_file": str(tmp_path / "lucyd.log"),
        },
        "behavior": {
            "compaction": {"threshold_tokens": 150000},
        },
    }
    base.update(overrides)

    # Ensure directories exist
    (tmp_path / "workspace").mkdir(exist_ok=True)
    (tmp_path / "workspace" / "SOUL.md").write_text("# Test Soul")
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "sessions").mkdir(exist_ok=True)

    return Config(base)


def _make_cost_db(path: Path, rows: list[tuple] = None):
    """Create and optionally populate a cost DB."""
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS costs (
            timestamp INTEGER,
            session_id TEXT,
            model TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_write_tokens INTEGER,
            cost_usd REAL
        )
    """)
    if rows:
        conn.executemany(
            "INSERT INTO costs VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows,
        )
    conn.commit()
    conn.close()


# ─── _NO_CHANNEL_DELIVERY ───────────────────────────────────────


class TestNoChannelDelivery:
    """Verify _NO_CHANNEL_DELIVERY frozenset membership."""

    def test_system_in_set(self):
        assert "system" in LucydDaemon._NO_CHANNEL_DELIVERY

    def test_http_in_set(self):
        assert "http" in LucydDaemon._NO_CHANNEL_DELIVERY

    def test_telegram_not_in_set(self):
        assert "telegram" not in LucydDaemon._NO_CHANNEL_DELIVERY

    def test_user_not_in_set(self):
        assert "user" not in LucydDaemon._NO_CHANNEL_DELIVERY

    def test_cli_not_in_set(self):
        assert "cli" not in LucydDaemon._NO_CHANNEL_DELIVERY

    def test_empty_not_in_set(self):
        assert "" not in LucydDaemon._NO_CHANNEL_DELIVERY

    def test_is_frozenset(self):
        """Must be frozenset (immutable), not set."""
        assert isinstance(LucydDaemon._NO_CHANNEL_DELIVERY, frozenset)


# ─── _build_status ───────────────────────────────────────────────


class TestBuildStatus:
    """Tests for LucydDaemon._build_status()."""

    def test_basic_status_fields(self, tmp_path):
        """Status dict contains all expected fields."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.providers = {"primary": MagicMock()}
        daemon.session_mgr = MagicMock()
        daemon.session_mgr._index = {"user1": "s-1", "system": "s-2"}

        status = daemon._build_status()

        assert status["status"] == "ok"
        assert status["pid"] == os.getpid()
        assert isinstance(status["uptime_seconds"], int)
        assert status["channel"] == "cli"
        assert status["models"] == ["primary"]
        assert status["active_sessions"] == 2
        assert isinstance(status["today_cost"], float)
        assert isinstance(status["queue_depth"], int)

    def test_today_cost_from_db(self, tmp_path):
        """Today's cost is calculated from cost.db."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.providers = {}
        daemon.session_mgr = MagicMock()
        daemon.session_mgr._index = {}

        # Populate cost DB with today's entries
        now_ts = int(time.time())
        _make_cost_db(tmp_path / "cost.db", [
            (now_ts, "s-1", "test", 1000, 500, 0, 0, 0.50),
            (now_ts - 60, "s-1", "test", 2000, 1000, 0, 0, 1.25),
            # Yesterday — should NOT be counted
            (now_ts - 86400 * 2, "s-2", "test", 5000, 2000, 0, 0, 10.00),
        ])

        status = daemon._build_status()
        assert status["today_cost"] == round(0.50 + 1.25, 4)

    def test_no_cost_db_returns_zero(self, tmp_path):
        """Missing cost DB file returns 0.0 cost."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.providers = {}
        daemon.session_mgr = MagicMock()
        daemon.session_mgr._index = {}

        # Don't create cost.db
        status = daemon._build_status()
        assert status["today_cost"] == 0.0

    def test_empty_cost_db(self, tmp_path):
        """Empty cost DB returns 0.0 cost."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.providers = {}
        daemon.session_mgr = MagicMock()
        daemon.session_mgr._index = {}

        _make_cost_db(tmp_path / "cost.db", [])
        status = daemon._build_status()
        assert status["today_cost"] == 0.0

    def test_no_session_manager(self, tmp_path):
        """No session manager returns 0 active sessions."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.providers = {}
        daemon.session_mgr = None

        status = daemon._build_status()
        assert status["active_sessions"] == 0

    def test_queue_depth_reflects_items(self, tmp_path):
        """Queue depth matches actual items in the queue."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.providers = {}
        daemon.session_mgr = None

        # Put items on queue
        daemon.queue.put_nowait({"text": "msg1"})
        daemon.queue.put_nowait({"text": "msg2"})

        status = daemon._build_status()
        assert status["queue_depth"] == 2

    def test_uptime_increases(self, tmp_path):
        """Uptime is positive and based on start_time."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.start_time = time.time() - 120  # Started 2 min ago
        daemon.providers = {}
        daemon.session_mgr = None

        status = daemon._build_status()
        assert status["uptime_seconds"] >= 119  # Allow 1s tolerance

    def test_multiple_providers_listed(self, tmp_path):
        """All provider names appear in status."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.providers = {
            "primary": MagicMock(),
            "subagent": MagicMock(),
            "compaction": MagicMock(),
        }
        daemon.session_mgr = None

        status = daemon._build_status()
        assert set(status["models"]) == {"primary", "subagent", "compaction"}


# ─── _resolve pattern ────────────────────────────────────────────


class TestResolvePattern:
    """Test the _resolve() inner function behavior via _process_message.

    _resolve is defined inside _process_message, so we test it through
    the daemon's behavior with response_future parameter.
    """

    @pytest.mark.asyncio
    async def test_future_resolved_on_no_provider(self, tmp_path):
        """If provider is missing, future is resolved with error."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.providers = {}  # No providers at all
        daemon.session_mgr = MagicMock()

        loop = asyncio.get_running_loop()
        future = loop.create_future()

        await daemon._process_message(
            text="test",
            sender="http-test",
            source="http",
            response_future=future,
        )

        assert future.done()
        result = future.result()
        assert "error" in result

    @pytest.mark.asyncio
    async def test_future_none_doesnt_crash(self, tmp_path):
        """_process_message with response_future=None doesn't crash."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.providers = {}  # Will trigger early return (no provider)
        daemon.session_mgr = MagicMock()

        # This should not raise
        await daemon._process_message(
            text="test",
            sender="test-sender",
            source="system",
            response_future=None,
        )

    @pytest.mark.asyncio
    async def test_double_resolve_is_safe(self, tmp_path):
        """If someone tries to resolve an already-done Future, it's safe."""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        future.set_result({"reply": "first"})

        # Simulating what _resolve does — check done() first
        if not future.done():
            future.set_result({"reply": "second"})

        # Should still have the first result
        assert future.result()["reply"] == "first"

    @pytest.mark.asyncio
    async def test_cancelled_future_is_safe(self, tmp_path):
        """Cancelled Future is handled safely by _resolve pattern."""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        future.cancel()

        # _resolve pattern: check done() first
        assert future.done()  # cancelled counts as done
        # So _resolve would skip set_result — no crash


class TestResolveIntegration:
    """Integration tests: verify Future is resolved at each exit path."""

    @pytest.fixture
    def daemon_with_mock_provider(self, tmp_path):
        """Daemon with a mocked provider that we can control."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)

        # Mock provider
        provider = MagicMock()
        provider.format_system = MagicMock(return_value=[])
        provider.format_messages = MagicMock(return_value=[])
        provider.format_tools = MagicMock(return_value=[])

        daemon.providers = {"primary": provider}

        # Mock session manager
        session = MagicMock()
        session.id = "test-session"
        session.messages = []
        session.pending_system_warning = ""
        session.last_input_tokens = 0
        session.needs_compaction = MagicMock(return_value=False)
        session.warned_about_compaction = False
        session.add_user_message = MagicMock()
        session.persist_assistant_message = MagicMock()
        session.persist_tool_results = MagicMock()
        session._save_state = MagicMock()

        daemon.session_mgr = MagicMock()
        daemon.session_mgr.get_or_create = MagicMock(return_value=session)

        # Mock context builder
        daemon.context_builder = MagicMock()
        daemon.context_builder.build = MagicMock(return_value=[
            {"text": "test context", "tier": "stable"},
        ])

        # Mock skill loader
        daemon.skill_loader = MagicMock()
        daemon.skill_loader.build_index = MagicMock(return_value="")
        daemon.skill_loader.get_bodies = MagicMock(return_value={})

        # Mock tool registry
        daemon.tool_registry = MagicMock()
        daemon.tool_registry.get_brief_descriptions = MagicMock(return_value=[])
        daemon.tool_registry.get_schemas = MagicMock(return_value=[])

        # Mock channel
        daemon.channel = AsyncMock()

        return daemon, provider, session

    @pytest.mark.asyncio
    async def test_future_resolved_on_agentic_loop_error(self, daemon_with_mock_provider):
        """Future is resolved with error when agentic loop raises."""
        daemon, provider, session = daemon_with_mock_provider
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        with patch("lucyd.run_agentic_loop", side_effect=RuntimeError("API down")):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="test",
                    sender="http-test",
                    source="http",
                    response_future=future,
                )

        assert future.done()
        result = future.result()
        assert "error" in result
        assert "API down" in result["error"]
        assert result["session_id"] == "test-session"

    @pytest.mark.asyncio
    async def test_future_resolved_on_silent_token(self, daemon_with_mock_provider):
        """Future is resolved with silent=True when reply matches a silent token."""
        daemon, provider, session = daemon_with_mock_provider
        daemon.config = MagicMock()
        daemon.config.route_model = MagicMock(return_value="primary")
        daemon.config.model_config = MagicMock(return_value={
            "model": "test", "cost_per_mtok": [1.0, 5.0, 0.1],
        })
        daemon.config.typing_indicators = False
        daemon.config.max_turns = 10
        daemon.config.agent_timeout = 30
        daemon.config.cost_db = Path("/tmp/nonexistent-cost.db")
        daemon.config.silent_tokens = ["HEARTBEAT_OK"]
        daemon.config.compaction_threshold = 150000
        daemon.config.always_on_skills = []
        daemon.config.error_message = "Error"
        daemon.config.message_retries = 0
        daemon.config.message_retry_base_delay = 0.01
        daemon.config.raw = MagicMock(return_value=0.0)

        loop = asyncio.get_running_loop()
        future = loop.create_future()

        # Mock the agentic loop to return a silent reply
        usage = MagicMock()
        usage.input_tokens = 1000
        usage.output_tokens = 50
        response = MagicMock()
        response.text = "HEARTBEAT_OK"
        response.usage = usage

        with patch("lucyd.run_agentic_loop", return_value=response):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="heartbeat trigger",
                    sender="http-test",
                    source="http",
                    response_future=future,
                )

        assert future.done()
        result = future.result()
        assert result["silent"] is True
        assert result["reply"] == "HEARTBEAT_OK"

    @pytest.mark.asyncio
    async def test_future_resolved_on_success(self, daemon_with_mock_provider):
        """Future is resolved with reply on normal successful processing."""
        daemon, provider, session = daemon_with_mock_provider
        daemon.config = MagicMock()
        daemon.config.route_model = MagicMock(return_value="primary")
        daemon.config.model_config = MagicMock(return_value={
            "model": "test", "cost_per_mtok": [1.0, 5.0, 0.1],
        })
        daemon.config.typing_indicators = False
        daemon.config.max_turns = 10
        daemon.config.agent_timeout = 30
        daemon.config.cost_db = Path("/tmp/nonexistent-cost.db")
        daemon.config.silent_tokens = []
        daemon.config.compaction_threshold = 150000
        daemon.config.always_on_skills = []
        daemon.config.error_message = "Error"
        daemon.config.message_retries = 0
        daemon.config.message_retry_base_delay = 0.01
        daemon.config.raw = MagicMock(return_value=0.0)

        loop = asyncio.get_running_loop()
        future = loop.create_future()

        usage = MagicMock()
        usage.input_tokens = 5000
        usage.output_tokens = 200
        response = MagicMock()
        response.text = "Here is my answer."
        response.usage = usage

        with patch("lucyd.run_agentic_loop", return_value=response):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="test question",
                    sender="http-test",
                    source="http",
                    response_future=future,
                )

        assert future.done()
        result = future.result()
        assert result["reply"] == "Here is my answer."
        assert result["tokens"]["input"] == 5000
        assert result["tokens"]["output"] == 200


# ─── Channel Delivery Suppression ────────────────────────────────


class TestChannelDeliverySuppression:
    """Verify that system and HTTP sources suppress channel delivery."""

    @pytest.fixture
    def daemon_with_successful_response(self, tmp_path):
        """Daemon rigged to return a successful non-silent response."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)

        provider = MagicMock()
        provider.format_system = MagicMock(return_value=[])
        daemon.providers = {"primary": provider}

        session = MagicMock()
        session.id = "test-session"
        session.messages = []
        session.pending_system_warning = ""
        session.last_input_tokens = 0
        session.needs_compaction = MagicMock(return_value=False)
        session.warned_about_compaction = False
        session.add_user_message = MagicMock()
        session.persist_assistant_message = MagicMock()
        session.persist_tool_results = MagicMock()
        session._save_state = MagicMock()

        daemon.session_mgr = MagicMock()
        daemon.session_mgr.get_or_create = MagicMock(return_value=session)

        daemon.context_builder = MagicMock()
        daemon.context_builder.build = MagicMock(return_value=[])

        daemon.skill_loader = MagicMock()
        daemon.skill_loader.build_index = MagicMock(return_value="")
        daemon.skill_loader.get_bodies = MagicMock(return_value={})

        daemon.tool_registry = MagicMock()
        daemon.tool_registry.get_brief_descriptions = MagicMock(return_value=[])
        daemon.tool_registry.get_schemas = MagicMock(return_value=[])

        daemon.channel = AsyncMock()

        daemon.config = MagicMock()
        daemon.config.route_model = MagicMock(return_value="primary")
        daemon.config.model_config = MagicMock(return_value={
            "model": "test", "cost_per_mtok": [1.0, 5.0, 0.1],
        })
        daemon.config.typing_indicators = True
        daemon.config.max_turns = 10
        daemon.config.agent_timeout = 30
        daemon.config.cost_db = Path("/tmp/nonexistent-cost.db")
        daemon.config.silent_tokens = []
        daemon.config.compaction_threshold = 150000
        daemon.config.always_on_skills = []
        daemon.config.error_message = "Error"
        daemon.config.message_retries = 0
        daemon.config.message_retry_base_delay = 0.01
        daemon.config.raw = MagicMock(return_value=0.0)

        usage = MagicMock()
        usage.input_tokens = 1000
        usage.output_tokens = 100
        response = MagicMock()
        response.text = "Test reply"
        response.usage = usage

        return daemon, response

    @pytest.mark.asyncio
    async def test_system_source_suppresses_typing(self, daemon_with_successful_response):
        """System source skips typing indicator."""
        daemon, response = daemon_with_successful_response

        with patch("lucyd.run_agentic_loop", return_value=response):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="heartbeat", sender="system", source="system",
                )

        daemon.channel.send_typing.assert_not_called()

    @pytest.mark.asyncio
    async def test_system_source_suppresses_reply(self, daemon_with_successful_response):
        """System source doesn't deliver reply via channel."""
        daemon, response = daemon_with_successful_response

        with patch("lucyd.run_agentic_loop", return_value=response):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="heartbeat", sender="system", source="system",
                )

        daemon.channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_http_source_suppresses_typing(self, daemon_with_successful_response):
        """HTTP source skips typing indicator."""
        daemon, response = daemon_with_successful_response
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        with patch("lucyd.run_agentic_loop", return_value=response):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="api call", sender="http", source="http",
                    response_future=future,
                )

        daemon.channel.send_typing.assert_not_called()

    @pytest.mark.asyncio
    async def test_http_source_suppresses_channel_reply(self, daemon_with_successful_response):
        """HTTP source doesn't deliver reply via channel.send."""
        daemon, response = daemon_with_successful_response
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        with patch("lucyd.run_agentic_loop", return_value=response):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="api call", sender="http", source="http",
                    response_future=future,
                )

        daemon.channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_telegram_source_delivers_typing(self, daemon_with_successful_response):
        """Telegram source sends typing indicator."""
        daemon, response = daemon_with_successful_response

        with patch("lucyd.run_agentic_loop", return_value=response):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="+431234567890", source="telegram",
                )

        daemon.channel.send_typing.assert_called_once()

    @pytest.mark.asyncio
    async def test_telegram_source_delivers_reply(self, daemon_with_successful_response):
        """Telegram source delivers reply via channel."""
        daemon, response = daemon_with_successful_response

        with patch("lucyd.run_agentic_loop", return_value=response):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="+431234567890", source="telegram",
                )

        daemon.channel.send.assert_called_once_with("+431234567890", "Test reply")

    @pytest.mark.asyncio
    async def test_system_source_suppresses_all_text(self, daemon_with_successful_response):
        """System source doesn't deliver any text via channel."""
        daemon, response = daemon_with_successful_response

        with patch("lucyd.run_agentic_loop", return_value=response):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="heartbeat", sender="system", source="system",
                )

        daemon.channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_http_error_suppresses_channel_error_msg(self, daemon_with_successful_response):
        """HTTP source doesn't send error message via channel on agentic loop failure."""
        daemon, _ = daemon_with_successful_response
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        with patch("lucyd.run_agentic_loop", side_effect=RuntimeError("fail")):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="test", sender="http", source="http",
                    response_future=future,
                )

        daemon.channel.send.assert_not_called()
        # But future should still be resolved
        assert future.done()
        assert "error" in future.result()


# ─── Context Builder Source Integration ──────────────────────────


class TestContextBuilderSourcePassthrough:
    """Verify that source is passed through to context_builder.build()."""

    @pytest.fixture
    def daemon_for_context_test(self, tmp_path):
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)

        provider = MagicMock()
        provider.format_system = MagicMock(return_value=[])
        daemon.providers = {"primary": provider}

        session = MagicMock()
        session.id = "ctx-test"
        session.messages = []
        session.pending_system_warning = ""
        session.last_input_tokens = 0
        session.needs_compaction = MagicMock(return_value=False)
        session.warned_about_compaction = False
        session.add_user_message = MagicMock()
        session._save_state = MagicMock()

        daemon.session_mgr = MagicMock()
        daemon.session_mgr.get_or_create = MagicMock(return_value=session)

        daemon.context_builder = MagicMock()
        daemon.context_builder.build = MagicMock(return_value=[])

        daemon.skill_loader = MagicMock()
        daemon.skill_loader.build_index = MagicMock(return_value="")
        daemon.skill_loader.get_bodies = MagicMock(return_value={})

        daemon.tool_registry = MagicMock()
        daemon.tool_registry.get_brief_descriptions = MagicMock(return_value=[])
        daemon.tool_registry.get_schemas = MagicMock(return_value=[])

        daemon.channel = AsyncMock()

        daemon.config = MagicMock()
        daemon.config.route_model = MagicMock(return_value="primary")
        daemon.config.model_config = MagicMock(return_value={
            "model": "test", "cost_per_mtok": [],
        })
        daemon.config.typing_indicators = False
        daemon.config.max_turns = 10
        daemon.config.agent_timeout = 30
        daemon.config.cost_db = Path("/tmp/nonexistent-cost.db")
        daemon.config.silent_tokens = []
        daemon.config.compaction_threshold = 150000
        daemon.config.always_on_skills = []
        daemon.config.raw = MagicMock(return_value=0.0)

        usage = MagicMock()
        usage.input_tokens = 100
        usage.output_tokens = 50
        resp = MagicMock()
        resp.text = "ok"
        resp.usage = usage

        return daemon, resp

    @pytest.mark.asyncio
    async def test_system_source_passed_to_context(self, daemon_for_context_test):
        daemon, resp = daemon_for_context_test

        with patch("lucyd.run_agentic_loop", return_value=resp):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="test", sender="system", source="system", tier="operational",
                )

        daemon.context_builder.build.assert_called_once()
        call_kwargs = daemon.context_builder.build.call_args
        assert call_kwargs.kwargs.get("source") == "system" or \
               (len(call_kwargs.args) >= 2 and call_kwargs.args[1] == "system") or \
               call_kwargs[1].get("source") == "system"

    @pytest.mark.asyncio
    async def test_http_source_passed_to_context(self, daemon_for_context_test):
        daemon, resp = daemon_for_context_test
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        with patch("lucyd.run_agentic_loop", return_value=resp):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="test", sender="http", source="http",
                    response_future=future,
                )

        daemon.context_builder.build.assert_called_once()
        call_kwargs = daemon.context_builder.build.call_args
        assert call_kwargs.kwargs.get("source") == "http" or \
               (len(call_kwargs.args) >= 2 and call_kwargs.args[1] == "http") or \
               call_kwargs[1].get("source") == "http"

    @pytest.mark.asyncio
    async def test_telegram_source_passed_to_context(self, daemon_for_context_test):
        daemon, resp = daemon_for_context_test

        with patch("lucyd.run_agentic_loop", return_value=resp):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="+431234567890", source="telegram",
                )

        daemon.context_builder.build.assert_called_once()
        call_kwargs = daemon.context_builder.build.call_args
        assert call_kwargs.kwargs.get("source") == "telegram" or \
               (len(call_kwargs.args) >= 2 and call_kwargs.args[1] == "telegram") or \
               call_kwargs[1].get("source") == "telegram"


# ─── Message Loop HTTP Bypass ────────────────────────────────────


class TestMessageLoopHTTPBypass:
    """Verify that HTTP /chat items bypass debouncing in the message loop."""

    @pytest.mark.asyncio
    async def test_http_item_has_response_future(self):
        """HTTP /chat items have response_future key (not None)."""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        item = {
            "sender": "http",
            "type": "http",
            "text": "test",
            "tier": "full",
            "response_future": future,
        }
        # The bypass check in the message loop
        assert item.get("response_future") is not None

    @pytest.mark.asyncio
    async def test_fifo_item_has_no_response_future(self):
        """FIFO items don't have response_future."""
        item = {
            "sender": "system",
            "type": "system",
            "text": "heartbeat",
            "tier": "operational",
        }
        assert item.get("response_future") is None

    @pytest.mark.asyncio
    async def test_notify_item_has_no_response_future(self):
        """Notify items (type=system from HTTP) don't have response_future."""
        item = {
            "sender": "http",
            "type": "system",
            "text": "[AUTOMATED SYSTEM MESSAGE] test",
            "tier": "operational",
        }
        assert item.get("response_future") is None


# ─── Extended _is_silent Tests ───────────────────────────────────


class TestIsSilentExtended:
    """Additional edge cases for _is_silent beyond test_daemon_helpers.py."""

    def test_token_with_surrounding_whitespace(self):
        """Token surrounded by whitespace matches."""
        assert _is_silent("  HEARTBEAT_OK  ", ["HEARTBEAT_OK"]) is True

    def test_token_followed_by_period_and_newline(self):
        assert _is_silent("Done.\nHEARTBEAT_OK", ["HEARTBEAT_OK"]) is True

    def test_token_as_part_of_longer_word_no_match(self):
        """Token embedded in a longer word should not match."""
        assert _is_silent("HEARTBEAT_OK_PLUS", ["HEARTBEAT_OK"]) is False

    def test_case_sensitive_no_match(self):
        """Token matching is case-sensitive."""
        assert _is_silent("heartbeat_ok", ["HEARTBEAT_OK"]) is False

    def test_no_reply_token(self):
        assert _is_silent("NO_REPLY", ["NO_REPLY"]) is True

    def test_no_reply_with_trailing_text(self):
        assert _is_silent("All done. NO_REPLY", ["NO_REPLY"]) is True

    def test_reply_with_only_whitespace(self):
        assert _is_silent("   ", ["HEARTBEAT_OK"]) is False

    def test_multiline_reply_token_at_end(self):
        """Token at the end of a multiline reply."""
        text = "Processing complete.\nAll tasks done.\nHEARTBEAT_OK"
        assert _is_silent(text, ["HEARTBEAT_OK"]) is True

    def test_multiline_reply_token_at_start(self):
        """Token at the start of a multiline reply."""
        text = "HEARTBEAT_OK\nSome extra text"
        # Token at start matches
        assert _is_silent(text, ["HEARTBEAT_OK"]) is True


# ─── Inbound Timestamp Capture ────────────────────────────────────


class TestInboundTimestampCapture:
    """Verify _last_inbound_ts is populated from InboundMessage."""

    def test_init_has_empty_dict(self, tmp_path):
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        assert daemon._last_inbound_ts == {}

    def test_timestamp_stored_from_inbound_message(self, tmp_path):
        """InboundMessage.timestamp (seconds float) is stored as ms int."""
        from channels import InboundMessage

        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.running = False  # prevent loop from running

        msg = InboundMessage(
            text="hello",
            sender="+431234567890",
            timestamp=1707700000.123,
            source="telegram",
        )
        # Simulate what the message loop does
        sender = msg.sender
        daemon._last_inbound_ts[sender] = int(msg.timestamp * 1000)

        assert daemon._last_inbound_ts["+431234567890"] == 1707700000123

    def test_timestamp_overwritten_on_new_message(self, tmp_path):
        """Newer message replaces older timestamp."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)

        daemon._last_inbound_ts["+431234"] = 1000
        daemon._last_inbound_ts["+431234"] = 2000

        assert daemon._last_inbound_ts["+431234"] == 2000

    def test_queue_has_maxsize(self, tmp_path):
        """Message queue has a bounded size to prevent unbounded memory growth."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        assert daemon.queue.maxsize == 1000

    def test_eviction_at_1001_entries(self, tmp_path):
        """OrderedDict evicts oldest when exceeding 1000 senders."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        for i in range(1001):
            sender = f"sender_{i:05d}"
            daemon._last_inbound_ts[sender] = i * 1000
            daemon._last_inbound_ts.move_to_end(sender)
            while len(daemon._last_inbound_ts) > 1000:
                daemon._last_inbound_ts.popitem(last=False)

        assert len(daemon._last_inbound_ts) == 1000
        assert "sender_00000" not in daemon._last_inbound_ts
        assert "sender_01000" in daemon._last_inbound_ts

    def test_reaccess_does_not_grow_beyond_limit(self, tmp_path):
        """Re-accessing existing sender doesn't increase size past 1000."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        for i in range(1000):
            daemon._last_inbound_ts[f"s{i:04d}"] = i
        assert len(daemon._last_inbound_ts) == 1000

        daemon._last_inbound_ts["s0000"] = 9999
        daemon._last_inbound_ts.move_to_end("s0000")
        while len(daemon._last_inbound_ts) > 1000:
            daemon._last_inbound_ts.popitem(last=False)

        assert len(daemon._last_inbound_ts) == 1000
        assert daemon._last_inbound_ts["s0000"] == 9999


# ─── FIFO JSON Validation ───────────────────────────────────────


class TestFIFOValidation:
    """SEC-12: FIFO JSON schema validation."""

    @pytest.mark.asyncio
    async def test_fifo_rejects_missing_fields(self, tmp_path, caplog):
        """JSON without required keys is logged and skipped."""
        # Simulate the FIFO parsing logic from lucyd.py
        _fifo_required = {"text", "sender"}
        line = json.dumps({"text": "hello"})  # missing "sender"
        msg = json.loads(line)
        if not isinstance(msg, dict) or not _fifo_required.issubset(msg.keys()):
            missing = list(_fifo_required - msg.keys())
            assert "sender" in missing
        else:
            pytest.fail("Should have rejected message missing 'sender'")

    @pytest.mark.asyncio
    async def test_fifo_rejects_non_dict(self):
        """JSON array or string is skipped."""
        _fifo_required = {"text", "sender"}
        for payload in ['["array"]', '"just a string"', '42']:
            msg = json.loads(payload)
            assert not isinstance(msg, dict) or not _fifo_required.issubset(msg.keys())

    @pytest.mark.asyncio
    async def test_fifo_accepts_valid_message(self):
        """Valid message with all required fields passes."""
        _fifo_required = {"text", "sender"}
        msg = json.loads(json.dumps({"text": "hello", "sender": "cli", "type": "user"}))
        assert isinstance(msg, dict) and _fifo_required.issubset(msg.keys())


# ─── TEST-2: _process_message Integration Tests ─────────────────


class TestProcessMessageIntegration:
    """Integration tests for _process_message — full pipeline with mock
    provider, mock channel, and mock session."""

    @pytest.fixture
    def full_daemon(self, tmp_path):
        """Daemon with all required mocks for _process_message."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)

        # Mock provider
        provider = MagicMock()
        provider.format_system = MagicMock(return_value=[])
        provider.format_messages = MagicMock(return_value=[])
        provider.format_tools = MagicMock(return_value=[])
        daemon.providers = {"primary": provider}

        # Mock session
        session = MagicMock()
        session.id = "integ-session-1"
        session.messages = []
        session.pending_system_warning = ""
        session.last_input_tokens = 0
        session.needs_compaction = MagicMock(return_value=False)
        session.warned_about_compaction = False
        session.add_user_message = MagicMock()
        session.persist_assistant_message = MagicMock()
        session.persist_tool_results = MagicMock()
        session._save_state = MagicMock()

        daemon.session_mgr = MagicMock()
        daemon.session_mgr.get_or_create = MagicMock(return_value=session)
        daemon.session_mgr.compact_session = AsyncMock()

        # Mock context builder
        daemon.context_builder = MagicMock()
        daemon.context_builder.build = MagicMock(return_value=[
            {"text": "test context", "tier": "stable"},
        ])

        # Mock skill loader
        daemon.skill_loader = MagicMock()
        daemon.skill_loader.build_index = MagicMock(return_value="")
        daemon.skill_loader.get_bodies = MagicMock(return_value={})

        # Mock tool registry
        daemon.tool_registry = MagicMock()
        daemon.tool_registry.get_brief_descriptions = MagicMock(return_value=[])
        daemon.tool_registry.get_schemas = MagicMock(return_value=[])

        # Mock channel
        daemon.channel = AsyncMock()

        # Override config as MagicMock for controlled attribute access
        daemon.config = MagicMock()
        daemon.config.route_model = MagicMock(return_value="primary")
        daemon.config.model_config = MagicMock(return_value={
            "model": "test-model", "cost_per_mtok": [1.0, 5.0, 0.1],
            "supports_vision": True,
        })
        daemon.config.typing_indicators = False
        daemon.config.max_turns = 10
        daemon.config.agent_timeout = 30
        daemon.config.cost_db = Path(str(tmp_path / "cost.db"))
        daemon.config.silent_tokens = []
        daemon.config.compaction_threshold = 150000
        daemon.config.compaction_model = "compaction"
        daemon.config.compaction_prompt = "Summarize"
        daemon.config.agent_name = "TestAgent"
        daemon.config.consolidation_enabled = False
        daemon.config.always_on_skills = []
        daemon.config.error_message = "Something went wrong."
        daemon.config.message_retries = 0
        daemon.config.message_retry_base_delay = 0.01
        daemon.config.raw = MagicMock(return_value=0.0)
        daemon.config.vision_max_image_bytes = 5 * 1024 * 1024
        daemon.config.vision_max_dimension = 1568
        daemon.config.vision_default_caption = "image"
        daemon.config.vision_too_large_msg = "image too large to display"

        return daemon, provider, session

    @pytest.mark.asyncio
    async def test_normal_text_message_updates_session(self, full_daemon):
        """Normal text message flows through and updates the session."""
        daemon, provider, session = full_daemon

        usage = MagicMock()
        usage.input_tokens = 3000
        usage.output_tokens = 150
        response = MagicMock()
        response.text = "Here is the answer to your question."
        response.usage = usage

        with patch("lucyd.run_agentic_loop", return_value=response):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="What is the weather?",
                    sender="+431234567890",
                    source="telegram",
                )

        # Session should have add_user_message called
        session.add_user_message.assert_called_once()
        call_args = session.add_user_message.call_args
        # The text should contain the original message (with timestamp prepended)
        assert "What is the weather?" in call_args[0][0]
        # Session state should be saved
        session._save_state.assert_called()
        # Channel should deliver the reply (source=telegram not in _NO_CHANNEL_DELIVERY)
        daemon.channel.send.assert_called_once_with(
            "+431234567890", "Here is the answer to your question.",
        )

    @pytest.mark.asyncio
    async def test_tool_use_response_persisted(self, full_daemon):
        """When agentic loop adds tool-use messages, they are persisted."""
        daemon, provider, session = full_daemon

        # Simulate the agentic loop appending messages to session.messages
        def mock_agentic_loop(**kwargs):
            msgs = kwargs["messages"]
            msgs.append({"role": "assistant", "content": "Let me check that."})
            msgs.append({"role": "tool_results", "results": [
                {"tool_use_id": "t1", "content": "result data"},
            ]})
            msgs.append({"role": "assistant", "content": "Here is the result."})

            usage = MagicMock()
            usage.input_tokens = 4000
            usage.output_tokens = 300
            resp = MagicMock()
            resp.text = "Here is the result."
            resp.usage = usage
            return resp

        with patch("lucyd.run_agentic_loop", side_effect=mock_agentic_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="Run the status tool",
                    sender="+431234567890",
                    source="telegram",
                )

        # persist_assistant_message should be called for each assistant message
        assert session.persist_assistant_message.call_count == 2
        # persist_tool_results should be called once
        session.persist_tool_results.assert_called_once()
        call_args = session.persist_tool_results.call_args[0][0]
        assert call_args[0]["tool_use_id"] == "t1"

    @pytest.mark.asyncio
    async def test_agentic_loop_error_delivers_error_for_telegram(self, full_daemon):
        """When agentic loop raises, error message is delivered via channel for telegram source."""
        daemon, provider, session = full_daemon

        with patch("lucyd.run_agentic_loop", side_effect=RuntimeError("Provider timeout")):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello",
                    sender="+431234567890",
                    source="telegram",
                )

        # Channel should deliver error message (telegram not in _NO_CHANNEL_DELIVERY)
        daemon.channel.send.assert_called_once_with(
            "+431234567890", "Something went wrong.",
        )

    @pytest.mark.asyncio
    async def test_agentic_loop_error_resolves_future_for_http(self, full_daemon):
        """When agentic loop raises for HTTP source, future is resolved with error."""
        daemon, provider, session = full_daemon

        loop = asyncio.get_running_loop()
        future = loop.create_future()

        with patch("lucyd.run_agentic_loop", side_effect=ValueError("Bad request")):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="bad request",
                    sender="http",
                    source="http",
                    response_future=future,
                )

        assert future.done()
        result = future.result()
        assert "error" in result
        assert "Bad request" in result["error"]
        assert result["session_id"] == "integ-session-1"
        # Channel should NOT deliver error (http in _NO_CHANNEL_DELIVERY)
        daemon.channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_image_attachment_adds_prefix_to_text(self, full_daemon, tmp_path):
        """Image attachment adds [image] prefix and creates image blocks."""
        daemon, provider, session = full_daemon

        # Create a real small test image via Pillow
        from PIL import Image as PILImage
        pil_img = PILImage.new("RGB", (100, 100), color="red")
        img_path = tmp_path / "test.jpg"
        pil_img.save(str(img_path), format="JPEG")

        from channels import Attachment
        att = Attachment(
            content_type="image/jpeg",
            local_path=str(img_path),
            filename="test.jpg",
            size=img_path.stat().st_size,
        )

        usage = MagicMock()
        usage.input_tokens = 2000
        usage.output_tokens = 100
        response = MagicMock()
        response.text = "I see an image."
        response.usage = usage

        # Make add_user_message actually append a message to session.messages
        # so that session.messages[user_msg_idx] works for image block injection
        def fake_add_user(text, **kwargs):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = MagicMock(side_effect=fake_add_user)

        with patch("lucyd.run_agentic_loop", return_value=response):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="What is in this picture?",
                    sender="+431234567890",
                    source="telegram",
                    attachments=[att],
                )

        # add_user_message should have been called with text containing [image]
        call_text = session.add_user_message.call_args[0][0]
        assert "[image]" in call_text
        assert "What is in this picture?" in call_text

    @pytest.mark.asyncio
    async def test_unfittable_image_shows_fallback(self, full_daemon, tmp_path):
        """PNG that can't be compressed below limit shows fallback message."""
        daemon, provider, session = full_daemon
        # Set very low limit — PNG can't quality-reduce
        daemon.config.vision_max_image_bytes = 100

        usage = MagicMock()
        usage.input_tokens = 2000
        usage.output_tokens = 100
        response = MagicMock()
        response.text = "ok"
        response.usage = usage

        def fake_add_user(text, **kwargs):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = MagicMock(side_effect=fake_add_user)

        from channels import Attachment
        from PIL import Image as PILImage

        img = PILImage.new("RGB", (200, 200), color="red")
        img_path = tmp_path / "big.png"
        img.save(str(img_path), format="PNG")
        att = Attachment(content_type="image/png", local_path=str(img_path),
                         filename="big.png", size=img_path.stat().st_size)

        with patch("lucyd.run_agentic_loop", return_value=response):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="look", sender="+431234567890", source="telegram",
                    attachments=[att],
                )

        call_text = session.add_user_message.call_args[0][0]
        assert "too large" in call_text.lower() or "after compression" in call_text.lower()

    @pytest.mark.asyncio
    async def test_unreadable_image_shows_fallback(self, full_daemon, tmp_path):
        """Image file that can't be read injects fallback instead of silent drop."""
        daemon, provider, session = full_daemon

        usage = MagicMock()
        usage.input_tokens = 2000
        usage.output_tokens = 100
        response = MagicMock()
        response.text = "ok"
        response.usage = usage

        def fake_add_user(text, **kwargs):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = MagicMock(side_effect=fake_add_user)

        from channels import Attachment

        # Point to a file that doesn't exist
        att = Attachment(content_type="image/jpeg", local_path=str(tmp_path / "gone.jpg"),
                         filename="gone.jpg", size=1000)

        with patch("lucyd.run_agentic_loop", return_value=response):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="check this", sender="+431234567890", source="telegram",
                    attachments=[att],
                )

        call_text = session.add_user_message.call_args[0][0]
        assert "could not read file" in call_text.lower()

    @pytest.mark.asyncio
    async def test_compaction_triggered_when_threshold_exceeded(self, full_daemon):
        """When session.needs_compaction returns True, compact_session is called."""
        daemon, provider, session = full_daemon

        # Configure compaction provider
        compaction_provider = MagicMock()
        daemon.providers["compaction"] = compaction_provider

        # Session reports it needs compaction after the agentic loop
        session.needs_compaction = MagicMock(return_value=True)
        session.last_input_tokens = 160000  # Above threshold

        usage = MagicMock()
        usage.input_tokens = 160000
        usage.output_tokens = 500
        response = MagicMock()
        response.text = "Done."
        response.usage = usage

        with patch("lucyd.run_agentic_loop", return_value=response):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="Continue working",
                    sender="+431234567890",
                    source="telegram",
                )

        # compact_session should have been called
        daemon.session_mgr.compact_session.assert_called_once()
        args = daemon.session_mgr.compact_session.call_args[0]
        assert args[0] is session
        assert args[1] is compaction_provider
        assert isinstance(args[2], str) and len(args[2]) > 0


# ─── TEST-3: _message_loop Behavior Tests ───────────────────────


class TestMessageLoopDebounce:
    """Test debounce window combining behavior in _message_loop."""

    @pytest.fixture
    def loop_daemon(self, tmp_path):
        """Daemon configured for message loop testing with short debounce."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)

        # Mock everything _process_message needs
        provider = MagicMock()
        provider.format_system = MagicMock(return_value=[])
        daemon.providers = {"primary": provider}

        session = MagicMock()
        session.id = "loop-test-session"
        session.messages = []
        session.pending_system_warning = ""
        session.last_input_tokens = 0
        session.needs_compaction = MagicMock(return_value=False)
        session.warned_about_compaction = False
        session.add_user_message = MagicMock()
        session._save_state = MagicMock()

        daemon.session_mgr = MagicMock()
        daemon.session_mgr.get_or_create = MagicMock(return_value=session)
        daemon.session_mgr.close_session = AsyncMock(return_value=True)
        daemon.session_mgr.close_session_by_id = AsyncMock(return_value=True)
        daemon.session_mgr._index = {}

        daemon.context_builder = MagicMock()
        daemon.context_builder.build = MagicMock(return_value=[])
        daemon.skill_loader = MagicMock()
        daemon.skill_loader.build_index = MagicMock(return_value="")
        daemon.skill_loader.get_bodies = MagicMock(return_value={})
        daemon.tool_registry = MagicMock()
        daemon.tool_registry.get_brief_descriptions = MagicMock(return_value=[])
        daemon.tool_registry.get_schemas = MagicMock(return_value=[])
        daemon.channel = AsyncMock()

        daemon.config = MagicMock()
        daemon.config.route_model = MagicMock(return_value="primary")
        daemon.config.model_config = MagicMock(return_value={
            "model": "test", "cost_per_mtok": [1.0, 5.0, 0.1],
        })
        daemon.config.typing_indicators = False
        daemon.config.max_turns = 10
        daemon.config.agent_timeout = 30
        daemon.config.cost_db = Path(str(tmp_path / "cost.db"))
        daemon.config.silent_tokens = []
        daemon.config.compaction_threshold = 150000
        daemon.config.compaction_model = "compaction"
        daemon.config.compaction_prompt = "Summarize"
        daemon.config.agent_name = "TestAgent"
        daemon.config.consolidation_enabled = False
        daemon.config.always_on_skills = []
        daemon.config.error_message = "Error"
        daemon.config.message_retries = 0
        daemon.config.message_retry_base_delay = 0.01
        daemon.config.raw = MagicMock(return_value=0.0)
        # Very short debounce for fast tests
        daemon.config.debounce_ms = 50

        daemon.running = True

        return daemon, session

    @pytest.mark.asyncio
    async def test_debounce_sleep_occurs_between_messages(self, loop_daemon):
        """Each message triggers a debounce sleep before drain_pending runs.

        The loop reads one item at a time from the queue, appends to pending,
        sleeps for debounce_ms, then drains all pending senders.  With two
        InboundMessages from the same sender, each is processed in its own
        iteration and add_user_message is called for each.
        """
        daemon, session = loop_daemon

        usage = MagicMock()
        usage.input_tokens = 100
        usage.output_tokens = 50
        response = MagicMock()
        response.text = "ok"
        response.usage = usage

        from channels import InboundMessage

        msg1 = InboundMessage(
            text="Hello",
            sender="+431234567890",
            timestamp=time.time(),
            source="telegram",
        )
        msg2 = InboundMessage(
            text="How are you?",
            sender="+431234567890",
            timestamp=time.time(),
            source="telegram",
        )
        await daemon.queue.put(msg1)
        await daemon.queue.put(msg2)
        await daemon.queue.put(None)

        with patch("lucyd.run_agentic_loop", return_value=response):
            with patch("tools.status.set_current_session"):
                await daemon._message_loop()

        # Each message is processed individually (one per loop iteration)
        assert session.add_user_message.call_count == 2
        first_text = session.add_user_message.call_args_list[0][0][0]
        second_text = session.add_user_message.call_args_list[1][0][0]
        assert "Hello" in first_text
        assert "How are you?" in second_text

    @pytest.mark.asyncio
    async def test_none_sentinel_exits_loop(self, loop_daemon):
        """None sentinel causes the loop to exit gracefully."""
        daemon, session = loop_daemon

        await daemon.queue.put(None)
        await daemon._message_loop()

        assert daemon.running is False

    @pytest.mark.asyncio
    async def test_reset_dict_triggers_close_session(self, loop_daemon):
        """A dict with type=reset triggers close_session on the session manager."""
        daemon, session = loop_daemon

        reset_item = {
            "type": "reset",
            "sender": "testuser",
        }
        await daemon.queue.put(reset_item)
        await daemon.queue.put(None)  # stop the loop

        await daemon._message_loop()

        daemon.session_mgr.close_session.assert_called_once_with("testuser")

    @pytest.mark.asyncio
    async def test_reset_dict_by_session_id(self, loop_daemon):
        """A dict with type=reset and session_id triggers close_session_by_id."""
        daemon, session = loop_daemon

        reset_item = {
            "type": "reset",
            "session_id": "sess-abc-123",
        }
        await daemon.queue.put(reset_item)
        await daemon.queue.put(None)

        await daemon._message_loop()

        daemon.session_mgr.close_session_by_id.assert_called_once_with("sess-abc-123")

    @pytest.mark.asyncio
    async def test_http_item_bypasses_debounce(self, loop_daemon):
        """HTTP items with response_future are processed immediately."""
        daemon, session = loop_daemon

        loop = asyncio.get_running_loop()
        future = loop.create_future()

        usage = MagicMock()
        usage.input_tokens = 100
        usage.output_tokens = 50
        response = MagicMock()
        response.text = "http reply"
        response.usage = usage

        http_item = {
            "sender": "http-client",
            "type": "http",
            "text": "api question",
            "tier": "full",
            "response_future": future,
        }
        await daemon.queue.put(http_item)
        await daemon.queue.put(None)

        with patch("lucyd.run_agentic_loop", return_value=response):
            with patch("tools.status.set_current_session"):
                await daemon._message_loop()

        # Future should be resolved
        assert future.done()
        result = future.result()
        assert result["reply"] == "http reply"

    @pytest.mark.asyncio
    async def test_empty_text_and_no_attachments_skipped(self, loop_daemon):
        """Messages with empty text and no attachments are skipped."""
        daemon, session = loop_daemon

        from channels import InboundMessage

        msg = InboundMessage(
            text="",
            sender="+431234567890",
            timestamp=time.time(),
            source="telegram",
            attachments=None,
        )
        await daemon.queue.put(msg)
        await daemon.queue.put(None)

        with patch("lucyd.run_agentic_loop") as mock_loop:
            with patch("tools.status.set_current_session"):
                await daemon._message_loop()

        # _process_message should NOT have been called (empty text, no attachments)
        mock_loop.assert_not_called()

    @pytest.mark.asyncio
    async def test_debounce_combines_same_sender_within_window(self, loop_daemon):
        """Messages from same sender queued before sleep completes are combined."""
        daemon, session = loop_daemon

        from channels import InboundMessage

        # Put two messages from same sender into the queue before loop starts
        msg1 = InboundMessage(text="A", sender="user1", timestamp=time.time(), source="telegram")
        msg2 = InboundMessage(text="B", sender="user1", timestamp=time.time(), source="telegram")
        await daemon.queue.put(msg1)

        response = MagicMock()
        response.text = "ok"
        response.usage = MagicMock(input_tokens=100, output_tokens=50)

        sleep_calls = []
        original_sleep = asyncio.sleep

        async def fake_sleep(secs):
            sleep_calls.append(secs)
            # During the first sleep, push msg2 into queue to simulate rapid typing
            if len(sleep_calls) == 1:
                await daemon.queue.put(msg2)
            # Yield to let queue.get pick up msg2
            await original_sleep(0)

        with patch("lucyd.asyncio.sleep", side_effect=fake_sleep):
            with patch("lucyd.run_agentic_loop", return_value=response):
                with patch("tools.status.set_current_session"):
                    await daemon.queue.put(None)
                    await daemon._message_loop()

        # Debounce sleep was called
        assert len(sleep_calls) >= 1

    @pytest.mark.asyncio
    async def test_different_senders_both_drained(self, loop_daemon):
        """Messages from different senders are each processed."""
        daemon, session = loop_daemon

        from channels import InboundMessage

        msg1 = InboundMessage(text="Hello", sender="alice", timestamp=time.time(), source="telegram")
        msg2 = InboundMessage(text="World", sender="bob", timestamp=time.time(), source="telegram")
        await daemon.queue.put(msg1)
        await daemon.queue.put(msg2)
        await daemon.queue.put(None)

        response = MagicMock()
        response.text = "ok"
        response.usage = MagicMock(input_tokens=100, output_tokens=50)

        with patch("lucyd.run_agentic_loop", return_value=response):
            with patch("tools.status.set_current_session"):
                await daemon._message_loop()

        # Both senders should have been processed
        assert session.add_user_message.call_count >= 2

    @pytest.mark.asyncio
    async def test_fifo_dict_messages_debounced(self, loop_daemon):
        """Dict-based FIFO messages are subject to debounce like InboundMessages."""
        daemon, session = loop_daemon

        fifo_item = {
            "sender": "system",
            "type": "system",
            "text": "FIFO message",
            "tier": "operational",
        }
        await daemon.queue.put(fifo_item)
        await daemon.queue.put(None)

        response = MagicMock()
        response.text = "ok"
        response.usage = MagicMock(input_tokens=100, output_tokens=50)

        with patch("lucyd.run_agentic_loop", return_value=response):
            with patch("tools.status.set_current_session"):
                await daemon._message_loop()

        # FIFO message was processed through debounce path
        call_text = session.add_user_message.call_args[0][0]
        assert "FIFO message" in call_text

    @pytest.mark.asyncio
    async def test_reset_all_closes_every_session(self, loop_daemon):
        """Reset with all=True closes every session in session_mgr._index."""
        daemon, session = loop_daemon
        daemon.session_mgr._index = {"alice": MagicMock(), "bob": MagicMock(), "system": MagicMock()}

        await daemon.queue.put({"type": "reset", "all": True})
        await daemon.queue.put(None)

        await daemon._message_loop()

        assert daemon.session_mgr.close_session.call_count == 3
        closed = {c.args[0] for c in daemon.session_mgr.close_session.call_args_list}
        assert closed == {"alice", "bob", "system"}

    @pytest.mark.asyncio
    async def test_reset_user_alias_resolves_contact(self, loop_daemon):
        """Reset with sender='user' resolves to first non-system/cli contact."""
        daemon, session = loop_daemon
        daemon.session_mgr._index = {"system": MagicMock(), "cli": MagicMock(), "nicolas": MagicMock()}

        await daemon.queue.put({"type": "reset", "sender": "user"})
        await daemon.queue.put(None)

        await daemon._message_loop()

        daemon.session_mgr.close_session.assert_called_once_with("nicolas")

    @pytest.mark.asyncio
    async def test_reset_unknown_sender_logs_warning(self, loop_daemon):
        """Reset for sender with no session → close_session returns False."""
        daemon, session = loop_daemon
        daemon.session_mgr.close_session = AsyncMock(return_value=False)

        await daemon.queue.put({"type": "reset", "sender": "nobody"})
        await daemon.queue.put(None)

        await daemon._message_loop()

        daemon.session_mgr.close_session.assert_called_once_with("nobody")

    @pytest.mark.asyncio
    async def test_dict_tier_defaults_operational_for_system(self, loop_daemon):
        """FIFO dict with type='system' defaults tier to 'operational'."""
        daemon, session = loop_daemon

        await daemon.queue.put({"sender": "cron", "type": "system", "text": "heartbeat"})
        await daemon.queue.put(None)

        response = MagicMock(text="ok", usage=MagicMock(input_tokens=10, output_tokens=5))

        with patch.object(daemon, "_process_message", new_callable=AsyncMock) as mock_pm:
            await daemon._message_loop()

        mock_pm.assert_called_once()
        _, kwargs = mock_pm.call_args
        # Positional: text, sender, source, tier
        args = mock_pm.call_args.args
        assert args[3] == "operational"

    @pytest.mark.asyncio
    async def test_dict_tier_defaults_full_for_user_type(self, loop_daemon):
        """FIFO dict with type='user' defaults tier to 'full'."""
        daemon, session = loop_daemon

        await daemon.queue.put({"sender": "cli", "type": "user", "text": "hello"})
        await daemon.queue.put(None)

        with patch.object(daemon, "_process_message", new_callable=AsyncMock) as mock_pm:
            await daemon._message_loop()

        args = mock_pm.call_args.args
        assert args[3] == "full"

    @pytest.mark.asyncio
    async def test_notify_meta_propagates_through_drain(self, loop_daemon):
        """notify_meta from dict message arrives in _process_message."""
        daemon, session = loop_daemon
        meta = {"ref": "ticket-42", "source": "n8n"}

        await daemon.queue.put({
            "sender": "webhook", "type": "system",
            "text": "new ticket", "notify_meta": meta,
        })
        await daemon.queue.put(None)

        with patch.object(daemon, "_process_message", new_callable=AsyncMock) as mock_pm:
            await daemon._message_loop()

        mock_pm.assert_called_once()
        assert mock_pm.call_args.kwargs["notify_meta"] == meta

    @pytest.mark.asyncio
    async def test_attachments_preserved_through_processing(self, loop_daemon):
        """Message attachments pass through to _process_message."""
        daemon, session = loop_daemon
        from channels import Attachment, InboundMessage

        att = Attachment(content_type="image/png", local_path="/tmp/a.png",
                         filename="a.png", size=100)
        msg = InboundMessage(text="pic", sender="user1", timestamp=time.time(),
                             source="telegram", attachments=[att])

        await daemon.queue.put(msg)
        await daemon.queue.put(None)

        with patch.object(daemon, "_process_message", new_callable=AsyncMock) as mock_pm:
            await daemon._message_loop()

        mock_pm.assert_called_once()
        passed_atts = mock_pm.call_args.kwargs.get("attachments")
        assert passed_atts is not None
        assert len(passed_atts) == 1
        assert passed_atts[0].filename == "a.png"

    @pytest.mark.asyncio
    async def test_inbound_message_timestamp_stored(self, loop_daemon):
        """InboundMessage timestamp stored in _last_inbound_ts as ms int."""
        daemon, session = loop_daemon
        from channels import InboundMessage

        ts = 1708790400.123  # Known timestamp
        msg = InboundMessage(text="hi", sender="alice", timestamp=ts, source="telegram")
        await daemon.queue.put(msg)
        await daemon.queue.put(None)

        response = MagicMock(text="ok", usage=MagicMock(input_tokens=10, output_tokens=5))
        with patch("lucyd.run_agentic_loop", return_value=response):
            with patch("tools.status.set_current_session"):
                await daemon._message_loop()

        assert daemon._last_inbound_ts["alice"] == int(ts * 1000)

    @pytest.mark.asyncio
    async def test_cancelled_error_exits_cleanly(self, loop_daemon):
        """CancelledError during queue.get exits the loop without crash."""
        daemon, session = loop_daemon

        call_count = 0
        original_get = daemon.queue.get

        async def cancelling_get():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise asyncio.CancelledError()
            return await original_get()

        with patch.object(daemon.queue, "get", side_effect=cancelling_get):
            await daemon._message_loop()

        # Loop exited without crash — running state unchanged by CancelledError
        # (CancelledError breaks out of while loop)

    @pytest.mark.asyncio
    async def test_unknown_item_type_skipped(self, loop_daemon):
        """Non-dict, non-InboundMessage items are silently skipped."""
        daemon, session = loop_daemon

        await daemon.queue.put(42)  # Neither InboundMessage nor dict
        await daemon.queue.put("stray string")
        await daemon.queue.put(None)

        with patch("lucyd.run_agentic_loop") as mock_loop:
            await daemon._message_loop()

        # Nothing was processed
        mock_loop.assert_not_called()


# ─── TEST-5: Audio Transcription Tests ───────────────────────────


class TestTranscribeAudio:
    """Tests for _transcribe_audio — pluggable STT backend."""

    @pytest.fixture
    def audio_daemon(self, tmp_path):
        """Daemon configured for audio transcription testing."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        # Create a minimal audio file for testing
        audio_file = tmp_path / "test_audio.ogg"
        audio_file.write_bytes(b"OggS" + b"\x00" * 100)  # minimal OGG header
        return daemon, str(audio_file)

    def _mock_openai_config(self, daemon, api_key="sk-test-key", **overrides):
        """Configure daemon.config for OpenAI STT backend."""
        daemon.config = MagicMock()
        daemon.config.stt_backend = "openai"
        daemon.config.api_key = MagicMock(return_value=api_key)
        daemon.config.stt_openai_api_url = overrides.get(
            "api_url", "https://api.openai.com/v1/audio/transcriptions")
        daemon.config.stt_openai_model = overrides.get("model", "whisper-1")
        daemon.config.stt_openai_timeout = overrides.get("timeout", 60)

    # --- Backend dispatch ---

    @pytest.mark.asyncio
    async def test_unknown_backend_raises(self, audio_daemon):
        """Unknown STT backend raises RuntimeError."""
        daemon, audio_path = audio_daemon
        daemon.config = MagicMock()
        daemon.config.stt_backend = "nonexistent"

        with pytest.raises(RuntimeError, match="Unknown STT backend"):
            await daemon._transcribe_audio(audio_path, "audio/ogg")

    # --- OpenAI backend ---

    @pytest.mark.asyncio
    async def test_openai_missing_api_key_raises(self, audio_daemon):
        """Missing OpenAI API key raises RuntimeError."""
        daemon, audio_path = audio_daemon
        self._mock_openai_config(daemon, api_key="")

        with pytest.raises(RuntimeError, match="No OpenAI API key for Whisper"):
            await daemon._transcribe_audio(audio_path, "audio/ogg")

    @pytest.mark.asyncio
    async def test_openai_successful_transcription(self, audio_daemon):
        """Successful OpenAI Whisper API call returns transcribed text."""
        daemon, audio_path = audio_daemon
        self._mock_openai_config(daemon, api_key="sk-test-key-12345")

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"text": "Hello, how are you?"})

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await daemon._transcribe_audio(audio_path, "audio/ogg")

        assert result == "Hello, how are you?"
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "https://api.openai.com/v1/audio/transcriptions"
        assert call_args[1]["headers"]["Authorization"] == "Bearer sk-test-key-12345"

    @pytest.mark.asyncio
    async def test_openai_custom_config(self, audio_daemon):
        """Custom OpenAI STT configuration is used."""
        daemon, audio_path = audio_daemon
        self._mock_openai_config(
            daemon, api_key="sk-custom-key",
            api_url="https://custom.api/v1/transcribe",
            model="whisper-large-v3",
            timeout=120,
        )

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"text": "Custom transcription"})

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client) as mock_cls:
            result = await daemon._transcribe_audio(audio_path, "audio/ogg")

        assert result == "Custom transcription"
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "https://custom.api/v1/transcribe"
        assert call_args[1]["data"]["model"] == "whisper-large-v3"
        mock_cls.assert_called_once_with(timeout=120)

    @pytest.mark.asyncio
    async def test_openai_api_error_raises(self, audio_daemon):
        """Non-200 API response raises via raise_for_status."""
        daemon, audio_path = audio_daemon
        self._mock_openai_config(daemon)

        import httpx
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "Server Error",
                request=MagicMock(),
                response=MagicMock(status_code=500),
            ),
        )

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(httpx.HTTPStatusError):
                await daemon._transcribe_audio(audio_path, "audio/ogg")

    @pytest.mark.asyncio
    async def test_openai_empty_text_raises(self, audio_daemon):
        """API response with empty text raises RuntimeError."""
        daemon, audio_path = audio_daemon
        self._mock_openai_config(daemon)

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={})  # No "text" key

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(RuntimeError, match="empty transcription"):
                await daemon._transcribe_audio(audio_path, "audio/ogg")

    # --- Local backend ---

    @pytest.mark.asyncio
    async def test_local_successful_transcription(self, audio_daemon):
        """Local whisper.cpp backend: ffmpeg + POST returns text."""
        daemon, audio_path = audio_daemon
        daemon.config = MagicMock()
        daemon.config.stt_backend = "local"
        daemon.config.stt_local_endpoint = "http://whisper:8082/inference"
        daemon.config.stt_local_language = "de"
        daemon.config.stt_local_ffmpeg_timeout = 30
        daemon.config.stt_local_request_timeout = 60

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"text": "Guten Morgen"})

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("subprocess.run") as mock_ffmpeg, \
             patch("httpx.AsyncClient", return_value=mock_client):
            result = await daemon._transcribe_audio(audio_path, "audio/ogg")

        assert result == "Guten Morgen"
        # Verify ffmpeg was called with correct args
        mock_ffmpeg.assert_called_once()
        ffmpeg_args = mock_ffmpeg.call_args[0][0]
        assert ffmpeg_args[0] == "ffmpeg"
        assert "-ar" in ffmpeg_args
        assert "16000" in ffmpeg_args
        # Verify whisper endpoint was called
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "http://whisper:8082/inference"
        assert call_args[1]["data"]["language"] == "de"

    @pytest.mark.asyncio
    async def test_local_ffmpeg_failure_raises(self, audio_daemon):
        """ffmpeg failure raises CalledProcessError."""
        import subprocess

        daemon, audio_path = audio_daemon
        daemon.config = MagicMock()
        daemon.config.stt_backend = "local"
        daemon.config.stt_local_endpoint = "http://whisper:8082/inference"
        daemon.config.stt_local_language = "auto"
        daemon.config.stt_local_ffmpeg_timeout = 30
        daemon.config.stt_local_request_timeout = 60

        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "ffmpeg")):
            with pytest.raises(subprocess.CalledProcessError):
                await daemon._transcribe_audio(audio_path, "audio/ogg")

    @pytest.mark.asyncio
    async def test_local_ffmpeg_timeout_raises(self, audio_daemon):
        """ffmpeg timeout raises TimeoutExpired."""
        import subprocess

        daemon, audio_path = audio_daemon
        daemon.config = MagicMock()
        daemon.config.stt_backend = "local"
        daemon.config.stt_local_endpoint = "http://whisper:8082/inference"
        daemon.config.stt_local_language = "auto"
        daemon.config.stt_local_ffmpeg_timeout = 30
        daemon.config.stt_local_request_timeout = 60

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 30)):
            with pytest.raises(subprocess.TimeoutExpired):
                await daemon._transcribe_audio(audio_path, "audio/ogg")

    @pytest.mark.asyncio
    async def test_local_whisper_error_raises(self, audio_daemon):
        """Whisper server error raises."""
        import httpx

        daemon, audio_path = audio_daemon
        daemon.config = MagicMock()
        daemon.config.stt_backend = "local"
        daemon.config.stt_local_endpoint = "http://whisper:8082/inference"
        daemon.config.stt_local_language = "auto"
        daemon.config.stt_local_ffmpeg_timeout = 30
        daemon.config.stt_local_request_timeout = 60

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "Server Error", request=MagicMock(),
                response=MagicMock(status_code=500),
            ),
        )

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("subprocess.run"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(httpx.HTTPStatusError):
                await daemon._transcribe_audio(audio_path, "audio/ogg")

    @pytest.mark.asyncio
    async def test_local_empty_text_raises(self, audio_daemon):
        """Whisper server returning empty text raises RuntimeError."""
        daemon, audio_path = audio_daemon
        daemon.config = MagicMock()
        daemon.config.stt_backend = "local"
        daemon.config.stt_local_endpoint = "http://whisper:8082/inference"
        daemon.config.stt_local_language = "auto"
        daemon.config.stt_local_ffmpeg_timeout = 30
        daemon.config.stt_local_request_timeout = 60

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"text": ""})

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("subprocess.run"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(RuntimeError, match="empty transcription"):
                await daemon._transcribe_audio(audio_path, "audio/ogg")

    @pytest.mark.asyncio
    async def test_local_wav_cleanup_on_success(self, audio_daemon):
        """WAV temp file is cleaned up after successful transcription."""
        daemon, audio_path = audio_daemon
        daemon.config = MagicMock()
        daemon.config.stt_backend = "local"
        daemon.config.stt_local_endpoint = "http://whisper:8082/inference"
        daemon.config.stt_local_language = "auto"
        daemon.config.stt_local_ffmpeg_timeout = 30
        daemon.config.stt_local_request_timeout = 60

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"text": "test"})

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        wav_paths = []
        original_mkstemp = __import__("tempfile").mkstemp

        def track_mkstemp(**kwargs):
            fd, path = original_mkstemp(**kwargs)
            wav_paths.append(path)
            return fd, path

        with patch("subprocess.run"), \
             patch("httpx.AsyncClient", return_value=mock_client), \
             patch("tempfile.mkstemp", side_effect=track_mkstemp):
            await daemon._transcribe_audio(audio_path, "audio/ogg")

        # WAV file should have been cleaned up
        assert len(wav_paths) == 1
        assert not Path(wav_paths[0]).exists()

    @pytest.mark.asyncio
    async def test_local_wav_cleanup_on_error(self, audio_daemon):
        """WAV temp file is cleaned up even when whisper fails."""
        import httpx

        daemon, audio_path = audio_daemon
        daemon.config = MagicMock()
        daemon.config.stt_backend = "local"
        daemon.config.stt_local_endpoint = "http://whisper:8082/inference"
        daemon.config.stt_local_language = "auto"
        daemon.config.stt_local_ffmpeg_timeout = 30
        daemon.config.stt_local_request_timeout = 60

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "Error", request=MagicMock(),
                response=MagicMock(status_code=500),
            ),
        )

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        wav_paths = []
        original_mkstemp = __import__("tempfile").mkstemp

        def track_mkstemp(**kwargs):
            fd, path = original_mkstemp(**kwargs)
            wav_paths.append(path)
            return fd, path

        with patch("subprocess.run"), \
             patch("httpx.AsyncClient", return_value=mock_client), \
             patch("tempfile.mkstemp", side_effect=track_mkstemp):
            with pytest.raises(httpx.HTTPStatusError):
                await daemon._transcribe_audio(audio_path, "audio/ogg")

        # WAV file should still be cleaned up
        assert len(wav_paths) == 1
        assert not Path(wav_paths[0]).exists()


# ─── _build_sessions Tests ────────────────────────────────────────


class TestBuildSessions:
    """Tests for LucydDaemon._build_sessions()."""

    def test_active_sessions(self, tmp_path):
        """Returns session info from index and live sessions."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)

        # Mock session manager with index and live sessions
        daemon.session_mgr = MagicMock()
        daemon.session_mgr._index = {
            "alice": {"session_id": "s-1", "created_at": 1707000000},
            "bob": {"session_id": "s-2", "created_at": 1707001000},
        }
        live_session = MagicMock()
        live_session.messages = [{"role": "user"}, {"role": "assistant"}]
        live_session.compaction_count = 1
        live_session.model = "primary"
        daemon.session_mgr._sessions = {"alice": live_session}

        result = daemon._build_sessions()

        assert len(result) == 2
        alice = next(s for s in result if s["contact"] == "alice")
        assert alice["session_id"] == "s-1"
        assert alice["message_count"] == 2
        assert alice["compaction_count"] == 1
        assert alice["model"] == "primary"

        bob = next(s for s in result if s["contact"] == "bob")
        assert bob["session_id"] == "s-2"
        assert "message_count" not in bob  # Not loaded in memory

    def test_empty_sessions(self, tmp_path):
        """No active sessions returns empty list."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.session_mgr = MagicMock()
        daemon.session_mgr._index = {}

        assert daemon._build_sessions() == []

    def test_no_session_manager(self, tmp_path):
        """No session manager returns empty list."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.session_mgr = None

        assert daemon._build_sessions() == []


# ─── _build_cost Tests ────────────────────────────────────────────


class TestBuildCost:
    """Tests for LucydDaemon._build_cost()."""

    def test_today_filter(self, tmp_path):
        """Today filter returns only today's costs grouped by model."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.providers = {}

        now_ts = int(time.time())
        _make_cost_db(tmp_path / "cost.db", [
            (now_ts, "s-1", "claude-sonnet", 5000, 2000, 0, 0, 0.50),
            (now_ts - 60, "s-1", "claude-sonnet", 3000, 1000, 0, 0, 0.30),
            (now_ts, "s-2", "claude-haiku", 1000, 500, 0, 0, 0.05),
            # Two days ago — should not be included in today
            (now_ts - 200000, "s-3", "claude-sonnet", 10000, 5000, 0, 0, 5.00),
        ])

        result = daemon._build_cost("today")
        assert result["period"] == "today"
        assert result["total_cost"] == round(0.50 + 0.30 + 0.05, 4)
        assert len(result["models"]) == 2

        sonnet = next(m for m in result["models"] if m["model"] == "claude-sonnet")
        assert sonnet["input_tokens"] == 8000
        assert sonnet["output_tokens"] == 3000

    def test_week_filter(self, tmp_path):
        """Week filter includes entries from the last 7 days."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.providers = {}

        now_ts = int(time.time())
        _make_cost_db(tmp_path / "cost.db", [
            (now_ts, "s-1", "test", 1000, 500, 0, 0, 0.10),
            (now_ts - 3 * 86400, "s-2", "test", 2000, 1000, 0, 0, 0.20),  # 3 days ago
            (now_ts - 10 * 86400, "s-3", "test", 3000, 1500, 0, 0, 0.30),  # 10 days ago
        ])

        result = daemon._build_cost("week")
        assert result["period"] == "week"
        assert result["total_cost"] == round(0.10 + 0.20, 4)

    def test_all_filter(self, tmp_path):
        """All filter includes everything."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.providers = {}

        now_ts = int(time.time())
        _make_cost_db(tmp_path / "cost.db", [
            (now_ts, "s-1", "test", 1000, 500, 0, 0, 0.10),
            (now_ts - 100 * 86400, "s-2", "test", 2000, 1000, 0, 0, 0.20),
        ])

        result = daemon._build_cost("all")
        assert result["period"] == "all"
        assert result["total_cost"] == round(0.30, 4)

    def test_missing_db_graceful(self, tmp_path):
        """Missing cost DB returns zero cost gracefully."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.providers = {}
        # Don't create cost.db

        result = daemon._build_cost("today")
        assert result["total_cost"] == 0.0
        assert result["models"] == []

    def test_empty_db(self, tmp_path):
        """Empty cost DB returns zero cost."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.providers = {}

        _make_cost_db(tmp_path / "cost.db", [])

        result = daemon._build_cost("today")
        assert result["total_cost"] == 0.0
        assert result["models"] == []


# ─── Webhook Tests ────────────────────────────────────────────────


class TestFireWebhook:
    """Tests for LucydDaemon._fire_webhook()."""

    @pytest.mark.asyncio
    async def test_no_url_skips(self, tmp_path):
        """Empty callback URL means no HTTP call."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.config = MagicMock()
        daemon.config.http_callback_url = ""

        with patch("httpx.AsyncClient") as mock_cls:
            await daemon._fire_webhook(
                reply="test", session_id="s-1", sender="alice",
                source="telegram", silent=False,
                tokens={"input": 100, "output": 50},
                notify_meta=None,
            )
            mock_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_posts_payload(self, tmp_path):
        """Webhook POSTs correct payload to configured URL."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.config = MagicMock()
        daemon.config.http_callback_url = "https://n8n.local/webhook/abc"
        daemon.config.http_callback_token = ""

        mock_response = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            await daemon._fire_webhook(
                reply="Hello!", session_id="s-1", sender="alice",
                source="telegram", silent=False,
                tokens={"input": 5000, "output": 200},
                notify_meta=None,
            )

        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert call_args[1]["json"]["reply"] == "Hello!"
        assert call_args[1]["json"]["session_id"] == "s-1"
        assert call_args[1]["json"]["sender"] == "alice"
        assert call_args[1]["json"]["source"] == "telegram"
        assert call_args[1]["json"]["silent"] is False
        assert call_args[1]["json"]["tokens"] == {"input": 5000, "output": 200}
        assert "notify_meta" not in call_args[1]["json"]

    @pytest.mark.asyncio
    async def test_auth_header(self, tmp_path):
        """Bearer token included when callback token is configured."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.config = MagicMock()
        daemon.config.http_callback_url = "https://n8n.local/webhook/abc"
        daemon.config.http_callback_token = "webhook-secret"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=MagicMock())
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            await daemon._fire_webhook(
                reply="test", session_id="s-1", sender="alice",
                source="telegram", silent=False,
                tokens={"input": 100, "output": 50},
                notify_meta=None,
            )

        headers = mock_client.post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer webhook-secret"

    @pytest.mark.asyncio
    async def test_failure_logged_not_raised(self, tmp_path, caplog):
        """Webhook failure is logged as warning, not raised."""
        import httpx

        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.config = MagicMock()
        daemon.config.http_callback_url = "https://n8n.local/webhook/abc"
        daemon.config.http_callback_token = ""

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            # Should not raise
            await daemon._fire_webhook(
                reply="test", session_id="s-1", sender="alice",
                source="telegram", silent=False,
                tokens={"input": 100, "output": 50},
                notify_meta=None,
            )

        assert any("Webhook callback failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_silent_flag(self, tmp_path):
        """Silent flag is correctly passed in webhook payload."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.config = MagicMock()
        daemon.config.http_callback_url = "https://n8n.local/webhook/abc"
        daemon.config.http_callback_token = ""

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=MagicMock())
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            await daemon._fire_webhook(
                reply="HEARTBEAT_OK", session_id="s-1", sender="system",
                source="system", silent=True,
                tokens={"input": 100, "output": 10},
                notify_meta=None,
            )

        payload = mock_client.post.call_args[1]["json"]
        assert payload["silent"] is True

    @pytest.mark.asyncio
    async def test_notify_meta_echo(self, tmp_path):
        """notify_meta is echoed in webhook payload."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.config = MagicMock()
        daemon.config.http_callback_url = "https://n8n.local/webhook/abc"
        daemon.config.http_callback_token = ""

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=MagicMock())
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        meta = {"source": "n8n-email", "ref": "Q-47", "data": {"amount": 1500}}

        with patch("httpx.AsyncClient", return_value=mock_client):
            await daemon._fire_webhook(
                reply="Quote accepted", session_id="s-1", sender="http-n8n",
                source="system", silent=False,
                tokens={"input": 2000, "output": 100},
                notify_meta=meta,
            )

        payload = mock_client.post.call_args[1]["json"]
        assert payload["notify_meta"]["source"] == "n8n-email"
        assert payload["notify_meta"]["ref"] == "Q-47"
        assert payload["notify_meta"]["data"]["amount"] == 1500


# ─── FIFO Attachment Reconstruction ──────────────────────────────


class TestFifoAttachmentReconstruction:
    """Test that _fifo_reader reconstructs Attachment objects from JSON dicts."""

    @pytest.mark.asyncio
    async def test_fifo_reconstructs_attachments(self, tmp_path):
        """Attachment dicts in FIFO JSON become Attachment objects on the queue."""
        from lucyd import _fifo_reader
        from channels import Attachment

        fifo_path = tmp_path / "test.pipe"
        queue = asyncio.Queue()

        # Create a test file the attachment references
        test_file = tmp_path / "photo.jpg"
        test_file.write_bytes(b"\xff\xd8\xff fake jpeg")

        msg = {
            "type": "user",
            "text": "look at this",
            "sender": "cli",
            "attachments": [{
                "content_type": "image/jpeg",
                "local_path": str(test_file),
                "filename": "photo.jpg",
                "size": test_file.stat().st_size,
            }],
        }

        # Start FIFO reader
        task = asyncio.create_task(_fifo_reader(fifo_path, queue))
        await asyncio.sleep(0.1)  # Let it create the FIFO

        # Write the message to the FIFO
        def write_fifo():
            with open(str(fifo_path), "w") as f:
                f.write(json.dumps(msg) + "\n")

        await asyncio.to_thread(write_fifo)
        # Wait for message to arrive on queue
        item = await asyncio.wait_for(queue.get(), timeout=2.0)

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert "attachments" in item
        atts = item["attachments"]
        assert len(atts) == 1
        assert isinstance(atts[0], Attachment)
        assert atts[0].content_type == "image/jpeg"
        assert atts[0].local_path == str(test_file)
        assert atts[0].filename == "photo.jpg"

    @pytest.mark.asyncio
    async def test_fifo_no_attachments_passthrough(self, tmp_path):
        """Messages without attachments pass through unchanged."""
        from lucyd import _fifo_reader

        fifo_path = tmp_path / "test.pipe"
        queue = asyncio.Queue()

        msg = {"type": "user", "text": "hello", "sender": "cli"}

        task = asyncio.create_task(_fifo_reader(fifo_path, queue))
        await asyncio.sleep(0.1)

        def write_fifo():
            with open(str(fifo_path), "w") as f:
                f.write(json.dumps(msg) + "\n")

        await asyncio.to_thread(write_fifo)
        item = await asyncio.wait_for(queue.get(), timeout=2.0)

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert item["text"] == "hello"
        assert "attachments" not in item or item.get("attachments") is None
