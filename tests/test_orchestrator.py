"""Contract tests for _process_message and unit tests for extracted decisions.

Phase 2: Contract tests verify _process_message side effects through mocks.
Phase 3: Unit tests verify extracted pure functions directly.

Following LUCYD-ORCHESTRATOR-TESTING-MANUAL.md:
- _process_message returns None — verify through mock side effects
- Mock everything in the "What Must Be Mocked" table
- AsyncMock for channel and provider
- Uses _make_daemon_for_monitor pattern from test_monitor.py as template
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lucyd import LucydDaemon
from pipeline import _inject_warning, _should_warn_context

_TEST_DAEMONS: list[LucydDaemon] = []

# ─── Helpers ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _cleanup_daemon_memory_conns():
    yield
    while _TEST_DAEMONS:
        daemon = _TEST_DAEMONS.pop()
        conn = getattr(daemon, "_memory_conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            daemon._memory_conn = None


def _deep_merge(base, overrides):
    for key, val in overrides.items():
        if key in base and isinstance(base[key], dict) and isinstance(val, dict):
            _deep_merge(base[key], val)
        else:
            base[key] = val
    return base


def _make_config(tmp_path, **overrides):
    """Build a complete Config for testing daemon methods."""
    from config import Config

    base = {
        "agent": {
            "name": "TestAgent",
            "workspace": str(tmp_path / "workspace"),
            "context": {"stable": ["SOUL.md"], "semi_stable": []},
            "skills": {"dir": "skills", "always_on": []},
        },
        "user": {"name": "testuser"},
        "http": {
            "enabled": False, "host": "127.0.0.1", "port": 8100, "token_env": "",
            "download_dir": "/tmp/lucyd-http", "max_body_bytes": 10485760,
            "max_attachment_bytes": 52428800,
            "rate_limit": 30, "rate_window": 60, "status_rate_limit": 60,
            "rate_limit_cleanup_threshold": 1000,
        },
        "models": {
            "primary": {
                "provider": "anthropic", "model": "test-model",
                "max_tokens": 1024, "cost_per_mtok": [1.0, 5.0, 0.1],
                "supports_vision": True,
            },
        },
        "memory": {
            "db": "", "search_top_k": 10, "vector_search_limit": 10000,
            "embedding_timeout": 15,
            "consolidation": {"enabled": False, "confidence_threshold": 0.6},
            "recall": {
                "decay_rate": 0.03, "max_facts_in_context": 20, "max_dynamic_tokens": 1500, "max_episodes_at_start": 3, "archive_messages": 20,
                "personality": {"priority_vector": 35, "priority_episodes": 25, "priority_facts": 15, "priority_commitments": 40,
                               "fact_format": "natural", "show_emotional_tone": True, "episode_section_header": "Recent conversations"},
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
                      "text_extensions": [".txt", ".md"]},
        "logging": {"suppress": []},
        "vision": {"max_image_bytes": 5242880, "max_dimension": 1568,
                   "jpeg_quality_steps": [85, 60, 40],
                   },
        "behavior": {
            "silent_tokens": ["NO_REPLY"], "typing_indicators": True, "error_message": "error", "debounce_ms": 500,
            "api_retries": 2, "api_retry_base_delay": 2.0, "message_retries": 2, "message_retry_base_delay": 30.0,
            "agent_timeout_seconds": 600,
            "max_turns_per_message": 50, "max_cost_per_message": 0.0,
            "notify_target": "",
            "compaction": {
                "threshold_tokens": 150000, "max_tokens": 2048,
                "prompt": "Summarize for {agent_name}.", "keep_recent_pct": 0.33,
                "keep_recent_pct_min": 0.05, "keep_recent_pct_max": 0.9,
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

    (tmp_path / "workspace").mkdir(exist_ok=True)
    (tmp_path / "workspace" / "SOUL.md").write_text("# Test Soul")
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "sessions").mkdir(exist_ok=True)

    return Config(base)


def _make_daemon(tmp_path):
    """Build a daemon rigged for contract testing.

    Returns (daemon, provider, session).
    Based on _make_daemon_for_monitor in test_monitor.py.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)

    config = _make_config(tmp_path)
    daemon = LucydDaemon(config)
    _TEST_DAEMONS.append(daemon)

    provider = MagicMock()
    provider.format_system = MagicMock(return_value=[])
    provider.format_messages = MagicMock(return_value=[])
    provider.format_tools = MagicMock(return_value=[])
    provider.capabilities.max_context_tokens = 200000
    provider.capabilities.supports_tools = True
    provider.capabilities.supports_streaming = False
    daemon.provider = provider
    daemon._providers = {"primary": provider}

    session = MagicMock()
    session.id = "test-session-001"
    session.messages = []
    session.pending_system_warning = ""
    session.last_input_tokens = 0
    session.needs_compaction = MagicMock(return_value=False)
    session.warned_about_compaction = False
    session.add_user_message = AsyncMock()
    session.add_assistant_message = AsyncMock()
    session.add_tool_results = AsyncMock()
    session.save_state = AsyncMock()

    daemon.session_mgr = MagicMock()
    daemon.session_mgr.get_or_create = AsyncMock(return_value=session)
    daemon.session_mgr.close_session = AsyncMock(return_value=True)
    daemon.session_mgr.save_state = AsyncMock(side_effect=lambda s: s.save_state())
    daemon.session_mgr.has_session = AsyncMock(return_value=False)
    daemon.session_mgr.list_contacts = AsyncMock(return_value=[])
    daemon.session_mgr.list_sessions = MagicMock(return_value=[])
    daemon.session_mgr.session_count = AsyncMock(return_value=0)
    daemon.session_mgr.get_index = AsyncMock(return_value={})
    daemon.session_mgr.get_loaded = MagicMock(return_value=None)

    daemon.context_builder = MagicMock()
    daemon.context_builder.build = MagicMock(return_value=[])

    daemon.skill_loader = MagicMock()
    daemon.skill_loader.build_index = MagicMock(return_value="")
    daemon.skill_loader.get_bodies = MagicMock(return_value={})

    daemon.tool_registry = MagicMock()
    daemon.tool_registry.get_schemas = MagicMock(return_value=[])

    daemon.config = MagicMock()
    daemon.config.state_dir = state_dir
    daemon.config.model_config = MagicMock(return_value={
        "model": "test-model", "cost_per_mtok": [1.0, 5.0, 0.1],
        "supports_vision": True,
    })
    daemon.config.typing_indicators = False
    daemon.config.max_turns = 10
    daemon.config.agent_timeout = 30
    daemon.config.agent_id = "test"
    daemon.config.silent_tokens = []
    daemon.config.compaction_threshold = 150000
    daemon.config.always_on_skills = []
    daemon.config.error_message = "Something went wrong."
    daemon.config.message_retries = 0
    daemon.config.message_retry_base_delay = 0.01
    daemon.config.raw = MagicMock(return_value=0.0)
    daemon.config.compaction_max_tokens = 2048
    daemon.config.compaction_prompt = "Compact this."
    daemon.config.agent_name = "TestAgent"
    daemon.config.consolidation_enabled = False
    daemon.config.notify_target = ""

    daemon.metering_db = None

    return daemon, provider, session


def _make_response(text="ok", stop_reason="end_turn", tool_calls=None,
                   input_tokens=1000, output_tokens=100,
                   cache_read_tokens=0, cache_write_tokens=0):
    """Build a mock LLMResponse."""
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    usage.cache_read_tokens = cache_read_tokens
    usage.cache_write_tokens = cache_write_tokens

    response = MagicMock()
    response.text = text
    response.stop_reason = stop_reason
    response.tool_calls = tool_calls or []
    response.usage = usage
    response.cost_limited = False
    response.turns = 1
    response.attachments = []
    return response


# ─── Phase 3: Extracted Function Unit Tests ──────────────────────


