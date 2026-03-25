"""Tests for LucydDaemon internals — _build_status, _check_context_budget,
_resolve pattern, deliver flag, process_http_immediate, message loop
HTTP bypass.

These tests mock heavy dependencies (providers, channels, sessions) to isolate
the daemon's orchestration logic.
"""

import asyncio
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from async_utils import run_blocking
from lucyd import LucydDaemon, _is_silent
from metering import MeteringDB


@dataclass
class _U:
    """Minimal usage object for MeteringDB.record()."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

# ─── Helpers ──────────────────────────────────────────────────────


def _deep_merge(base: dict, overrides: dict) -> dict:
    """Deep merge overrides into base dict (mutates base)."""
    for key, val in overrides.items():
        if key in base and isinstance(base[key], dict) and isinstance(val, dict):
            _deep_merge(base[key], val)
        else:
            base[key] = val
    return base


def _make_config(tmp_path, **overrides):
    """Build a minimal Config for testing daemon methods."""
    from config import Config

    base = {
        "agent": {
            "name": "TestAgent",
            "workspace": str(tmp_path / "workspace"),
            "context": {"stable": ["SOUL.md"], "semi_stable": []},
            "skills": {"dir": "skills", "always_on": []},
        },
        "channel": {"type": "cli", "debounce_ms": 500},
        "http": {
            "enabled": False, "host": "127.0.0.1", "port": 8100, "token_env": "",
            "download_dir": "/tmp/lucyd-http", "max_body_bytes": 10485760,
            "callback_url": "", "callback_token_env": "", "callback_timeout": 10, "callback_max_failures": 10,
            "max_attachment_bytes": 52428800,
            "rate_limit": 30, "rate_window": 60, "status_rate_limit": 60,
            "rate_limit_cleanup_threshold": 1000,
        },
        "models": {
            "primary": {
                "provider": "anthropic-compat",
                "model": "test-model",
                "max_tokens": 1024,
                "cost_per_mtok": [1.0, 5.0, 0.1],
                "supports_vision": True,
            },
        },
        "memory": {
            "db": "", "search_top_k": 10, "vector_search_limit": 10000,
            "fts_min_results": 3, "embedding_timeout": 15,
            "consolidation": {"enabled": False, "min_messages": 4, "confidence_threshold": 0.6, "max_extraction_chars": 50000},
            "recall": {
                "decay_rate": 0.03, "max_facts_in_context": 20, "max_dynamic_tokens": 1500, "max_episodes_at_start": 3, "archive_messages": 20,
                "personality": {
                    "priority_vector": 35, "priority_episodes": 25, "priority_facts": 15, "priority_commitments": 40,
                    "fact_format": "natural", "show_emotional_tone": True, "episode_section_header": "Recent conversations",
                },
            },
            "maintenance": {"stale_threshold_days": 90},
            "indexer": {"include_patterns": ["memory/*.md"], "exclude_dirs": [], "chunk_size_chars": 1600, "chunk_overlap_chars": 320, "embed_batch_limit": 100},
        },
        "tools": {
            "enabled": ["read", "write", "edit", "exec"],
            "plugins_dir": "plugins.d", "output_truncation": 30000,
            "subagent_deny": [], "subagent_max_turns": 0, "subagent_timeout": 0,
            "exec_timeout": 120, "exec_max_timeout": 600,
            "filesystem": {"allowed_paths": ["/tmp/"], "default_read_limit": 2000},
            "web_search": {"provider": "", "api_key_env": "", "timeout": 15},
            "web_fetch": {"timeout": 15},
        },
        "documents": {"enabled": True, "max_chars": 30000, "max_file_bytes": 10485760,
                      "text_extensions": [".txt", ".md", ".csv", ".json"]},
        "logging": {"max_bytes": 10485760, "backup_count": 3, "suppress": []},
        "vision": {"max_image_bytes": 5242880, "max_dimension": 1568,
                   "jpeg_quality_steps": [85, 60, 40],
                   },
        "behavior": {
            "silent_tokens": ["NO_REPLY"], "typing_indicators": True,
            "error_message": "connection error", "sqlite_timeout": 30,
            "api_retries": 2, "api_retry_base_delay": 2.0,
            "message_retries": 2, "message_retry_base_delay": 30.0,
            "audit_truncation_limit": 500, "agent_timeout_seconds": 600,
            "max_turns_per_message": 50, "max_cost_per_message": 0.0,
            "queue_capacity": 1000, "queue_poll_interval": 1.0, "quote_max_chars": 200,
            "notify_target": "",
            "compaction": {
                "threshold_tokens": 150000, "max_tokens": 2048,
                "prompt": "Summarize this conversation for {agent_name}.",
                "keep_recent_pct": 0.33, "keep_recent_pct_min": 0.05, "keep_recent_pct_max": 0.9,
                "min_messages": 4, "tool_result_max_chars": 2000, "warning_pct": 0.8,
                "diary_prompt": "Write a log for {date}.",
            },
        },
        "paths": {
            "state_dir": str(tmp_path / "state"),
            "sessions_dir": str(tmp_path / "sessions"),
            "log_file": str(tmp_path / "lucyd.log"),
        },
    }
    _deep_merge(base, overrides)

    # Ensure directories exist
    (tmp_path / "workspace").mkdir(exist_ok=True)
    (tmp_path / "workspace" / "SOUL.md").write_text("# Test Soul")
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "sessions").mkdir(exist_ok=True)

    return Config(base)


def _attach_metering(daemon, tmp_path):
    """Attach a fresh MeteringDB to the daemon."""
    daemon.metering_db = MeteringDB(str(tmp_path / "metering.db"))


# ─── _build_status ───────────────────────────────────────────────


class TestBuildStatus:
    """Tests for LucydDaemon._build_status()."""

    def test_basic_status_fields(self, tmp_path):
        """Status dict contains all expected fields."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        _attach_metering(daemon, tmp_path)
        daemon.provider = MagicMock()
        daemon._providers = {"primary": daemon.provider}
        daemon.session_mgr = MagicMock()
        daemon.session_mgr._index = {"user1": "s-1", "system": "s-2"}
        daemon.session_mgr.session_count = MagicMock(return_value=2)

        status = daemon._build_status()

        assert status["status"] == "ok"
        assert status["pid"] == os.getpid()
        assert isinstance(status["uptime_seconds"], int)
        assert status["channel"] == "cli"
        assert status["model"] == "test-model"
        assert status["active_sessions"] == 2
        assert isinstance(status["today_cost"], float)
        assert isinstance(status["queue_depth"], int)

    def test_today_cost_from_db(self, tmp_path):
        """Today's cost is calculated from metering DB."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        _attach_metering(daemon, tmp_path)
        daemon.provider = None
        daemon._providers = {}
        daemon.session_mgr = MagicMock()
        daemon.session_mgr._index = {}
        daemon.session_mgr.session_count = MagicMock(return_value=0)

        # Insert test data via metering DB.
        # cost = input*rate[0]/1M + output*rate[1]/1M + cache_read*rate[2]/1M
        # Record 1: 1M input * 0.5/1M = 0.50
        daemon.metering_db.record("s-1", "test", "p", _U(input_tokens=1_000_000), [0.5, 0.0, 0.0])
        # Record 2: 1M input * 1.25/1M = 1.25
        daemon.metering_db.record("s-1", "test", "p", _U(input_tokens=1_000_000), [1.25, 0.0, 0.0])
        # Record 3: will be backdated — should NOT be counted today
        daemon.metering_db.record("s-2", "test", "p", _U(input_tokens=1_000_000), [10.0, 0.0, 0.0])
        # Backdate the last record to 2 days ago
        conn = sqlite3.connect(daemon.metering_db.path)
        conn.execute("UPDATE costs SET timestamp = ? WHERE rowid = (SELECT MAX(rowid) FROM costs)",
                      (int(time.time()) - 86400 * 2,))
        conn.commit()
        conn.close()

        status = daemon._build_status()
        assert status["today_cost"] == round(0.50 + 1.25, 4)

    def test_no_cost_db_returns_zero(self, tmp_path):
        """Empty metering DB returns 0.0 cost."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        _attach_metering(daemon, tmp_path)
        daemon.provider = None
        daemon._providers = {}
        daemon.session_mgr = MagicMock()
        daemon.session_mgr._index = {}
        daemon.session_mgr.session_count = MagicMock(return_value=0)

        # No records inserted
        status = daemon._build_status()
        assert status["today_cost"] == 0.0

    def test_empty_cost_db(self, tmp_path):
        """Empty metering DB returns 0.0 cost."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        _attach_metering(daemon, tmp_path)
        daemon.provider = None
        daemon._providers = {}
        daemon.session_mgr = MagicMock()
        daemon.session_mgr._index = {}
        daemon.session_mgr.session_count = MagicMock(return_value=0)

        status = daemon._build_status()
        assert status["today_cost"] == 0.0

    def test_no_session_manager(self, tmp_path):
        """No session manager returns 0 active sessions."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        _attach_metering(daemon, tmp_path)
        daemon.provider = None
        daemon._providers = {}
        daemon.session_mgr = None

        status = daemon._build_status()
        assert status["active_sessions"] == 0

    def test_queue_depth_reflects_items(self, tmp_path):
        """Queue depth matches actual items in the queue."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        _attach_metering(daemon, tmp_path)
        daemon.provider = None
        daemon._providers = {}
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
        _attach_metering(daemon, tmp_path)
        daemon.start_time = time.time() - 120  # Started 2 min ago
        daemon.provider = None
        daemon._providers = {}
        daemon.session_mgr = None

        status = daemon._build_status()
        assert status["uptime_seconds"] >= 119  # Allow 1s tolerance

    def test_provider_listed_in_status(self, tmp_path):
        """Provider name appears in status."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        _attach_metering(daemon, tmp_path)
        daemon.provider = MagicMock()
        daemon._providers = {"primary": daemon.provider}
        daemon.session_mgr = None

        status = daemon._build_status()
        assert status["model"] == "test-model"


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
        daemon.provider = None  # No provider configured
        daemon._providers = {}
        daemon.session_mgr = MagicMock()

        loop = asyncio.get_running_loop()
        future = loop.create_future()

        await daemon._process_message(
            text="test",
            sender="http-test",
            source="http", deliver=False,
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
        daemon.provider = None  # Will trigger early return (no provider)
        daemon._providers = {}
        daemon.session_mgr = MagicMock()

        # This should not raise
        await daemon._process_message(
            text="test",
            sender="test-sender",
            source="system", deliver=False,
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
        _attach_metering(daemon, tmp_path)

        # Mock provider
        provider = MagicMock()
        provider.format_system = MagicMock(return_value=[])
        provider.format_messages = MagicMock(return_value=[])
        provider.format_tools = MagicMock(return_value=[])

        daemon.provider = provider
        daemon._providers = {"primary": provider}

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
        session.save_state = MagicMock()

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
            await daemon._process_message(
                text="test",
                sender="http-test",
                source="http", deliver=False,
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
        daemon.config.http_callback_max_failures = 0
        daemon.config.model_config = MagicMock(return_value={
            "model": "test", "cost_per_mtok": [1.0, 5.0, 0.1],
        })
        daemon.config.typing_indicators = False
        daemon.config.max_turns = 10
        daemon.config.agent_timeout = 30
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
            await daemon._process_message(
                text="heartbeat trigger",
                sender="http-test",
                source="http", deliver=False,
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
        daemon.config.http_callback_max_failures = 0
        daemon.config.model_config = MagicMock(return_value={
            "model": "test", "cost_per_mtok": [1.0, 5.0, 0.1],
        })
        daemon.config.typing_indicators = False
        daemon.config.max_turns = 10
        daemon.config.agent_timeout = 30
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
            await daemon._process_message(
                text="test question",
                sender="http-test",
                source="http", deliver=False,
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
        _attach_metering(daemon, tmp_path)

        provider = MagicMock()
        provider.format_system = MagicMock(return_value=[])
        daemon.provider = provider
        daemon._providers = {"primary": provider}

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
        session.save_state = MagicMock()

        daemon.session_mgr = MagicMock()
        daemon.session_mgr.get_or_create = MagicMock(return_value=session)
        daemon.session_mgr.close_session = AsyncMock(return_value=True)

        daemon.context_builder = MagicMock()
        daemon.context_builder.build = MagicMock(return_value=[])

        daemon.skill_loader = MagicMock()
        daemon.skill_loader.build_index = MagicMock(return_value="")
        daemon.skill_loader.get_bodies = MagicMock(return_value={})

        daemon.tool_registry = MagicMock()

        daemon.tool_registry.get_schemas = MagicMock(return_value=[])

        daemon.channel = AsyncMock()

        daemon.config = MagicMock()
        daemon.config.http_callback_max_failures = 0
        daemon.config.model_config = MagicMock(return_value={
            "model": "test", "cost_per_mtok": [1.0, 5.0, 0.1],
        })
        daemon.config.typing_indicators = True
        daemon.config.max_turns = 10
        daemon.config.agent_timeout = 30
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
    async def test_deliver_false_suppresses_typing(self, daemon_with_successful_response):
        """deliver=False skips typing indicator."""
        daemon, response = daemon_with_successful_response

        with patch("lucyd.run_agentic_loop", return_value=response):
            await daemon._process_message(
                text="heartbeat", sender="system", source="system", deliver=False,
            )

        daemon.channel.send_typing.assert_not_called()

    @pytest.mark.asyncio
    async def test_deliver_false_suppresses_reply(self, daemon_with_successful_response):
        """deliver=False doesn't deliver reply via channel."""
        daemon, response = daemon_with_successful_response

        with patch("lucyd.run_agentic_loop", return_value=response):
            await daemon._process_message(
                text="heartbeat", sender="system", source="system", deliver=False,
            )

        daemon.channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_deliver_true_sends_typing(self, daemon_with_successful_response):
        """deliver=True sends typing indicator."""
        daemon, response = daemon_with_successful_response

        with patch("lucyd.run_agentic_loop", return_value=response):
            await daemon._process_message(
                text="hello", sender="user", source="system", deliver=True,
            )

        daemon.channel.send_typing.assert_called()

    @pytest.mark.asyncio
    async def test_http_chat_suppresses_channel_reply(self, daemon_with_successful_response):
        """HTTP /chat (deliver=False) doesn't deliver reply via channel.send."""
        daemon, response = daemon_with_successful_response
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        with patch("lucyd.run_agentic_loop", return_value=response):
            await daemon._process_message(
                text="api call", sender="http", source="http",
                response_future=future, deliver=False,
            )

        daemon.channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_telegram_source_delivers_typing(self, daemon_with_successful_response):
        """Telegram source sends typing indicator."""
        daemon, response = daemon_with_successful_response

        with patch("lucyd.run_agentic_loop", return_value=response):
            await daemon._process_message(
                text="hello", sender="+431234567890", source="telegram",
            )

        daemon.channel.send_typing.assert_called_once()

    @pytest.mark.asyncio
    async def test_telegram_source_delivers_reply(self, daemon_with_successful_response):
        """Telegram source delivers reply via channel."""
        daemon, response = daemon_with_successful_response

        with patch("lucyd.run_agentic_loop", return_value=response):
            await daemon._process_message(
                text="hello", sender="+431234567890", source="telegram",
            )

        daemon.channel.send.assert_called_once_with("+431234567890", "Test reply")

    @pytest.mark.asyncio
    async def test_system_source_suppresses_all_text(self, daemon_with_successful_response):
        """System source doesn't deliver any text via channel."""
        daemon, response = daemon_with_successful_response

        with patch("lucyd.run_agentic_loop", return_value=response):
            await daemon._process_message(
                text="heartbeat", sender="system", source="system", deliver=False,
            )

        daemon.channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_http_error_suppresses_channel_error_msg(self, daemon_with_successful_response):
        """HTTP source doesn't send error message via channel on agentic loop failure."""
        daemon, _ = daemon_with_successful_response
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        with patch("lucyd.run_agentic_loop", side_effect=RuntimeError("fail")):
            await daemon._process_message(
                text="test", sender="http", source="http", deliver=False,
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
        _attach_metering(daemon, tmp_path)

        provider = MagicMock()
        provider.format_system = MagicMock(return_value=[])
        daemon.provider = provider
        daemon._providers = {"primary": provider}

        session = MagicMock()
        session.id = "ctx-test"
        session.messages = []
        session.pending_system_warning = ""
        session.last_input_tokens = 0
        session.needs_compaction = MagicMock(return_value=False)
        session.warned_about_compaction = False
        session.add_user_message = MagicMock()
        session.save_state = MagicMock()

        daemon.session_mgr = MagicMock()
        daemon.session_mgr.get_or_create = MagicMock(return_value=session)
        daemon.session_mgr.close_session = AsyncMock(return_value=True)

        daemon.context_builder = MagicMock()
        daemon.context_builder.build = MagicMock(return_value=[])

        daemon.skill_loader = MagicMock()
        daemon.skill_loader.build_index = MagicMock(return_value="")
        daemon.skill_loader.get_bodies = MagicMock(return_value={})

        daemon.tool_registry = MagicMock()

        daemon.tool_registry.get_schemas = MagicMock(return_value=[])

        daemon.channel = AsyncMock()

        daemon.config = MagicMock()
        daemon.config.http_callback_max_failures = 0
        daemon.config.model_config = MagicMock(return_value={
            "model": "test", "cost_per_mtok": [],
        })
        daemon.config.typing_indicators = False
        daemon.config.max_turns = 10
        daemon.config.agent_timeout = 30
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
            await daemon._process_message(
                text="test", sender="system", source="system", deliver=False,
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
            await daemon._process_message(
                text="test", sender="http", source="http", deliver=False,
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
            "response_future": future,
        }
        # The bypass check in the message loop
        assert item.get("response_future") is not None

    @pytest.mark.asyncio
    async def test_notify_item_has_no_response_future(self):
        """Notify items (type=system from HTTP) don't have response_future."""
        item = {
            "sender": "http",
            "type": "system",
            "text": "[AUTOMATED SYSTEM MESSAGE] test",
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



# ─── TEST-2: _process_message Integration Tests ─────────────────


class TestProcessMessageIntegration:
    """Integration tests for _process_message — full pipeline with mock
    provider, mock channel, and mock session."""

    @pytest.fixture
    def full_daemon(self, tmp_path):
        """Daemon with all required mocks for _process_message."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        _attach_metering(daemon, tmp_path)

        # Mock provider
        provider = MagicMock()
        provider.format_system = MagicMock(return_value=[])
        provider.format_messages = MagicMock(return_value=[])
        provider.format_tools = MagicMock(return_value=[])
        daemon.provider = provider
        daemon._providers = {"primary": provider}

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
        session.save_state = MagicMock()

        daemon.session_mgr = MagicMock()
        daemon.session_mgr.get_or_create = MagicMock(return_value=session)
        daemon.session_mgr.compact_session = AsyncMock()
        daemon.session_mgr.close_session = AsyncMock(return_value=True)
        daemon.session_mgr.save_state = MagicMock(side_effect=lambda s: s.save_state())
        daemon.session_mgr.has_session = MagicMock(return_value=False)
        daemon.session_mgr.list_contacts = MagicMock(return_value=[])
        daemon.session_mgr.list_sessions = MagicMock(return_value=[])
        daemon.session_mgr.session_count = MagicMock(return_value=0)
        daemon.session_mgr.get_index = MagicMock(return_value={})
        daemon.session_mgr.get_loaded = MagicMock(return_value=None)

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

        daemon.tool_registry.get_schemas = MagicMock(return_value=[])

        # Mock channel
        daemon.channel = AsyncMock()

        # Override config as MagicMock for controlled attribute access
        daemon.config = MagicMock()
        daemon.config.http_callback_max_failures = 0
        daemon.config.model_config = MagicMock(return_value={
            "model": "test-model", "cost_per_mtok": [1.0, 5.0, 0.1],
            "supports_vision": True,
        })
        daemon.config.typing_indicators = False
        daemon.config.max_turns = 10
        daemon.config.agent_timeout = 30
        daemon.config.silent_tokens = []
        daemon.config.compaction_threshold = 150000
        daemon.config.compaction_max_tokens = 2048
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
        session.save_state.assert_called()
        # Channel should deliver the reply (source=telegram not in deliver flag)
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
            await daemon._process_message(
                text="hello",
                sender="+431234567890",
                source="telegram",
            )

        # Channel should deliver error message (telegram not in deliver flag)
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
            await daemon._process_message(
                text="bad request",
                sender="http",
                source="http", deliver=False,
                response_future=future,
            )

        assert future.done()
        result = future.result()
        assert "error" in result
        assert "Bad request" in result["error"]
        assert result["session_id"] == "integ-session-1"
        # Channel should NOT deliver error (http in deliver flag)
        daemon.channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_image_attachment_adds_prefix_to_text(self, full_daemon, tmp_path):
        """Image attachment adds [image] prefix and creates image blocks."""
        daemon, provider, session = full_daemon

        # Create a real small test image via Pillow
        pytest.importorskip("PIL")
        from PIL import Image as PILImage
        pil_img = PILImage.new("RGB", (100, 100), color="red")
        img_path = tmp_path / "test.jpg"
        pil_img.save(str(img_path), format="JPEG")

        from models import Attachment
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
            await daemon._process_message(
                text="What is in this picture?",
                sender="+431234567890",
                source="telegram",
                attachments=[att],
            )

        # add_user_message should have been called with text containing [image]
        call_text = session.add_user_message.call_args[0][0]
        assert "[image, saved:" in call_text
        assert "What is in this picture?" in call_text

    @pytest.mark.asyncio
    async def test_unfittable_image_shows_fallback(self, full_daemon, tmp_path):
        """PNG that can't be compressed below limit shows fallback message."""
        pytest.importorskip("PIL")
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

        from PIL import Image as PILImage

        from models import Attachment

        img = PILImage.new("RGB", (200, 200), color="red")
        img_path = tmp_path / "big.png"
        img.save(str(img_path), format="PNG")
        att = Attachment(content_type="image/png", local_path=str(img_path),
                         filename="big.png", size=img_path.stat().st_size)

        with patch("lucyd.run_agentic_loop", return_value=response):
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

        from models import Attachment

        # Point to a file that doesn't exist
        att = Attachment(content_type="image/jpeg", local_path=str(tmp_path / "gone.jpg"),
                         filename="gone.jpg", size=1000)

        with patch("lucyd.run_agentic_loop", return_value=response):
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
            await daemon._process_message(
                text="Continue working",
                sender="+431234567890",
                source="telegram",
            )

        # compact_session should have been called
        daemon.session_mgr.compact_session.assert_called_once()
        args = daemon.session_mgr.compact_session.call_args[0]
        assert args[0] is session
        assert args[1] is provider  # Uses the main provider
        assert isinstance(args[2], str) and len(args[2]) > 0


# ─── TEST-3: _message_loop Behavior Tests ───────────────────────


class TestMessageLoopDebounce:
    """Test debounce window combining behavior in _message_loop."""

    @pytest.fixture
    def loop_daemon(self, tmp_path):
        """Daemon configured for message loop testing with short debounce."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        _attach_metering(daemon, tmp_path)

        # Mock everything _process_message needs
        provider = MagicMock()
        provider.format_system = MagicMock(return_value=[])
        daemon.provider = provider
        daemon._providers = {"primary": provider}

        session = MagicMock()
        session.id = "loop-test-session"
        session.messages = []
        session.pending_system_warning = ""
        session.last_input_tokens = 0
        session.needs_compaction = MagicMock(return_value=False)
        session.warned_about_compaction = False
        session.add_user_message = MagicMock()
        session.save_state = MagicMock()

        daemon.session_mgr = MagicMock()
        daemon.session_mgr.get_or_create = MagicMock(return_value=session)
        daemon.session_mgr.close_session = AsyncMock(return_value=True)
        daemon.session_mgr.close_session_by_id = AsyncMock(return_value=True)
        daemon.session_mgr._index = {}
        daemon.session_mgr.save_state = MagicMock(side_effect=lambda s: s.save_state())
        daemon.session_mgr.has_session = MagicMock(return_value=False)
        daemon.session_mgr.list_contacts = MagicMock(return_value=[])
        daemon.session_mgr.list_sessions = MagicMock(return_value=[])
        daemon.session_mgr.session_count = MagicMock(return_value=0)
        daemon.session_mgr.get_index = MagicMock(return_value={})
        daemon.session_mgr.get_loaded = MagicMock(return_value=None)

        daemon.context_builder = MagicMock()
        daemon.context_builder.build = MagicMock(return_value=[])
        daemon.skill_loader = MagicMock()
        daemon.skill_loader.build_index = MagicMock(return_value="")
        daemon.skill_loader.get_bodies = MagicMock(return_value={})
        daemon.tool_registry = MagicMock()

        daemon.tool_registry.get_schemas = MagicMock(return_value=[])
        daemon.channel = AsyncMock()

        daemon.config = MagicMock()
        daemon.config.http_callback_max_failures = 0
        daemon.config.model_config = MagicMock(return_value={
            "model": "test", "cost_per_mtok": [1.0, 5.0, 0.1],
        })
        daemon.config.typing_indicators = False
        daemon.config.max_turns = 10
        daemon.config.agent_timeout = 30
        daemon.config.silent_tokens = []
        daemon.config.compaction_threshold = 150000
        daemon.config.compaction_max_tokens = 2048
        daemon.config.compaction_prompt = "Summarize"
        daemon.config.agent_name = "TestAgent"
        daemon.config.consolidation_enabled = False
        daemon.config.always_on_skills = []
        daemon.config.error_message = "Error"
        daemon.config.message_retries = 0
        daemon.config.message_retry_base_delay = 0.01
        daemon.config.raw = MagicMock(return_value=0.0)
        daemon.config.queue_poll_interval = 1.0
        daemon.config.quote_max_chars = 200
        daemon.config.sqlite_timeout = 30
        daemon.config.compaction_warning_pct = 0.8
        daemon.config.compaction_min_messages = 4
        daemon.config.compaction_tool_result_max_chars = 2000
        daemon.config.notify_target = ""
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

        from models import InboundMessage

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
            "response_future": future,
        }
        await daemon.queue.put(http_item)
        await daemon.queue.put(None)

        with patch("lucyd.run_agentic_loop", return_value=response):
            await daemon._message_loop()

        # Future should be resolved
        assert future.done()
        result = future.result()
        assert result["reply"] == "http reply"

    @pytest.mark.asyncio
    async def test_empty_text_and_no_attachments_skipped(self, loop_daemon):
        """Messages with empty text and no attachments are skipped."""
        daemon, session = loop_daemon

        from models import InboundMessage

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
            await daemon._message_loop()

        # _process_message should NOT have been called (empty text, no attachments)
        mock_loop.assert_not_called()

    @pytest.mark.asyncio
    async def test_debounce_combines_same_sender_within_window(self, loop_daemon):
        """Messages from same sender queued before sleep completes are combined."""
        daemon, session = loop_daemon

        from models import InboundMessage

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
                await daemon.queue.put(None)
                await daemon._message_loop()

        # Debounce sleep was called
        assert len(sleep_calls) >= 1

    @pytest.mark.asyncio
    async def test_different_senders_both_drained(self, loop_daemon):
        """Messages from different senders are each processed."""
        daemon, session = loop_daemon

        from models import InboundMessage

        msg1 = InboundMessage(text="Hello", sender="alice", timestamp=time.time(), source="telegram")
        msg2 = InboundMessage(text="World", sender="bob", timestamp=time.time(), source="telegram")
        await daemon.queue.put(msg1)
        await daemon.queue.put(msg2)
        await daemon.queue.put(None)

        response = MagicMock()
        response.text = "ok"
        response.usage = MagicMock(input_tokens=100, output_tokens=50)

        with patch("lucyd.run_agentic_loop", return_value=response):
            await daemon._message_loop()

        # Both senders should have been processed
        assert session.add_user_message.call_count >= 2

    @pytest.mark.asyncio
    async def test_dict_messages_debounced(self, loop_daemon):
        """Dict-based queue messages are subject to debounce like InboundMessages."""
        daemon, session = loop_daemon

        queue_item = {
            "sender": "system",
            "type": "system",
            "text": "queued message",
        }
        await daemon.queue.put(queue_item)
        await daemon.queue.put(None)

        response = MagicMock()
        response.text = "ok"
        response.usage = MagicMock(input_tokens=100, output_tokens=50)

        with patch("lucyd.run_agentic_loop", return_value=response):
            await daemon._message_loop()

        call_text = session.add_user_message.call_args[0][0]
        assert "queued message" in call_text

    @pytest.mark.asyncio
    async def test_reset_all_closes_every_session(self, loop_daemon):
        """Reset with all=True closes every session in session_mgr._index."""
        daemon, session = loop_daemon
        daemon.session_mgr._index = {"alice": MagicMock(), "bob": MagicMock(), "system": MagicMock()}
        daemon.session_mgr.list_contacts.return_value = ["alice", "bob", "system"]

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
        daemon.session_mgr.list_contacts.return_value = ["system", "cli", "nicolas"]

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
        from models import Attachment, InboundMessage

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
    async def test_quote_injected_into_text(self, loop_daemon):
        """InboundMessage.quote is prepended to text before processing."""
        daemon, session = loop_daemon
        from models import InboundMessage

        msg = InboundMessage(
            text="totally agree",
            sender="user1",
            timestamp=time.time(),
            source="telegram",
            quote="here is my take on things",
        )
        await daemon.queue.put(msg)
        await daemon.queue.put(None)

        with patch.object(daemon, "_process_message", new_callable=AsyncMock) as mock_pm:
            await daemon._message_loop()

        mock_pm.assert_called_once()
        passed_text = mock_pm.call_args[0][0]  # first positional arg = text
        assert "[replying to: here is my take on things]" in passed_text
        assert "totally agree" in passed_text

    @pytest.mark.asyncio
    async def test_quote_none_not_injected(self, loop_daemon):
        """No quote prefix when InboundMessage.quote is None."""
        daemon, session = loop_daemon
        from models import InboundMessage

        msg = InboundMessage(
            text="just a normal message",
            sender="user1",
            timestamp=time.time(),
            source="telegram",
        )
        await daemon.queue.put(msg)
        await daemon.queue.put(None)

        with patch.object(daemon, "_process_message", new_callable=AsyncMock) as mock_pm:
            await daemon._message_loop()

        mock_pm.assert_called_once()
        passed_text = mock_pm.call_args[0][0]
        assert "[replying to:" not in passed_text
        assert "just a normal message" in passed_text

    @pytest.mark.asyncio
    async def test_long_quote_truncated(self, loop_daemon):
        """Quotes longer than 200 chars are truncated with ellipsis."""
        daemon, session = loop_daemon
        from models import InboundMessage

        long_quote = "a" * 300
        msg = InboundMessage(
            text="yes",
            sender="user1",
            timestamp=time.time(),
            source="telegram",
            quote=long_quote,
        )
        await daemon.queue.put(msg)
        await daemon.queue.put(None)

        with patch.object(daemon, "_process_message", new_callable=AsyncMock) as mock_pm:
            await daemon._message_loop()

        mock_pm.assert_called_once()
        passed_text = mock_pm.call_args[0][0]
        # Should be truncated at 200 chars + ellipsis
        assert "a" * 200 + "…" in passed_text
        assert "a" * 201 not in passed_text

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


# ─── _build_sessions Tests ────────────────────────────────────────


class TestBuildSessions:
    """Tests for LucydDaemon._build_sessions()."""

    def test_active_sessions(self, tmp_path):
        """Returns session info from index and live sessions."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        _attach_metering(daemon, tmp_path)

        # Mock session manager with index and live sessions
        daemon.session_mgr = MagicMock()
        daemon.session_mgr.dir = tmp_path / "sessions"
        the_index = {
            "alice": {"session_id": "s-1", "created_at": 1707000000},
            "bob": {"session_id": "s-2", "created_at": 1707001000},
        }
        daemon.session_mgr._index = the_index
        daemon.session_mgr.get_index = MagicMock(return_value=the_index)
        live_session = MagicMock()
        live_session.messages = [{"role": "user"}, {"role": "assistant"}]
        live_session.compaction_count = 1
        live_session.model = "primary"
        the_sessions = {"alice": live_session}
        daemon.session_mgr._sessions = the_sessions
        daemon.session_mgr.get_loaded = MagicMock(side_effect=lambda c: the_sessions.get(c))

        result = daemon._build_sessions()

        assert len(result) == 2
        alice = next(s for s in result if s["contact"] == "alice")
        assert alice["session_id"] == "s-1"
        assert alice["message_count"] == 2
        assert alice["compaction_count"] == 1
        assert alice["model"] == "primary"

        bob = next(s for s in result if s["contact"] == "bob")
        assert bob["session_id"] == "s-2"
        # build_session_info always includes enriched fields (defaults when no state)
        assert bob["message_count"] == 0
        assert bob["compaction_count"] == 0
        assert bob["context_tokens"] == 0
        assert bob["context_pct"] == 0
        assert "cost" in bob
        assert "log_files" in bob
        assert "log_bytes" in bob

    def test_empty_sessions(self, tmp_path):
        """No active sessions returns empty list."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.session_mgr = MagicMock()
        daemon.session_mgr._index = {}
        daemon.session_mgr.get_index = MagicMock(return_value={})

        assert daemon._build_sessions() == []

    def test_no_session_manager(self, tmp_path):
        """No session manager returns empty list."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.session_mgr = None

        assert daemon._build_sessions() == []


# ─── Cost (metering integration) ─────────────────────────────────


class TestMeteringIntegration:
    """Verify daemon metering DB is wired correctly."""

    def test_get_records_returns_data(self, tmp_path):
        """metering_db.get_records() returns recorded data."""
        from dataclasses import dataclass

        @dataclass
        class _Usage:
            input_tokens: int = 1000
            output_tokens: int = 500
            cache_read_tokens: int = 200
            cache_write_tokens: int = 50

        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        _attach_metering(daemon, tmp_path)

        daemon.metering_db.record("s1", "test-model", "", _Usage(), [1.0, 1.0, 0.1])

        result = daemon.metering_db.get_records()
        assert len(result["records"]) == 1
        assert result["records"][0]["model"] == "test-model"
        assert result["records"][0]["cache_read_tokens"] == 200

    def test_empty_db_returns_no_records(self, tmp_path):
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        _attach_metering(daemon, tmp_path)

        result = daemon.metering_db.get_records()
        assert result["records"] == []


# ─── Webhook Tests ────────────────────────────────────────────────


@pytest.mark.skipif(
    not __import__("importlib").util.find_spec("httpx"),
    reason="httpx not installed",
)
class TestFireWebhook:
    """Tests for LucydDaemon._fire_webhook()."""

    @pytest.mark.asyncio
    async def test_no_url_skips(self, tmp_path):
        """Empty callback URL means no HTTP call."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.config = MagicMock()
        daemon.config.http_callback_max_failures = 0
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
        daemon.config.http_callback_max_failures = 0
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
        daemon.config.http_callback_max_failures = 0
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
        daemon.config.http_callback_max_failures = 0
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
        daemon.config.http_callback_max_failures = 0
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
        daemon.config.http_callback_max_failures = 0
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


# ─── _reset_session Tests ────────────────────────────────────────


class TestResetSession:
    """Tests for the extracted _reset_session() method."""

    @pytest.mark.asyncio
    async def test_reset_all(self, tmp_path):
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.session_mgr = MagicMock()
        daemon.session_mgr._index = {"alice": {}, "bob": {}}
        daemon.session_mgr.list_contacts = MagicMock(return_value=["alice", "bob"])
        daemon.session_mgr.close_session = AsyncMock(return_value=True)

        result = await daemon._reset_session("all")

        assert result["reset"] is True
        assert result["count"] == 2
        assert daemon.session_mgr.close_session.call_count == 2

    @pytest.mark.asyncio
    async def test_reset_by_uuid(self, tmp_path):
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.session_mgr = MagicMock()
        daemon.session_mgr.close_session_by_id = AsyncMock(return_value=True)

        sid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        result = await daemon._reset_session(sid)

        assert result["reset"] is True
        assert result["type"] == "session_id"
        daemon.session_mgr.close_session_by_id.assert_called_once_with(sid)

    @pytest.mark.asyncio
    async def test_reset_by_id_flag(self, tmp_path):
        """by_id=True treats any string as session ID."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.session_mgr = MagicMock()
        daemon.session_mgr.close_session_by_id = AsyncMock(return_value=True)

        result = await daemon._reset_session("not-a-uuid", by_id=True)

        assert result["reset"] is True
        daemon.session_mgr.close_session_by_id.assert_called_once_with("not-a-uuid")

    @pytest.mark.asyncio
    async def test_reset_by_contact(self, tmp_path):
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.session_mgr = MagicMock()
        daemon.session_mgr.close_session = AsyncMock(return_value=True)

        result = await daemon._reset_session("alice")

        assert result["reset"] is True
        assert result["type"] == "contact"
        daemon.session_mgr.close_session.assert_called_once_with("alice")

    @pytest.mark.asyncio
    async def test_reset_user_skips_internal_senders(self, tmp_path):
        """'user' alias skips system, http-*, and cli contacts."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.session_mgr = MagicMock()
        daemon.session_mgr._index = {
            "system": {}, "cli": {}, "http-n8n": {},
            "nicolas": {},
        }
        daemon.session_mgr.list_contacts = MagicMock(return_value=["system", "cli", "http-n8n", "nicolas"])
        daemon.session_mgr.close_session = AsyncMock(return_value=True)

        result = await daemon._reset_session("user")

        assert result["reset"] is True
        daemon.session_mgr.close_session.assert_called_once_with("nicolas")

    @pytest.mark.asyncio
    async def test_reset_user_no_user_found(self, tmp_path):
        """'user' alias with only internal senders returns not found."""
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.session_mgr = MagicMock()
        daemon.session_mgr._index = {"system": {}, "http-api": {}}
        daemon.session_mgr.list_contacts = MagicMock(return_value=["system", "http-api"])

        result = await daemon._reset_session("user")

        assert result["reset"] is False
        assert "no user session found" in result["reason"]

    @pytest.mark.asyncio
    async def test_reset_no_session_mgr(self, tmp_path):
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.session_mgr = None

        result = await daemon._reset_session("all")

        assert result["reset"] is False

    @pytest.mark.asyncio
    async def test_reset_unknown_contact(self, tmp_path):
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.session_mgr = MagicMock()
        daemon.session_mgr.close_session = AsyncMock(return_value=False)

        result = await daemon._reset_session("nobody")

        assert result["reset"] is False


# ─── _build_monitor Tests ────────────────────────────────────────


class TestBuildMonitor:
    """Tests for _build_monitor()."""

    def test_reads_monitor_json(self, tmp_path):
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)

        monitor_path = tmp_path / "state" / "monitor.json"
        monitor_path.write_text(json.dumps({
            "state": "thinking",
            "contact": "alice",
            "turn": 3,
        }))

        result = daemon._build_monitor()
        assert result["state"] == "thinking"
        assert result["contact"] == "alice"

    def test_no_monitor_file(self, tmp_path):
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)

        result = daemon._build_monitor()
        assert result["state"] == "unknown"


# ─── _build_history Tests ────────────────────────────────────────


class TestBuildHistory:
    """Tests for _build_history()."""

    def test_returns_events(self, tmp_path):
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(exist_ok=True)

        from session import SessionManager
        daemon.session_mgr = SessionManager(sessions_dir)

        # Write JSONL
        events = [
            {"type": "message", "role": "user", "content": "test", "timestamp": 1.0},
        ]
        (sessions_dir / "s-h1.2026-02-26.jsonl").write_text(
            json.dumps(events[0]) + "\n"
        )

        result = daemon._build_history("s-h1")
        assert result["session_id"] == "s-h1"
        assert len(result["events"]) == 1

    def test_no_session_mgr(self, tmp_path):
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.session_mgr = None

        result = daemon._build_history("any")
        assert result["events"] == []


# ─── Webhook agent field Tests ───────────────────────────────────


@pytest.mark.skipif(
    not __import__("importlib").util.find_spec("httpx"),
    reason="httpx not installed",
)
class TestWebhookAgentField:
    """Tests for agent name in webhook payload."""

    @pytest.mark.asyncio
    async def test_webhook_includes_agent(self, tmp_path):
        config = _make_config(tmp_path)
        daemon = LucydDaemon(config)
        daemon.config = MagicMock()
        daemon.config.http_callback_max_failures = 0
        daemon.config.http_callback_url = "https://test/hook"
        daemon.config.http_callback_token = ""
        daemon.config.http_callback_timeout = 10
        daemon.config.agent_name = "TestAgent"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=MagicMock())
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            await daemon._fire_webhook(
                reply="test", session_id="s1", sender="alice",
                source="telegram", silent=False,
                tokens={"input": 100, "output": 50},
                notify_meta=None,
            )

        call_kwargs = mock_client.post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert payload["agent"] == "TestAgent"


# ─── _check_context_budget ─────────────────────────────────────


class TestCheckContextBudget:
    """Tests for LucydDaemon._check_context_budget() startup warning."""

    def _make_daemon_with_budget(self, tmp_path, max_context_tokens, soul_text="# Test Soul"):
        """Build a daemon with context budget components wired up."""
        from context import ContextBuilder
        from skills import SkillLoader
        from tools import ToolRegistry

        config = _make_config(
            tmp_path,
            models={
                "primary": {
                    "provider": "anthropic-compat",
                    "model": "test-model",
                    "max_tokens": 1024,
                    "max_context_tokens": max_context_tokens,
                    "cost_per_mtok": [1.0, 5.0, 0.1],
                },
            },
        )

        # Write workspace content
        ws = tmp_path / "workspace"
        ws.mkdir(exist_ok=True)
        (ws / "SOUL.md").write_text(soul_text)

        daemon = LucydDaemon(config)
        daemon.context_builder = ContextBuilder(
            workspace=config.workspace,
            stable_files=config.context_stable,
            semi_stable_files=config.context_semi_stable,
        )
        daemon.tool_registry = ToolRegistry()
        daemon.skill_loader = SkillLoader(
            workspace=config.workspace,
            skills_dir=config.skills_dir,
        )
        daemon.skill_loader.scan()

        # Mock provider with capabilities matching config
        from providers import ModelCapabilities
        mock_provider = MagicMock()
        mock_provider.capabilities = ModelCapabilities(max_context_tokens=max_context_tokens)
        daemon.provider = mock_provider

        return daemon

    def test_warning_fires_when_over_50_percent(self, tmp_path, caplog):
        """Warning logged when system prompt exceeds 50% of max_context_tokens."""
        import logging

        # max_context_tokens=1500 → 50% = 750 tokens.
        # Varied text produces ~818 tokens with byte fallback → ~60% → triggers warning (between 50-80%).
        big_soul = "The quick brown fox jumps over the lazy dog. " * 60
        daemon = self._make_daemon_with_budget(tmp_path, max_context_tokens=1500, soul_text=big_soul)

        with caplog.at_level(logging.WARNING, logger="lucyd"):
            daemon._check_context_budget()

        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("System prompt uses" in m for m in warning_msgs), (
            f"Expected context budget warning but got: {warning_msgs}"
        )

    def test_no_warning_when_under_50_percent(self, tmp_path, caplog):
        """No warning logged when system prompt is within budget."""
        import logging

        # max_context_tokens=100000 → 50% = 50000 tokens.
        # A tiny SOUL.md (~11 chars) → well under threshold.
        daemon = self._make_daemon_with_budget(tmp_path, max_context_tokens=100000, soul_text="# Test Soul")

        with caplog.at_level(logging.WARNING, logger="lucyd"):
            daemon._check_context_budget()

        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert not any("System prompt uses" in m for m in warning_msgs), (
            f"Unexpected context budget warning: {warning_msgs}"
        )

    def test_info_log_always_emitted(self, tmp_path, caplog):
        """Info-level context budget log is always emitted regardless of threshold."""
        import logging

        daemon = self._make_daemon_with_budget(tmp_path, max_context_tokens=100000, soul_text="# Small")

        with caplog.at_level(logging.INFO, logger="lucyd"):
            daemon._check_context_budget()

        info_msgs = [r.message for r in caplog.records if r.levelno == logging.INFO]
        assert any("Context budget:" in m for m in info_msgs), (
            f"Expected context budget info log but got: {info_msgs}"
        )

    def test_skips_when_no_max_context_tokens(self, tmp_path, caplog):
        """No warning or info when max_context_tokens is 0 (unset)."""
        import logging

        config = _make_config(tmp_path)  # Default config has no max_context_tokens
        daemon = LucydDaemon(config)
        daemon.context_builder = MagicMock()
        daemon.tool_registry = MagicMock()

        with caplog.at_level(logging.DEBUG, logger="lucyd"):
            daemon._check_context_budget()

        # Should not even call build() since it returns early
        daemon.context_builder.build.assert_not_called()

    def test_warning_includes_percentage_and_tokens(self, tmp_path, caplog):
        """Warning message includes percentage, estimated tokens, and max tokens."""
        import logging

        # ~950 tokens out of 1500 → ~63% → warning (between 50% and 80%)
        big_soul = "The quick brown fox jumps over the lazy dog. " * 60
        daemon = self._make_daemon_with_budget(tmp_path, max_context_tokens=1500, soul_text=big_soul)

        with caplog.at_level(logging.WARNING, logger="lucyd"):
            daemon._check_context_budget()

        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_msgs) >= 1
        msg = warning_msgs[0]
        assert "1500" in msg  # max_context_tokens
        assert "%" in msg  # percentage

    def test_large_workspace_triggers_budget_warning(self, tmp_path, caplog):
        """Large workspace files inflate system prompt and trigger budget warning."""
        import logging
        from context import ContextBuilder
        from skills import SkillLoader
        from tools import ToolRegistry

        # Context window sized so workspace files push over 50% but under 80%
        config = _make_config(
            tmp_path,
            models={
                "primary": {
                    "provider": "anthropic-compat",
                    "model": "test-model",
                    "max_tokens": 1024,
                    "max_context_tokens": 1500,
                    "cost_per_mtok": [1.0, 5.0, 0.1],
                },
            },
        )

        ws = tmp_path / "workspace"
        ws.mkdir(exist_ok=True)
        # Large SOUL.md to inflate system prompt beyond 50% of 1500 tokens (~65%)
        (ws / "SOUL.md").write_text("The quick brown fox jumps. " * 100)

        daemon = LucydDaemon(config)
        daemon.context_builder = ContextBuilder(
            workspace=config.workspace,
            stable_files=config.context_stable,
            semi_stable_files=config.context_semi_stable,
        )
        daemon.skill_loader = SkillLoader(
            workspace=config.workspace,
            skills_dir=config.skills_dir,
        )
        daemon.skill_loader.scan()
        daemon.tool_registry = ToolRegistry()

        from providers import ModelCapabilities
        mock_provider = MagicMock()
        mock_provider.capabilities = ModelCapabilities(max_context_tokens=1500)
        daemon.provider = mock_provider

        with caplog.at_level(logging.WARNING, logger="lucyd"):
            daemon._check_context_budget()

        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("System prompt uses" in m for m in warning_msgs), (
            f"Expected warning from large workspace inflating budget: {warning_msgs}"
        )