class TestShouldWarnContext:
    """Unit tests for _should_warn_context."""

    def test_warns_above_80pct(self):
        """120001 tokens > 80% of 150000 → should warn."""
        assert _should_warn_context(
            input_tokens=120001,
            compaction_threshold=150000,
            needs_compaction=False,
            already_warned=False,
            warning_pct=0.8,
        ) is True

    def test_no_warn_below_threshold(self):
        """119999 tokens < 80% of 150000 → no warn."""
        assert _should_warn_context(
            input_tokens=119999,
            compaction_threshold=150000,
            needs_compaction=False,
            already_warned=False,
            warning_pct=0.8,
        ) is False

    def test_no_warn_at_exact_threshold(self):
        """Exactly at threshold (120000) → no warn (> not >=)."""
        assert _should_warn_context(
            input_tokens=120000,
            compaction_threshold=150000,
            needs_compaction=False,
            already_warned=False,
            warning_pct=0.8,
        ) is False

    def test_no_warn_if_needs_compaction(self):
        """If already at hard compaction, skip warning."""
        assert _should_warn_context(
            input_tokens=160000,
            compaction_threshold=150000,
            needs_compaction=True,
            already_warned=False,
            warning_pct=0.8,
        ) is False

    def test_no_warn_if_already_warned(self):
        """If already warned this session, don't repeat."""
        assert _should_warn_context(
            input_tokens=130000,
            compaction_threshold=150000,
            needs_compaction=False,
            already_warned=True,
            warning_pct=0.8,
        ) is False

    def test_no_warn_zero_tokens(self):
        """Zero tokens → no warn."""
        assert _should_warn_context(
            input_tokens=0,
            compaction_threshold=150000,
            needs_compaction=False,
            already_warned=False,
            warning_pct=0.8,
        ) is False

    def test_custom_warning_pct(self):
        """Custom warning_pct (0.5) changes the threshold."""
        # 50% of 100000 = 50000
        assert _should_warn_context(
            input_tokens=50001,
            compaction_threshold=100000,
            needs_compaction=False,
            already_warned=False,
            warning_pct=0.5,
        ) is True
        assert _should_warn_context(
            input_tokens=49999,
            compaction_threshold=100000,
            needs_compaction=False,
            already_warned=False,
            warning_pct=0.5,
        ) is False


class TestInjectWarning:
    """Unit tests for _inject_warning."""

    def test_injects_warning(self):
        text, consumed = _inject_warning("hello", "Context is getting long")
        assert consumed is True
        assert text == "[system: Context is getting long]\n\nhello"

    def test_no_warning_empty_string(self):
        text, consumed = _inject_warning("hello", "")
        assert consumed is False
        assert text == "hello"

    def test_preserves_original_text(self):
        """Original text intact after injection."""
        text, consumed = _inject_warning("line1\nline2", "warning!")
        assert consumed is True
        assert "line1\nline2" in text

    def test_warning_format_matches_original(self):
        """Format matches: [system: WARNING]\\n\\nTEXT"""
        text, _ = _inject_warning("msg", "warn")
        assert text == "[system: warn]\n\nmsg"


# ─── Phase 2: Contract Tests ─────────────────────────────────────


class TestBasicMessageFlow:
    """Contract: message in → reply delivered via channel."""

    @pytest.mark.asyncio
    async def test_reply_delivered_to_channel(self, tmp_path):
        """Provider response text is returned via response_future."""
        daemon, provider, session = _make_daemon(tmp_path)
        response = _make_response(text="Hello, Nicolas!")

        async def fake_loop(**kwargs):
            return response

        loop = asyncio.get_running_loop()
        future = loop.create_future()

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="hi", sender="Nicolas", talker="user", channel="telegram",
                response_future=future,
            )

        result = future.result()
        assert result["reply"] == "Hello, Nicolas!"

    @pytest.mark.asyncio
    async def test_session_get_or_create_called(self, tmp_path):
        """get_or_create called with sender and model."""
        daemon, provider, session = _make_daemon(tmp_path)
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="hello", sender="alice", talker="user", channel="telegram",
            )

        daemon.session_mgr.get_or_create.assert_called_once_with(
            "user:alice"
        )

    @pytest.mark.asyncio
    async def test_user_message_added_to_session(self, tmp_path):
        """add_user_message called with text containing timestamp."""
        daemon, provider, session = _make_daemon(tmp_path)
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="hello", sender="user", talker="user", channel="telegram",
            )

        session.add_user_message.assert_called_once()
        call_text = session.add_user_message.call_args[0][0]
        # Timestamp is prepended
        assert "hello" in call_text
        assert call_text.startswith("[")


class TestProviderErrorHandling:
    """Contract: provider/agentic loop errors → graceful error message, no crash."""

    @pytest.mark.asyncio
    async def test_error_sends_graceful_message(self, tmp_path):
        """Agentic loop raises → error returned via response_future."""
        daemon, provider, session = _make_daemon(tmp_path)

        async def fake_loop(**kwargs):
            raise RuntimeError("API connection failed")

        loop = asyncio.get_running_loop()
        future = loop.create_future()

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="hello", sender="user", talker="user", channel="telegram",
                response_future=future,
            )

        result = future.result()
        assert "error" in result

    @pytest.mark.asyncio
    async def test_error_does_not_crash(self, tmp_path):
        """Agentic loop raises → _process_message completes without raising."""
        daemon, provider, session = _make_daemon(tmp_path)

        async def fake_loop(**kwargs):
            raise ValueError("bad input")

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            # Should not raise
            await daemon._process_message(
                text="hello", sender="user", talker="user", channel="telegram",
            )

    @pytest.mark.asyncio
    async def test_error_no_delivery_for_system_source(self, tmp_path):
        """System source error → no crash, completes without raising."""
        daemon, provider, session = _make_daemon(tmp_path)

        async def fake_loop(**kwargs):
            raise RuntimeError("API down")

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="heartbeat", sender="system", talker="system",
            )

    @pytest.mark.asyncio
    async def test_no_provider_returns_early(self, tmp_path):
        """No provider configured → early return, no agentic loop."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.provider = None
        daemon._providers = {}

        with patch("pipeline.run_agentic_loop") as mock_loop:
            await daemon._process_message(
                text="hello", sender="user", talker="user", channel="telegram",
            )

        mock_loop.assert_not_called()


class TestSilentTokenSuppression:
    """Contract: silent token replies are not delivered to channel."""

    @pytest.mark.asyncio
    async def test_silent_reply_not_delivered(self, tmp_path):
        """Reply matching silent_tokens → future resolved with silent=True."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.silent_tokens = ["HEARTBEAT_OK"]
        response = _make_response(text="HEARTBEAT_OK")

        async def fake_loop(**kwargs):
            return response

        loop = asyncio.get_running_loop()
        future = loop.create_future()

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="run heartbeat", sender="user", talker="user", channel="telegram",
                response_future=future,
            )

        result = future.result()
        assert result["silent"] is True

    @pytest.mark.asyncio
    async def test_non_silent_reply_delivered(self, tmp_path):
        """Reply not matching silent_tokens → future resolved with reply."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.silent_tokens = ["HEARTBEAT_OK"]
        response = _make_response(text="Here's your answer!")

        async def fake_loop(**kwargs):
            return response

        loop = asyncio.get_running_loop()
        future = loop.create_future()

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="hello", sender="user", talker="user", channel="telegram",
                response_future=future,
            )

        result = future.result()
        assert result["reply"] == "Here's your answer!"


class TestWarningInjection:
    """Contract: pending_system_warning is prepended and consumed."""

    @pytest.mark.asyncio
    async def test_warning_prepended_to_text(self, tmp_path):
        """pending_system_warning → text includes warning prefix."""
        daemon, provider, session = _make_daemon(tmp_path)
        session.pending_system_warning = "Context is getting long"
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="hello", sender="user", talker="user", channel="telegram",
            )

        # Warning consumed
        assert session.pending_system_warning == ""
        # Check the text passed to add_user_message includes the warning
        call_text = session.add_user_message.call_args[0][0]
        assert "[system: Context is getting long]" in call_text
        assert "hello" in call_text

    @pytest.mark.asyncio
    async def test_warning_consumed_persists_before_agentic_loop(self, tmp_path):
        """Cleared warning is saved to state before the agentic loop runs."""
        daemon, provider, session = _make_daemon(tmp_path)
        session.pending_system_warning = "Context warning"
        response = _make_response()

        call_order = []

        async def tracking_save_state(s):
            call_order.append("save_state")

        daemon.session_mgr.save_state = AsyncMock(side_effect=tracking_save_state)

        original_add = session.add_user_message

        async def tracking_add(*a, **kw):
            call_order.append("add_user_message")

        session.add_user_message = AsyncMock(side_effect=tracking_add)

        async def fake_loop(**kwargs):
            call_order.append("agentic_loop")
            return response

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="hello", sender="user", talker="user", channel="telegram",
            )

        # _save_state must be called BEFORE agentic_loop
        assert "save_state" in call_order
        assert "agentic_loop" in call_order
        save_idx = call_order.index("save_state")
        loop_idx = call_order.index("agentic_loop")
        assert save_idx < loop_idx, "Warning clear must be persisted before agentic loop"

    @pytest.mark.asyncio
    async def test_no_warning_when_empty(self, tmp_path):
        """No pending warning → text not modified with [system:]."""
        daemon, provider, session = _make_daemon(tmp_path)
        session.pending_system_warning = ""
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="hello", sender="user", talker="user", channel="telegram",
            )

        call_text = session.add_user_message.call_args[0][0]
        assert "[system:" not in call_text


class TestCompactionWarning:
    """Contract: warning threshold sets pending_system_warning on session."""

    @pytest.mark.asyncio
    async def test_warning_set_above_80pct(self, tmp_path):
        """Session > 80% of threshold → pending_system_warning set."""
        daemon, provider, session = _make_daemon(tmp_path)
        session.last_input_tokens = 130000  # > 80% of 150000 = 120000
        session.needs_compaction = MagicMock(return_value=False)
        session.warned_about_compaction = False
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.MAX_CONTEXT_TOKENS", 200000):
                await daemon._process_message(
                    text="hello", sender="user", talker="user", channel="telegram",
                )

        assert session.pending_system_warning != ""
        assert "130,000" in session.pending_system_warning
        assert session.warned_about_compaction is True
        session.save_state.assert_called()

    @pytest.mark.asyncio
    async def test_no_warning_below_80pct(self, tmp_path):
        """Session < 80% of threshold → no warning."""
        daemon, provider, session = _make_daemon(tmp_path)
        session.last_input_tokens = 100000  # < 120000
        session.needs_compaction = MagicMock(return_value=False)
        session.warned_about_compaction = False
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="hello", sender="user", talker="user", channel="telegram",
            )

        assert session.pending_system_warning == "" or session.pending_system_warning is None or not session.pending_system_warning

    @pytest.mark.asyncio
    async def test_no_double_warning(self, tmp_path):
        """Already warned → no second warning."""
        daemon, provider, session = _make_daemon(tmp_path)
        session.last_input_tokens = 130000
        session.needs_compaction = MagicMock(return_value=False)
        session.warned_about_compaction = True  # already warned
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="hello", sender="user", talker="user", channel="telegram",
            )

        # pending_system_warning should stay empty (not set again)
        assert not session.pending_system_warning or session.pending_system_warning == ""

    @pytest.mark.asyncio
    async def test_zero_max_context_tokens_no_crash(self, tmp_path):
        """BUG-8: MAX_CONTEXT_TOKENS == 0 must not cause ZeroDivisionError."""
        daemon, provider, session = _make_daemon(tmp_path)
        session.last_input_tokens = 130000
        session.needs_compaction = MagicMock(return_value=False)
        session.warned_about_compaction = False
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.MAX_CONTEXT_TOKENS", 0):
                await daemon._process_message(
                    text="hello", sender="user", talker="user", channel="telegram",
                )

        # Should not crash; warning may or may not be set but no ZeroDivisionError


class TestHardCompaction:
    """Contract: hard compaction triggers when session.needs_compaction is True."""

    @pytest.mark.asyncio
    async def test_compaction_triggered(self, tmp_path):
        """needs_compaction → compact_session called."""
        daemon, provider, session = _make_daemon(tmp_path)
        session.needs_compaction = MagicMock(return_value=True)
        session.last_input_tokens = 160000
        session.warned_about_compaction = True
        daemon.session_mgr.compact_session = AsyncMock()
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="hello", sender="user", talker="user", channel="telegram",
            )

        daemon.session_mgr.compact_session.assert_called_once()
        args = daemon.session_mgr.compact_session.call_args[0]
        assert args[0] is session
        assert args[1] is provider
        assert isinstance(args[2], str) and len(args[2]) > 0

    @pytest.mark.asyncio
    async def test_no_compaction_under_threshold(self, tmp_path):
        """needs_compaction=False → compact_session not called."""
        daemon, provider, session = _make_daemon(tmp_path)
        session.needs_compaction = MagicMock(return_value=False)
        daemon.session_mgr.compact_session = AsyncMock()
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="hello", sender="user", talker="user", channel="telegram",
            )

        daemon.session_mgr.compact_session.assert_not_called()


class TestHTTPFutureResolution:
    """Contract: HTTP response_future is resolved with reply."""

    @pytest.mark.asyncio
    async def test_future_resolved_with_reply(self, tmp_path):
        """response_future gets set_result with reply dict."""
        daemon, provider, session = _make_daemon(tmp_path)
        response = _make_response(text="answer", input_tokens=500, output_tokens=50)
        future = asyncio.get_event_loop().create_future()

        async def fake_loop(**kwargs):
            return response

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="question", sender="api", talker="operator",
                response_future=future,
            )

        assert future.done()
        result = future.result()
        assert result["reply"] == "answer"
        assert result["session_id"] == "test-session-001"
        assert result["tokens"]["input"] == 500
        assert result["tokens"]["output"] == 50

    @pytest.mark.asyncio
    async def test_future_resolved_on_error(self, tmp_path):
        """On agentic loop error, future resolved with error dict."""
        daemon, provider, session = _make_daemon(tmp_path)
        future = asyncio.get_event_loop().create_future()

        async def fake_loop(**kwargs):
            raise RuntimeError("API exploded")

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="question", sender="api", talker="operator",
                response_future=future,
            )

        assert future.done()
        result = future.result()
        assert "error" in result

    @pytest.mark.asyncio
    async def test_future_resolved_for_silent_reply(self, tmp_path):
        """Silent reply → future still resolved (with silent=True)."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.silent_tokens = ["HEARTBEAT_OK"]
        response = _make_response(text="HEARTBEAT_OK")
        future = asyncio.get_event_loop().create_future()

        async def fake_loop(**kwargs):
            return response

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="heartbeat", sender="system", talker="operator",
                response_future=future,
            )

        assert future.done()
        result = future.result()
        assert result["silent"] is True

    @pytest.mark.asyncio
    async def test_no_future_no_crash(self, tmp_path):
        """response_future=None → no crash."""
        daemon, provider, session = _make_daemon(tmp_path)
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="hello", sender="user", talker="user", channel="telegram",
                response_future=None,
            )


class TestMessagePersistence:
    """Contract: new messages from agentic loop are persisted."""

    @pytest.mark.asyncio
    async def test_assistant_messages_persisted(self, tmp_path):
        """Assistant messages appended by loop → add_assistant_message(persist_only=True) called."""
        daemon, provider, session = _make_daemon(tmp_path)
        response = _make_response(text="reply")

        async def fake_loop(**kwargs):
            # Simulate agentic loop appending messages
            kwargs["messages"].append({"role": "agent", "content": "reply"})
            return response

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="hello", sender="user", talker="user", channel="telegram",
            )

        session.add_assistant_message.assert_called_once_with(
            {"role": "agent", "content": "reply"}, persist_only=True
        )

    @pytest.mark.asyncio
    async def test_tool_results_persisted(self, tmp_path):
        """Tool result messages → add_tool_results(persist_only=True) called."""
        daemon, provider, session = _make_daemon(tmp_path)
        response = _make_response(text="reply")

        async def fake_loop(**kwargs):
            kwargs["messages"].append({
                "role": "tool_result",
                "results": [{"tool_use_id": "tc-1", "content": "done"}],
            })
            kwargs["messages"].append({"role": "agent", "content": "reply"})
            return response

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="hello", sender="user", talker="user", channel="telegram",
            )

        session.add_tool_results.assert_called_once_with(
            [{"tool_use_id": "tc-1", "content": "done"}], persist_only=True
        )

    @pytest.mark.asyncio
    async def test_state_saved_after_processing(self, tmp_path):
        """_save_state called after message processing."""
        daemon, provider, session = _make_daemon(tmp_path)
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="hello", sender="user", talker="user", channel="telegram",
            )

        session.save_state.assert_called()


# ─── Memory v2 Wiring Contract Tests ─────────────────────────────


class TestMemoryV2Wiring:
    """Contract: Memory v2 structured recall and consolidation wiring."""

    @pytest.mark.asyncio
    async def test_structured_recall_injected_at_session_start(self, tmp_path):
        """When consolidation_enabled and first message, structured recall is injected."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.consolidation_enabled = True
        # First message: session.messages is empty before add_user_message
        session.messages = []
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        mock_context = "Facts:\n- nicolas — lives in: Austria"
        daemon.pool = MagicMock()

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            with patch("pipeline.get_session_start_context", return_value=mock_context) as mock_gsc:
                await daemon._process_message(
                    text="hello", sender="user", talker="user", channel="telegram",
                )

        mock_gsc.assert_called_once()
        # Verify context_builder.build received the recall text
        build_kwargs = daemon.context_builder.build.call_args
        extra = build_kwargs.kwargs.get("extra_dynamic", "") or (
            build_kwargs[1].get("extra_dynamic", "") if len(build_kwargs) > 1 else ""
        )
        assert mock_context in extra

    @pytest.mark.asyncio
    async def test_no_structured_recall_when_disabled(self, tmp_path):
        """When consolidation_enabled=False, no structured recall."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.consolidation_enabled = False
        session.messages = []
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            with patch("pipeline.get_session_start_context") as mock_gsc:
                await daemon._process_message(
                    text="hello", sender="user", talker="user", channel="telegram",
                )

        mock_gsc.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_structured_recall_on_subsequent_messages(self, tmp_path):
        """Structured recall only on first message (len(messages) <= 1)."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.consolidation_enabled = True
        # Simulate existing messages (not first message)
        session.messages = [{"role": "user", "content": "prior"}, {"role": "agent", "content": "reply"}]
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            with patch("pipeline.get_session_start_context") as mock_gsc:
                await daemon._process_message(
                    text="hello", sender="user", talker="user", channel="telegram",
                )

        mock_gsc.assert_not_called()

    @pytest.mark.asyncio
    async def test_structured_recall_failure_does_not_crash(self, tmp_path):
        """Structured recall failure is caught — _process_message continues."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.consolidation_enabled = True
        session.messages = []
        response = _make_response(text="reply despite recall failure")

        async def fake_loop(**kwargs):
            return response

        loop = asyncio.get_running_loop()
        future = loop.create_future()

        daemon.pool = MagicMock()

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            with patch("pipeline.get_session_start_context", side_effect=Exception("DB corrupt")):
                await daemon._process_message(
                    text="hello", sender="user", talker="user", channel="telegram",
                    response_future=future,
                )

        # Should complete without crashing and deliver reply
        result = future.result()
        assert result["reply"] == "reply despite recall failure"

    @pytest.mark.asyncio
    async def test_pre_compaction_consolidation_called(self, tmp_path):
        """When needs_compaction and consolidation_enabled, consolidation runs before compact."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.consolidation_enabled = True
        session.needs_compaction = MagicMock(return_value=True)
        session.last_input_tokens = 160000
        session.warned_about_compaction = True
        session.compaction_count = 0
        daemon.session_mgr.compact_session = AsyncMock()
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        mock_result = {"facts_added": 3, "episode_id": "ep-1"}
        daemon.pool = MagicMock()

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            with patch("consolidation.consolidate_session", new_callable=AsyncMock, return_value=mock_result) as mock_consol:
                await daemon._process_message(
                    text="hello", sender="user", talker="user", channel="telegram",
                )

        mock_consol.assert_called_once()
        # Compaction should also proceed
        daemon.session_mgr.compact_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_consolidation_failure_blocks_compaction(self, tmp_path):
        """If consolidation fails, compaction is skipped to prevent fact loss."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.consolidation_enabled = True
        session.needs_compaction = MagicMock(return_value=True)
        session.last_input_tokens = 160000
        session.warned_about_compaction = True
        session.compaction_count = 0
        daemon.session_mgr.compact_session = AsyncMock()
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        daemon.pool = MagicMock()

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            with patch("consolidation.consolidate_session", new_callable=AsyncMock, side_effect=RuntimeError("LLM timeout")):
                await daemon._process_message(
                    text="hello", sender="user", talker="user", channel="telegram",
                )

        # Compaction blocked — don't summarize unconsolidated messages
        daemon.session_mgr.compact_session.assert_not_called()
        assert session.consolidation_pending is True

    @pytest.mark.asyncio
    async def test_no_pre_compaction_consolidation_when_disabled(self, tmp_path):
        """When consolidation_enabled=False, no pre-compaction consolidation."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.consolidation_enabled = False
        session.needs_compaction = MagicMock(return_value=True)
        session.last_input_tokens = 160000
        session.warned_about_compaction = True
        daemon.session_mgr.compact_session = AsyncMock()
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            with patch("consolidation.consolidate_session", new_callable=AsyncMock) as mock_consol:
                await daemon._process_message(
                    text="hello", sender="user", talker="user", channel="telegram",
                )

        mock_consol.assert_not_called()
        # Compaction still proceeds
        daemon.session_mgr.compact_session.assert_called_once()


class TestConsolidateOnClose:
    """Contract: session close callback fires consolidation."""

    @pytest.mark.asyncio
    async def test_consolidate_on_close_calls_consolidation(self, tmp_path):
        """_consolidate_on_close calls consolidation.consolidate_session."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.pool = MagicMock()
        session.compaction_count = 0

        mock_result = {"facts_added": 1, "episode_id": "ep-close"}

        with patch("consolidation.get_unprocessed_range", return_value=(0, 5)):
            with patch("consolidation.consolidate_session", new_callable=AsyncMock, return_value=mock_result) as mock_consol:
                await daemon._consolidate_on_close(session)

        mock_consol.assert_called_once()

    @pytest.mark.asyncio
    async def test_consolidate_on_close_skips_when_no_unprocessed(self, tmp_path):
        """No unprocessed messages → consolidation not called."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.pool = MagicMock()
        session.compaction_count = 0

        with patch("consolidation.get_unprocessed_range", return_value=(5, 5)):
            with patch("consolidation.consolidate_session", new_callable=AsyncMock) as mock_consol:
                await daemon._consolidate_on_close(session)

        mock_consol.assert_not_called()

    @pytest.mark.asyncio
    async def test_consolidate_on_close_failure_does_not_crash(self, tmp_path):
        """Consolidation failure on close is caught — no exception propagated."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.pool = MagicMock()
        session.compaction_count = 0

        with patch("consolidation.get_unprocessed_range", side_effect=RuntimeError("DB locked")):
            # Should not raise
            await daemon._consolidate_on_close(session)


# ─── Error Recovery: Orphaned User Messages ──────────────────────


class TestErrorRecoveryOrphanedMessages:
    """Contract: agentic loop failure removes orphaned user message."""

    @pytest.mark.asyncio
    async def test_error_removes_orphaned_user_message(self, tmp_path):
        """Agentic loop error → orphaned user message popped from session."""
        daemon, provider, session = _make_daemon(tmp_path)

        # Make add_user_message actually append (real behavior)
        def fake_add_user(text, sender="", source=""):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = AsyncMock(side_effect=fake_add_user)

        async def fake_loop(**kwargs):
            raise RuntimeError("API returned 400")

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="hello", sender="user", talker="user", channel="telegram",
            )

        # Session must NOT end with an orphaned user message
        assert not session.messages or session.messages[-1].get("role") != "user"
        # State must be saved after cleanup
        session.save_state.assert_called()

    @pytest.mark.asyncio
    async def test_error_preserves_prior_assistant_message(self, tmp_path):
        """Error cleanup only removes the trailing user message, not earlier ones."""
        daemon, provider, session = _make_daemon(tmp_path)

        # Pre-populate with a valid exchange
        session.messages = [
            {"role": "user", "content": "first"},
            {"role": "agent", "content": "reply"},
        ]

        def fake_add_user(text, sender="", source=""):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = AsyncMock(side_effect=fake_add_user)

        async def fake_loop(**kwargs):
            raise RuntimeError("API error")

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="second", sender="user", talker="user", channel="telegram",
            )

        # Prior exchange intact, orphaned user message removed
        assert len(session.messages) == 2
        assert session.messages[-1]["role"] == "agent"

    @pytest.mark.asyncio
    async def test_error_with_image_blocks_cleans_up(self, tmp_path):
        """Error with image attachments → image blocks restored AND user message popped."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.vision_max_image_bytes = 10 * 1024 * 1024
        daemon.config.vision_max_dimension = 1568

        def fake_add_user(text, sender="", source=""):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = AsyncMock(side_effect=fake_add_user)

        async def fake_loop(**kwargs):
            raise RuntimeError("API rejected image")

        # Create a tiny fake image
        img_path = tmp_path / "test.jpg"
        img_path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

        from attachments import Attachment
        att = Attachment(
            content_type="image/jpeg",
            filename="test.jpg",
            local_path=str(img_path),
            size=104,
        )

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="look at this", sender="user", talker="user", channel="telegram",
                attachments=[att],
            )

        # Orphaned user message removed despite image blocks
        assert not session.messages or session.messages[-1].get("role") != "user"

    @pytest.mark.asyncio
    async def test_second_message_after_error_succeeds(self, tmp_path):
        """After error recovery, next message processes normally (no consecutive-user crash)."""
        daemon, provider, session = _make_daemon(tmp_path)

        call_count = [0]

        def fake_add_user(text, sender="", source=""):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = AsyncMock(side_effect=fake_add_user)

        response = _make_response(text="recovered!")

        async def fake_loop(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("First call fails")
            return response

        loop = asyncio.get_running_loop()
        future1 = loop.create_future()
        future2 = loop.create_future()

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            # First message — error
            await daemon._process_message(
                text="msg1", sender="user", talker="user", channel="telegram",
                response_future=future1,
            )
            # Second message — should succeed
            await daemon._process_message(
                text="msg2", sender="user", talker="user", channel="telegram",
                response_future=future2,
            )

        # Second call completed (loop was called twice)
        assert call_count[0] == 2
        # First call: error; second call: reply
        assert "error" in future1.result()
        assert future2.result()["reply"] == "recovered!"


# ─── Defense: Consecutive User Message Merge ─────────────────────


class TestConsecutiveUserMessageRejection:
    """Contract: consecutive user messages are rejected, not silently merged."""

    @pytest.mark.asyncio
    async def test_consecutive_user_message_rejected(self, tmp_path):
        """Adding a user message when last is already user → rejected."""
        daemon, provider, session = _make_daemon(tmp_path)

        session.messages = [{"role": "user", "content": "orphaned message"}]

        from session import ConsecutiveRoleError

        async def fake_add_user(text="", sender="", source=""):
            raise ConsecutiveRoleError("test")
        session.add_user_message = AsyncMock(side_effect=fake_add_user)

        result = await daemon._process_message(
            text="new message", sender="user", talker="user", channel="telegram",
            response_future=asyncio.get_event_loop().create_future(),
        )

    @pytest.mark.asyncio
    async def test_normal_alternation_works(self, tmp_path):
        """Normal alternating user/assistant → message added successfully."""
        daemon, provider, session = _make_daemon(tmp_path)

        session.messages = [
            {"role": "user", "content": "first"},
            {"role": "agent", "text": "reply"},
        ]

        def fake_add_user(text="", sender="", source=""):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = AsyncMock(side_effect=fake_add_user)

        response = _make_response(text="ok")

        async def fake_loop(**kwargs):
            return response

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="second", sender="user", talker="user", channel="telegram",
            )

        session.add_user_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_first_message_in_empty_session(self, tmp_path):
        """First message in empty session → no rejection."""
        daemon, provider, session = _make_daemon(tmp_path)
        session.messages = []

        def fake_add_user(text="", sender="", source=""):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = AsyncMock(side_effect=fake_add_user)

        response = _make_response(text="ok")

        async def fake_loop(**kwargs):
            return response

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="hello", sender="user", talker="user", channel="telegram",
            )

        session.add_user_message.assert_called_once()


# ─── Image Dimension Check ───────────────────────────────────────


# ─── Document Text Extraction ─────────────────────────────────────


class TestExtractDocumentText:
    """Unit tests for _extract_document_text."""

    def test_text_file_extracted(self, tmp_path):
        """Plain .txt file → content returned."""
        from attachments import extract_document_text as _extract_document_text

        f = tmp_path / "notes.txt"
        f.write_text("Hello, world!")
        result = _extract_document_text(
            str(f), "text/plain", "notes.txt",
            max_chars=30000, max_bytes=10_000_000,
            text_extensions=[".txt", ".md"],
        )
        assert result == "Hello, world!"

    def test_text_file_by_mime(self, tmp_path):
        """text/* MIME type → content returned even without matching extension."""
        from attachments import extract_document_text as _extract_document_text

        f = tmp_path / "data.unknown"
        f.write_text("MIME-based extraction")
        result = _extract_document_text(
            str(f), "text/csv", "data.unknown",
            max_chars=30000, max_bytes=10_000_000,
            text_extensions=[],  # no extension match
        )
        assert result == "MIME-based extraction"

    def test_truncation_at_max_chars(self, tmp_path):
        """Large text file truncated at max_chars."""
        from attachments import extract_document_text as _extract_document_text

        f = tmp_path / "big.txt"
        f.write_text("x" * 500)
        result = _extract_document_text(
            str(f), "text/plain", "big.txt",
            max_chars=100, max_bytes=10_000_000,
            text_extensions=[".txt"],
        )
        assert len(result) < 500
        assert result.startswith("x" * 100)
        assert "truncated at 100 chars" in result

    def test_non_readable_format_returns_none(self, tmp_path):
        """Unrecognized format (e.g. .psd) → None."""
        from attachments import extract_document_text as _extract_document_text

        f = tmp_path / "image.psd"
        f.write_bytes(b"\x00" * 100)
        result = _extract_document_text(
            str(f), "application/octet-stream", "image.psd",
            max_chars=30000, max_bytes=10_000_000,
            text_extensions=[".txt"],
        )
        assert result is None

    def test_file_too_large_returns_none(self, tmp_path):
        """File exceeding max_bytes → None."""
        from attachments import extract_document_text as _extract_document_text

        f = tmp_path / "huge.txt"
        f.write_bytes(b"x" * 200)
        result = _extract_document_text(
            str(f), "text/plain", "huge.txt",
            max_chars=30000, max_bytes=100,  # 100 bytes limit
            text_extensions=[".txt"],
        )
        assert result is None

    def test_pdf_returns_none(self, tmp_path):
        """PDF content type → None (PDFs handled by pdf_read tool, not extraction)."""
        from attachments import extract_document_text as _extract_document_text

        f = tmp_path / "doc.pdf"
        f.write_bytes(b"%PDF-1.4 fake")
        result = _extract_document_text(
            str(f), "application/pdf", "doc.pdf",
            max_chars=30000, max_bytes=10_000_000,
            text_extensions=[],
        )
        assert result is None

    def test_md_extension_extracted(self, tmp_path):
        """Markdown file matched by extension."""
        from attachments import extract_document_text as _extract_document_text

        f = tmp_path / "readme.md"
        f.write_text("# Hello\nWorld")
        result = _extract_document_text(
            str(f), "application/octet-stream", "readme.md",
            max_chars=30000, max_bytes=10_000_000,
            text_extensions=[".txt", ".md"],
        )
        assert result == "# Hello\nWorld"

    def test_empty_filename_uses_mime(self, tmp_path):
        """Empty filename → falls back to MIME type detection."""
        from attachments import extract_document_text as _extract_document_text

        f = tmp_path / "noname"
        f.write_text("plain text content")
        result = _extract_document_text(
            str(f), "text/plain", "",
            max_chars=30000, max_bytes=10_000_000,
            text_extensions=[],
        )
        assert result == "plain text content"


class TestDocumentExtractionIntegration:
    """Contract: document attachments are extracted or fall through to label."""

    @pytest.mark.asyncio
    async def test_text_document_injected(self, tmp_path):
        """Text file attachment → [document: file.txt] with content in text."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.documents_enabled = True
        daemon.config.documents_max_chars = 30000
        daemon.config.documents_max_file_bytes = 10_000_000
        daemon.config.documents_text_extensions = [".txt"]

        doc_path = tmp_path / "notes.txt"
        doc_path.write_text("Meeting notes here")

        def fake_add_user(text, sender="", source=""):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = AsyncMock(side_effect=fake_add_user)

        response = _make_response(text="Got it")

        from attachments import Attachment
        att = Attachment(content_type="text/plain", local_path=str(doc_path),
                         filename="notes.txt", size=doc_path.stat().st_size)

        with patch("pipeline.run_agentic_loop", return_value=response):
            await daemon._process_message(
                text="check this", sender="user", talker="user", channel="telegram",
                attachments=[att],
            )

        call_text = session.add_user_message.call_args[0][0]
        assert "[document: notes.txt, saved:" in call_text
        assert "Meeting notes here" in call_text

    @pytest.mark.asyncio
    async def test_non_readable_falls_to_label(self, tmp_path):
        """Non-extractable file → [attachment: file, type] label."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.documents_enabled = True
        daemon.config.documents_max_chars = 30000
        daemon.config.documents_max_file_bytes = 10_000_000
        daemon.config.documents_text_extensions = [".txt"]

        doc_path = tmp_path / "design.psd"
        doc_path.write_bytes(b"\x00" * 50)

        def fake_add_user(text, sender="", source=""):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = AsyncMock(side_effect=fake_add_user)

        response = _make_response(text="ok")

        from attachments import Attachment
        att = Attachment(content_type="application/octet-stream", local_path=str(doc_path),
                         filename="design.psd", size=50)

        with patch("pipeline.run_agentic_loop", return_value=response):
            await daemon._process_message(
                text="", sender="user", talker="user", channel="telegram",
                attachments=[att],
            )

        call_text = session.add_user_message.call_args[0][0]
        assert "[attachment: design.psd, application/octet-stream, saved:" in call_text

    @pytest.mark.asyncio
    async def test_documents_disabled_falls_to_label(self, tmp_path):
        """documents_enabled=False → all documents get label-only."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.documents_enabled = False

        doc_path = tmp_path / "readme.txt"
        doc_path.write_text("This should not be extracted")

        def fake_add_user(text, sender="", source=""):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = AsyncMock(side_effect=fake_add_user)

        response = _make_response(text="ok")

        from attachments import Attachment
        att = Attachment(content_type="text/plain", local_path=str(doc_path),
                         filename="readme.txt", size=doc_path.stat().st_size)

        with patch("pipeline.run_agentic_loop", return_value=response):
            await daemon._process_message(
                text="", sender="user", talker="user", channel="telegram",
                attachments=[att],
            )

        call_text = session.add_user_message.call_args[0][0]
        assert "[attachment: readme.txt, text/plain, saved:" in call_text
        assert "This should not be extracted" not in call_text

    @pytest.mark.asyncio
    async def test_oversized_document_falls_to_label(self, tmp_path):
        """File exceeding max_file_bytes → label-only."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.documents_enabled = True
        daemon.config.documents_max_chars = 30000
        daemon.config.documents_max_file_bytes = 50  # very small limit
        daemon.config.documents_text_extensions = [".txt"]

        doc_path = tmp_path / "big.txt"
        doc_path.write_text("x" * 200)

        def fake_add_user(text, sender="", source=""):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = AsyncMock(side_effect=fake_add_user)

        response = _make_response(text="ok")

        from attachments import Attachment
        att = Attachment(content_type="text/plain", local_path=str(doc_path),
                         filename="big.txt", size=200)

        with patch("pipeline.run_agentic_loop", return_value=response):
            await daemon._process_message(
                text="", sender="user", talker="user", channel="telegram",
                attachments=[att],
            )

        call_text = session.add_user_message.call_args[0][0]
        assert "[attachment: big.txt, text/plain, saved:" in call_text

    @pytest.mark.asyncio
    async def test_extraction_error_falls_to_label(self, tmp_path):
        """Extraction exception → graceful fallback to label."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.documents_enabled = True
        daemon.config.documents_max_chars = 30000
        daemon.config.documents_max_file_bytes = 10_000_000
        daemon.config.documents_text_extensions = [".txt"]

        doc_path = tmp_path / "bad.txt"
        doc_path.write_text("content")

        def fake_add_user(text, sender="", source=""):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = AsyncMock(side_effect=fake_add_user)

        response = _make_response(text="ok")

        from attachments import Attachment
        att = Attachment(content_type="text/plain", local_path=str(doc_path),
                         filename="bad.txt", size=7)

        with patch("pipeline.run_agentic_loop", return_value=response):
            with patch("pipeline.extract_document_text", side_effect=OSError("disk error")):
                await daemon._process_message(
                    text="", sender="user", talker="user", channel="telegram",
                    attachments=[att],
                )

        call_text = session.add_user_message.call_args[0][0]
        assert "[attachment: bad.txt, text/plain, saved:" in call_text


# ─── PDF Label Emission ──────────────────────────────────────────


class TestPdfLabelEmission:
    """PDFs get a label only — agent uses pdf_read tool for extraction."""

    @pytest.mark.asyncio
    async def test_pdf_gets_label_with_path(self, tmp_path):
        """PDF attachment → [pdf: filename, saved: /path] label."""
        daemon, provider, session = _make_daemon(tmp_path)

        pdf_path = tmp_path / "report.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake")

        def fake_add_user(text, sender="", source=""):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = AsyncMock(side_effect=fake_add_user)

        response = _make_response(text="ok")

        from attachments import Attachment
        att = Attachment(content_type="application/pdf", local_path=str(pdf_path),
                         filename="report.pdf", size=100)

        with patch("pipeline.run_agentic_loop", return_value=response):
            await daemon._process_message(
                text="check this", sender="user", talker="user", channel="telegram",
                attachments=[att],
            )

        call_text = session.add_user_message.call_args[0][0]
        assert "[pdf: report.pdf, saved:" in call_text
        assert str(pdf_path) in call_text

    @pytest.mark.asyncio
    async def test_pdf_label_regardless_of_documents_enabled(self, tmp_path):
        """PDF label emitted even when documents_enabled=False."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.documents_enabled = False

        pdf_path = tmp_path / "report.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake")

        def fake_add_user(text, sender="", source=""):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = AsyncMock(side_effect=fake_add_user)

        response = _make_response(text="ok")

        from attachments import Attachment
        att = Attachment(content_type="application/pdf", local_path=str(pdf_path),
                         filename="report.pdf", size=100)

        with patch("pipeline.run_agentic_loop", return_value=response):
            await daemon._process_message(
                text="", sender="user", talker="user", channel="telegram",
                attachments=[att],
            )

        call_text = session.add_user_message.call_args[0][0]
        assert "[pdf: report.pdf, saved:" in call_text

    @pytest.mark.asyncio
    async def test_pdf_detected_by_extension(self, tmp_path):
        """PDF detected by .pdf extension even with generic MIME type."""
        daemon, provider, session = _make_daemon(tmp_path)

        pdf_path = tmp_path / "report.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake")

        def fake_add_user(text, sender="", source=""):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = AsyncMock(side_effect=fake_add_user)

        response = _make_response(text="ok")

        from attachments import Attachment
        att = Attachment(content_type="application/octet-stream", local_path=str(pdf_path),
                         filename="report.pdf", size=100)

        with patch("pipeline.run_agentic_loop", return_value=response):
            await daemon._process_message(
                text="", sender="user", talker="user", channel="telegram",
                attachments=[att],
            )

        call_text = session.add_user_message.call_args[0][0]
        assert "[pdf: report.pdf, saved:" in call_text

    @pytest.mark.asyncio
    async def test_pdf_no_text_extraction_attempted(self, tmp_path):
        """PDF attachment does not call extract_document_text."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.documents_enabled = True
        daemon.config.documents_max_chars = 30000
        daemon.config.documents_max_file_bytes = 10_000_000

        pdf_path = tmp_path / "doc.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake")

        def fake_add_user(text, sender="", source=""):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = AsyncMock(side_effect=fake_add_user)

        response = _make_response(text="ok")

        from attachments import Attachment
        att = Attachment(content_type="application/pdf", local_path=str(pdf_path),
                         filename="doc.pdf", size=100)

        with patch("pipeline.run_agentic_loop", return_value=response):
            with patch("pipeline.extract_document_text") as mock_extract:
                await daemon._process_message(
                    text="", sender="user", talker="user", channel="telegram",
                    attachments=[att],
                )

        mock_extract.assert_not_called()
        call_text = session.add_user_message.call_args[0][0]
        assert "[pdf: doc.pdf, saved:" in call_text


# ─── Image Dimension Check ───────────────────────────────────────


@pytest.mark.skipif(
    not __import__("importlib").util.find_spec("PIL"),
    reason="Pillow not installed",
)
class TestImageFitting:
    """Verify _fit_image scales dimensions and reduces quality."""

    def test_dimensions_scaled_down(self):
        """Image exceeding max_dimension is scaled to fit."""
        from io import BytesIO

        from PIL import Image

        from attachments import fit_image as _fit_image

        img = Image.new("RGB", (10000, 5000), color="red")
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=95)
        data = buf.getvalue()

        result = _fit_image(data, "image/jpeg", 5 * 1024 * 1024, 1568)
        with Image.open(BytesIO(result)) as fitted:
            assert max(fitted.size) <= 1568

    def test_small_image_unchanged(self):
        """Image within all limits is returned as-is."""
        from io import BytesIO

        from PIL import Image

        from attachments import fit_image as _fit_image

        img = Image.new("RGB", (800, 600), color="blue")
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=90)
        data = buf.getvalue()

        result = _fit_image(data, "image/jpeg", 5 * 1024 * 1024, 1568)
        assert result == data

    def test_jpeg_quality_reduction(self):
        """Large JPEG gets quality reduced to fit under byte limit."""
        # Create a noisy image that compresses poorly
        import random
        from io import BytesIO

        from PIL import Image

        from attachments import fit_image as _fit_image
        img = Image.new("RGB", (1500, 1200))
        pixels = img.load()
        rng = random.Random(42)
        for y in range(1200):
            for x in range(1500):
                pixels[x, y] = (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255))
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=98)
        data = buf.getvalue()

        # Set a tight limit so quality reduction kicks in
        limit = len(data) // 2
        result = _fit_image(data, "image/jpeg", limit, 1568)
        assert len(result) <= limit

    def test_png_too_large_raises(self):
        """PNG that can't be compressed raises _ImageTooLarge."""
        from io import BytesIO

        from PIL import Image

        from attachments import fit_image as _fit_image, ImageTooLarge as _ImageTooLarge

        img = Image.new("RGB", (1000, 800), color="red")
        buf = BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()

        # Set absurdly low limit — PNG is lossless, can't reduce quality
        with pytest.raises(_ImageTooLarge):
            _fit_image(data, "image/png", 100, 1568)

    def test_phone_photo_scaled_to_max_dimension(self):
        """4000x3000 phone photo gets scaled to 1568px longest side."""
        from io import BytesIO

        from PIL import Image

        from attachments import fit_image as _fit_image

        img = Image.new("RGB", (4000, 3000), color="green")
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=90)
        data = buf.getvalue()

        result = _fit_image(data, "image/jpeg", 5 * 1024 * 1024, 1568)
        with Image.open(BytesIO(result)) as fitted:
            assert max(fitted.size) <= 1568
            # Aspect ratio preserved
            assert abs(fitted.size[0] / fitted.size[1] - 4 / 3) < 0.01

    def test_custom_max_dimension(self):
        """Custom max_dimension=768 scales a 1024x768 image."""
        from io import BytesIO

        from PIL import Image

        from attachments import fit_image as _fit_image

        img = Image.new("RGB", (1024, 768), color="yellow")
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=90)
        data = buf.getvalue()

        result = _fit_image(data, "image/jpeg", 5 * 1024 * 1024, 768)
        with Image.open(BytesIO(result)) as fitted:
            assert max(fitted.size) <= 768

    @pytest.mark.asyncio
    async def test_oversized_image_sent_after_fitting(self, tmp_path):
        """Integration: oversized image is fitted and sent, not rejected."""
        from PIL import Image

        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.vision_max_image_bytes = 5 * 1024 * 1024
        daemon.config.vision_max_dimension = 1568

        img = Image.new("RGB", (10000, 8000), color="green")
        img_path = tmp_path / "huge.jpg"
        img.save(str(img_path), format="JPEG", quality=95)

        def fake_add_user(text, sender="", source=""):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = AsyncMock(side_effect=fake_add_user)

        response = _make_response(text="I see green")

        from attachments import Attachment
        att = Attachment(content_type="image/jpeg", local_path=str(img_path),
                         filename="huge.jpg", size=img_path.stat().st_size)

        with patch("pipeline.run_agentic_loop", return_value=response):
            await daemon._process_message(
                text="look", sender="user", talker="user", channel="telegram",
                attachments=[att],
            )

        call_text = session.add_user_message.call_args[0][0]
        assert "[image, saved:" in call_text
        assert "too large" not in call_text


# ─── Message-Level Retry ─────────────────────────────────────────


# ─── Auto-close system sessions ─────────────────────────────────


class TestAutoCloseSystemSessions:
    """System-sourced sessions are one-shot — auto-closed after processing."""

    @pytest.mark.asyncio
    async def test_system_source_triggers_close(self, tmp_path):
        """talker='system' → close_session called after processing."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.session_mgr.close_session = AsyncMock(return_value=True)
        response = _make_response(text="done")

        async def fake_loop(**kwargs):
            return response

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="evolve", sender="evolution", talker="system",
            )

        daemon.session_mgr.close_session.assert_called_once_with("system:evolution")

    @pytest.mark.asyncio
    async def test_telegram_source_not_closed(self, tmp_path):
        """source='telegram' → close_session NOT called."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.session_mgr.close_session = AsyncMock(return_value=True)
        response = _make_response(text="hello")

        async def fake_loop(**kwargs):
            return response

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="hi", sender="Nicolas", talker="user", channel="telegram",
            )

        daemon.session_mgr.close_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_http_source_not_closed(self, tmp_path):
        """source='http' → close_session NOT called (HTTP has follow-ups)."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.session_mgr.close_session = AsyncMock(return_value=True)
        response = _make_response(text="ok")

        async def fake_loop(**kwargs):
            return response

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="check", sender="http-n8n", talker="operator",
            )

        daemon.session_mgr.close_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_cli_source_not_closed(self, tmp_path):
        """source='cli' → close_session NOT called."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.session_mgr.close_session = AsyncMock(return_value=True)
        response = _make_response(text="ok")

        async def fake_loop(**kwargs):
            return response

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="hello", sender="Claudio", talker="operator",
            )

        daemon.session_mgr.close_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_system_error_still_closes(self, tmp_path):
        """Agentic loop error → system session still auto-closed to prevent accumulation."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.session_mgr.close_session = AsyncMock(return_value=True)

        async def fake_loop(**kwargs):
            raise RuntimeError("API down")

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="evolve", sender="evolution", talker="system",
            )

        # System sessions must auto-close even on error — otherwise they
        # accumulate messages and blow past context limits on next trigger.
        daemon.session_mgr.close_session.assert_awaited_once_with("system:evolution")


# ─── Primary Sender Routing ─────────────────────────────────────


class TestPrimarySenderRouting:
    """Notifications route to primary session when notify_target is configured."""

    @pytest.mark.asyncio
    async def test_fresh_system_session_still_autoclosed(self, tmp_path):
        """System events creating fresh sessions still auto-close."""
        daemon, provider, session = _make_daemon(tmp_path)
        # No pre-existing sessions
        daemon.session_mgr._sessions = {}
        daemon.session_mgr._index = {}
        response = _make_response(text="done")

        async def fake_loop(**kwargs):
            return response

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="evolve", sender="evolution", talker="system",
            )

        daemon.session_mgr.close_session.assert_called_once_with("system:evolution")


    @pytest.mark.asyncio
    async def test_error_path_fresh_session_still_autoclosed(self, tmp_path):
        """Error path: fresh system session still auto-closed."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.session_mgr._sessions = {}
        daemon.session_mgr._index = {}

        async def fake_loop(**kwargs):
            raise RuntimeError("API down")

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="evolve", sender="evolution", talker="system",
            )

        daemon.session_mgr.close_session.assert_awaited_once_with("system:evolution")

    @pytest.mark.asyncio
    async def test_telegram_source_unaffected(self, tmp_path):
        """Telegram messages never auto-close regardless of pre-existence."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.session_mgr._sessions = {"Nicolas": session}
        daemon.session_mgr._index = {"Nicolas": {"session_id": session.id}}
        response = _make_response(text="hello")

        async def fake_loop(**kwargs):
            return response

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="hi", sender="Nicolas", talker="user", channel="telegram",
            )

        daemon.session_mgr.close_session.assert_not_called()


# ─── Forced Compact ─────────────────────────────────────────────

class TestForcedCompact:
    """Tests for _handle_compact and force_compact flag."""

    @pytest.mark.asyncio
    async def test_handle_compact_no_sessions(self, tmp_path):
        """Returns skipped when no active sessions."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.session_mgr._index = {}
        # list_contacts already returns [] from _make_daemon defaults

        result = await daemon._handle_compact()
        assert result["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_handle_compact_skips_when_no_user_session(self, tmp_path):
        """Only user:* sessions are targeted — non-user sessions don't trigger compaction."""
        daemon, provider, session = _make_daemon(tmp_path)
        evo_session = MagicMock()
        evo_session.messages = [{"role": "user"}] * 100
        daemon.session_mgr._index = {"system:evolution": evo_session, "system:maintenance": evo_session}
        daemon.session_mgr.list_contacts = AsyncMock(return_value=["system:evolution", "system:maintenance"])

        result = await daemon._handle_compact()
        assert result["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_handle_compact_targets_single_user_session(self, tmp_path):
        """Compact targets the single user session, ignoring any other sessions."""
        daemon, provider, session = _make_daemon(tmp_path)

        user_session = MagicMock()
        user_session.messages = [{"role": "user"}] * 50
        user_session.id = "user-sess"

        user_key = f"user:{daemon.config.user_name}"
        daemon.session_mgr._index = {user_key: {"session_id": "user-sess"}}
        daemon.session_mgr.list_contacts = AsyncMock(return_value=[user_key])
        daemon.session_mgr.get_or_create = AsyncMock(return_value=user_session)

        daemon._process_message = AsyncMock()

        result = await daemon._handle_compact()
        assert result["status"] == "completed"
        assert result["session"] == "user-sess"

        daemon._process_message.assert_awaited_once()
        call_kwargs = daemon._process_message.call_args.kwargs
        assert call_kwargs["force_compact"] is True
        assert call_kwargs["sender"] == daemon.config.user_name
        assert call_kwargs["talker"] == "user"

    @pytest.mark.asyncio
    async def test_force_compact_triggers_compaction_under_threshold(self, tmp_path):
        """force_compact=True triggers compaction even under token threshold."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.consolidation_enabled = False
        # Session is under compaction threshold
        session.needs_compaction = MagicMock(return_value=False)
        session.last_input_tokens = 10000

        def fake_add_user(text, sender="", source=""):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = AsyncMock(side_effect=fake_add_user)

        response = _make_response(text="diary written")

        async def fake_loop(**kwargs):
            session.messages.append(response.to_internal_message())
            return response

        compacted = []

        captured_prompts = []

        async def fake_compact(sess, prov, prompt, **kwargs):
            compacted.append(True)
            captured_prompts.append(prompt)

        daemon.session_mgr.compact_session = AsyncMock(side_effect=fake_compact)
        daemon.config.compaction_prompt = "Compact for {agent_name}, limit {max_tokens}."
        daemon.config.compaction_max_tokens = 2048

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="Write diary", sender="Nicolas", talker="system",
                force_compact=True,
            )

        assert len(compacted) == 1, "Compaction should fire despite under threshold"
        assert "2048" in captured_prompts[0], "max_tokens placeholder should be resolved"
        assert "{max_tokens}" not in captured_prompts[0], "placeholder should not remain raw"

    @pytest.mark.asyncio
    async def test_force_compact_does_not_auto_close(self, tmp_path):
        """force_compact=True with source=system should NOT auto-close session."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.consolidation_enabled = False
        session.needs_compaction = MagicMock(return_value=False)

        def fake_add_user(text, sender="", source=""):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = AsyncMock(side_effect=fake_add_user)

        response = _make_response(text="done")

        async def fake_loop(**kwargs):
            session.messages.append(response.to_internal_message())
            return response

        daemon.session_mgr.compact_session = AsyncMock()

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop):
            await daemon._process_message(
                text="compact diary", sender="Nicolas", talker="system",
                force_compact=True,
            )

        # Should NOT auto-close — this is the primary session
        daemon.session_mgr.close_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_force_compact_with_consolidation(self, tmp_path):
        """force_compact=True runs pre-compaction consolidation."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.consolidation_enabled = True
        daemon.pool = MagicMock()
        session.needs_compaction = MagicMock(return_value=False)
        session.compaction_count = 0

        def fake_add_user(text, sender="", source=""):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = AsyncMock(side_effect=fake_add_user)

        response = _make_response(text="done")

        async def fake_loop(**kwargs):
            session.messages.append(response.to_internal_message())
            return response

        daemon.session_mgr.compact_session = AsyncMock()

        consolidation_called = []

        async def fake_consolidate(**kwargs):
            consolidation_called.append(True)
            return {"facts_added": 0, "episode_id": None}

        with patch("pipeline.run_agentic_loop", side_effect=fake_loop), \
             patch("consolidation.consolidate_session", side_effect=fake_consolidate):
            await daemon._process_message(
                text="compact diary", sender="Nicolas", talker="system",
                force_compact=True,
            )

        assert len(consolidation_called) == 1


# ─── Log Injection Prevention ────────────────────────────────────


class TestLogSafeSanitizer:
    """Verify lucyd module re-exports _log_safe from log_utils."""

    def test_reexports_log_safe(self):
        from lucyd import _log_safe
        from log_utils import _log_safe as canonical
        assert _log_safe is canonical
