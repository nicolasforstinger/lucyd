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

from lucyd import (
    LucydDaemon,
    _inject_warning,
    _should_deliver,
    _should_warn_context,
)

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

    provider = MagicMock()
    provider.format_system = MagicMock(return_value=[])
    provider.format_messages = MagicMock(return_value=[])
    provider.format_tools = MagicMock(return_value=[])
    daemon.providers = {"primary": provider}

    session = MagicMock()
    session.id = "test-session-001"
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
    daemon.session_mgr.build_recall = MagicMock(return_value="")

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
    daemon.config.state_dir = state_dir
    daemon.config.route_model = MagicMock(return_value="primary")
    daemon.config.model_config = MagicMock(return_value={
        "model": "test-model", "cost_per_mtok": [1.0, 5.0, 0.1],
    })
    daemon.config.typing_indicators = False
    daemon.config.max_turns = 10
    daemon.config.agent_timeout = 30
    daemon.config.cost_db = Path(str(tmp_path / "cost.db"))
    daemon.config.silent_tokens = []
    daemon.config.compaction_threshold = 150000
    daemon.config.always_on_skills = []
    daemon.config.error_message = "Something went wrong."
    daemon.config.raw = MagicMock(return_value=0.0)
    daemon.config.compaction_model = "compaction"
    daemon.config.compaction_prompt = "Compact this."
    daemon.config.agent_name = "TestAgent"
    daemon.config.consolidation_enabled = False

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
        ) is True

    def test_no_warn_below_threshold(self):
        """119999 tokens < 80% of 150000 → no warn."""
        assert _should_warn_context(
            input_tokens=119999,
            compaction_threshold=150000,
            needs_compaction=False,
            already_warned=False,
        ) is False

    def test_no_warn_at_exact_threshold(self):
        """Exactly at threshold (120000) → no warn (> not >=)."""
        assert _should_warn_context(
            input_tokens=120000,
            compaction_threshold=150000,
            needs_compaction=False,
            already_warned=False,
        ) is False

    def test_no_warn_if_needs_compaction(self):
        """If already at hard compaction, skip warning."""
        assert _should_warn_context(
            input_tokens=160000,
            compaction_threshold=150000,
            needs_compaction=True,
            already_warned=False,
        ) is False

    def test_no_warn_if_already_warned(self):
        """If already warned this session, don't repeat."""
        assert _should_warn_context(
            input_tokens=130000,
            compaction_threshold=150000,
            needs_compaction=False,
            already_warned=True,
        ) is False

    def test_no_warn_zero_tokens(self):
        """Zero tokens → no warn."""
        assert _should_warn_context(
            input_tokens=0,
            compaction_threshold=150000,
            needs_compaction=False,
            already_warned=False,
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


class TestShouldDeliver:
    """Unit tests for _should_deliver."""

    NO_DELIVERY = frozenset({"system", "http"})

    def test_deliver_normal_reply(self):
        assert _should_deliver("Hello!", "telegram", self.NO_DELIVERY) is True

    def test_no_deliver_empty_reply(self):
        assert _should_deliver("", "telegram", self.NO_DELIVERY) is False

    def test_no_deliver_whitespace_reply(self):
        assert _should_deliver("   \n  ", "telegram", self.NO_DELIVERY) is False

    def test_no_deliver_system_source(self):
        assert _should_deliver("Hello!", "system", self.NO_DELIVERY) is False

    def test_no_deliver_http_source(self):
        assert _should_deliver("Hello!", "http", self.NO_DELIVERY) is False

    def test_deliver_cli_source(self):
        """CLI is not in suppressed sources."""
        assert _should_deliver("Hello!", "cli", self.NO_DELIVERY) is True

    def test_no_deliver_empty_and_suppressed(self):
        """Both conditions false → no delivery."""
        assert _should_deliver("", "system", self.NO_DELIVERY) is False


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
        """Provider response text is sent to channel.send."""
        daemon, provider, session = _make_daemon(tmp_path)
        response = _make_response(text="Hello, Nicolas!")

        async def fake_loop(**kwargs):
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hi", sender="Nicolas", source="telegram",
                )

        daemon.channel.send.assert_called_once()
        args = daemon.channel.send.call_args[0]
        assert args[0] == "Nicolas"
        assert args[1] == "Hello, Nicolas!"

    @pytest.mark.asyncio
    async def test_session_get_or_create_called(self, tmp_path):
        """get_or_create called with sender and model."""
        daemon, provider, session = _make_daemon(tmp_path)
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="alice", source="telegram",
                )

        daemon.session_mgr.get_or_create.assert_called_once_with(
            "alice", model="primary"
        )

    @pytest.mark.asyncio
    async def test_user_message_added_to_session(self, tmp_path):
        """add_user_message called with text containing timestamp."""
        daemon, provider, session = _make_daemon(tmp_path)
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="user", source="telegram",
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
        """Agentic loop raises → error message sent to channel."""
        daemon, provider, session = _make_daemon(tmp_path)

        async def fake_loop(**kwargs):
            raise RuntimeError("API connection failed")

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="user", source="telegram",
                )

        daemon.channel.send.assert_called_once()
        args = daemon.channel.send.call_args[0]
        assert args[0] == "user"
        assert args[1] == "Something went wrong."

    @pytest.mark.asyncio
    async def test_error_does_not_crash(self, tmp_path):
        """Agentic loop raises → _process_message completes without raising."""
        daemon, provider, session = _make_daemon(tmp_path)

        async def fake_loop(**kwargs):
            raise ValueError("bad input")

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                # Should not raise
                await daemon._process_message(
                    text="hello", sender="user", source="telegram",
                )

    @pytest.mark.asyncio
    async def test_error_no_delivery_for_system_source(self, tmp_path):
        """System source error → no error message via channel."""
        daemon, provider, session = _make_daemon(tmp_path)

        async def fake_loop(**kwargs):
            raise RuntimeError("API down")

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="heartbeat", sender="system", source="system",
                )

        daemon.channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_model_returns_early(self, tmp_path):
        """No provider for routed model → early return, no agentic loop."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.route_model.return_value = "nonexistent"
        daemon.providers = {}

        with patch("lucyd.run_agentic_loop") as mock_loop:
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="user", source="telegram",
                )

        mock_loop.assert_not_called()


class TestTypingIndicators:
    """Contract: typing indicators sent/suppressed based on source."""

    @pytest.mark.asyncio
    async def test_typing_sent_for_telegram(self, tmp_path):
        """Telegram source + typing enabled → send_typing called."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.typing_indicators = True
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="user", source="telegram",
                )

        daemon.channel.send_typing.assert_called_once_with("user")

    @pytest.mark.asyncio
    async def test_no_typing_for_system(self, tmp_path):
        """System source → typing suppressed even if enabled."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.typing_indicators = True
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="user", source="system",
                )

        daemon.channel.send_typing.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_typing_for_http(self, tmp_path):
        """HTTP source → typing suppressed."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.typing_indicators = True
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="user", source="http",
                )

        daemon.channel.send_typing.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_typing_when_disabled(self, tmp_path):
        """typing_indicators=False → no typing regardless of source."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.typing_indicators = False
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="user", source="telegram",
                )

        daemon.channel.send_typing.assert_not_called()


class TestSilentTokenSuppression:
    """Contract: silent token replies are not delivered to channel."""

    @pytest.mark.asyncio
    async def test_silent_reply_not_delivered(self, tmp_path):
        """Reply matching silent_tokens → channel.send not called."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.silent_tokens = ["HEARTBEAT_OK"]
        response = _make_response(text="HEARTBEAT_OK")

        async def fake_loop(**kwargs):
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="run heartbeat", sender="user", source="telegram",
                )

        daemon.channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_silent_reply_delivered(self, tmp_path):
        """Reply not matching silent_tokens → channel.send called."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.silent_tokens = ["HEARTBEAT_OK"]
        response = _make_response(text="Here's your answer!")

        async def fake_loop(**kwargs):
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="user", source="telegram",
                )

        daemon.channel.send.assert_called_once()


class TestDeliverySuppression:
    """Contract: non-channel sources don't get channel.send."""

    @pytest.mark.asyncio
    async def test_no_delivery_for_system_source(self, tmp_path):
        """System source → no channel.send even with non-empty reply."""
        daemon, provider, session = _make_daemon(tmp_path)
        response = _make_response(text="Done processing")

        async def fake_loop(**kwargs):
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="heartbeat", sender="system", source="system",
                )

        daemon.channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_delivery_for_http_source(self, tmp_path):
        """HTTP source → no channel.send."""
        daemon, provider, session = _make_daemon(tmp_path)
        response = _make_response(text="HTTP response")

        async def fake_loop(**kwargs):
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="query", sender="test", source="http",
                )

        daemon.channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_delivery_for_empty_reply(self, tmp_path):
        """Empty reply → no channel.send even for telegram."""
        daemon, provider, session = _make_daemon(tmp_path)
        response = _make_response(text="")

        async def fake_loop(**kwargs):
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="user", source="telegram",
                )

        daemon.channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_delivery_for_cli_source(self, tmp_path):
        """CLI source → channel.send called (not suppressed)."""
        daemon, provider, session = _make_daemon(tmp_path)
        response = _make_response(text="reply")

        async def fake_loop(**kwargs):
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="user", source="cli",
                )

        daemon.channel.send.assert_called_once()


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

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="user", source="telegram",
                )

        # Warning consumed
        assert session.pending_system_warning == ""
        # Check the text passed to add_user_message includes the warning
        call_text = session.add_user_message.call_args[0][0]
        assert "[system: Context is getting long]" in call_text
        assert "hello" in call_text

    @pytest.mark.asyncio
    async def test_no_warning_when_empty(self, tmp_path):
        """No pending warning → text not modified with [system:]."""
        daemon, provider, session = _make_daemon(tmp_path)
        session.pending_system_warning = ""
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="user", source="telegram",
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

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                with patch("tools.status.MAX_CONTEXT_TOKENS", 200000):
                    await daemon._process_message(
                        text="hello", sender="user", source="telegram",
                    )

        assert session.pending_system_warning != ""
        assert "130,000" in session.pending_system_warning
        assert session.warned_about_compaction is True
        session._save_state.assert_called()

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

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="user", source="telegram",
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

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="user", source="telegram",
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

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                with patch("tools.status.MAX_CONTEXT_TOKENS", 0):
                    await daemon._process_message(
                        text="hello", sender="user", source="telegram",
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
        compaction_provider = MagicMock()
        daemon.providers["compaction"] = compaction_provider
        daemon.session_mgr.compact_session = AsyncMock()
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="user", source="telegram",
                )

        daemon.session_mgr.compact_session.assert_called_once()
        args = daemon.session_mgr.compact_session.call_args[0]
        assert args[0] is session
        assert args[1] is compaction_provider
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

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="user", source="telegram",
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

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="question", sender="api", source="http",
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

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="question", sender="api", source="http",
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

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="heartbeat", sender="system", source="http",
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

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="user", source="telegram",
                    response_future=None,
                )


class TestMessagePersistence:
    """Contract: new messages from agentic loop are persisted."""

    @pytest.mark.asyncio
    async def test_assistant_messages_persisted(self, tmp_path):
        """Assistant messages appended by loop → persist_assistant_message called."""
        daemon, provider, session = _make_daemon(tmp_path)
        response = _make_response(text="reply")

        async def fake_loop(**kwargs):
            # Simulate agentic loop appending messages
            kwargs["messages"].append({"role": "assistant", "content": "reply"})
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="user", source="telegram",
                )

        session.persist_assistant_message.assert_called_once_with(
            {"role": "assistant", "content": "reply"}
        )

    @pytest.mark.asyncio
    async def test_tool_results_persisted(self, tmp_path):
        """Tool result messages → persist_tool_results called."""
        daemon, provider, session = _make_daemon(tmp_path)
        response = _make_response(text="reply")

        async def fake_loop(**kwargs):
            kwargs["messages"].append({
                "role": "tool_results",
                "results": [{"tool_use_id": "tc-1", "content": "done"}],
            })
            kwargs["messages"].append({"role": "assistant", "content": "reply"})
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="user", source="telegram",
                )

        session.persist_tool_results.assert_called_once_with(
            [{"tool_use_id": "tc-1", "content": "done"}]
        )

    @pytest.mark.asyncio
    async def test_state_saved_after_processing(self, tmp_path):
        """_save_state called after message processing."""
        daemon, provider, session = _make_daemon(tmp_path)
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="user", source="telegram",
                )

        session._save_state.assert_called()


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

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                with patch("memory.get_session_start_context", return_value=mock_context) as mock_gsc:
                    with patch.object(daemon, "_get_memory_conn", return_value=MagicMock()):
                        await daemon._process_message(
                            text="hello", sender="user", source="telegram",
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

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                with patch("memory.get_session_start_context") as mock_gsc:
                    await daemon._process_message(
                        text="hello", sender="user", source="telegram",
                    )

        mock_gsc.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_structured_recall_on_subsequent_messages(self, tmp_path):
        """Structured recall only on first message (len(messages) <= 1)."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.consolidation_enabled = True
        # Simulate existing messages (not first message)
        session.messages = [{"role": "user", "content": "prior"}, {"role": "assistant", "content": "reply"}]
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                with patch("memory.get_session_start_context") as mock_gsc:
                    await daemon._process_message(
                        text="hello", sender="user", source="telegram",
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

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                with patch("memory.get_session_start_context", side_effect=Exception("DB corrupt")):
                    with patch.object(daemon, "_get_memory_conn", return_value=MagicMock()):
                        await daemon._process_message(
                            text="hello", sender="user", source="telegram",
                        )

        # Should complete without crashing and deliver reply
        daemon.channel.send.assert_called_once()
        assert daemon.channel.send.call_args[0][1] == "reply despite recall failure"

    @pytest.mark.asyncio
    async def test_pre_compaction_consolidation_called(self, tmp_path):
        """When needs_compaction and consolidation_enabled, consolidation runs before compact."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.consolidation_enabled = True
        session.needs_compaction = MagicMock(return_value=True)
        session.last_input_tokens = 160000
        session.warned_about_compaction = True
        session.compaction_count = 0
        compaction_provider = MagicMock()
        daemon.providers["compaction"] = compaction_provider
        daemon.providers["subagent"] = MagicMock()
        daemon.session_mgr.compact_session = AsyncMock()
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        mock_result = {"facts_added": 3, "episode_id": "ep-1"}

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                with patch("consolidation.consolidate_session", new_callable=AsyncMock, return_value=mock_result) as mock_consol:
                    with patch.object(daemon, "_get_memory_conn", return_value=MagicMock()):
                        await daemon._process_message(
                            text="hello", sender="user", source="telegram",
                        )

        mock_consol.assert_called_once()
        # Compaction should also proceed
        daemon.session_mgr.compact_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_pre_compaction_consolidation_failure_does_not_block_compaction(self, tmp_path):
        """If pre-compaction consolidation fails, compaction still proceeds."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.consolidation_enabled = True
        session.needs_compaction = MagicMock(return_value=True)
        session.last_input_tokens = 160000
        session.warned_about_compaction = True
        session.compaction_count = 0
        compaction_provider = MagicMock()
        daemon.providers["compaction"] = compaction_provider
        daemon.session_mgr.compact_session = AsyncMock()
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                with patch("consolidation.consolidate_session", new_callable=AsyncMock, side_effect=Exception("LLM timeout")):
                    with patch.object(daemon, "_get_memory_conn", return_value=MagicMock()):
                        await daemon._process_message(
                            text="hello", sender="user", source="telegram",
                        )

        # Compaction MUST still proceed despite consolidation failure
        daemon.session_mgr.compact_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_pre_compaction_consolidation_when_disabled(self, tmp_path):
        """When consolidation_enabled=False, no pre-compaction consolidation."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.consolidation_enabled = False
        session.needs_compaction = MagicMock(return_value=True)
        session.last_input_tokens = 160000
        session.warned_about_compaction = True
        compaction_provider = MagicMock()
        daemon.providers["compaction"] = compaction_provider
        daemon.session_mgr.compact_session = AsyncMock()
        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                with patch("consolidation.consolidate_session", new_callable=AsyncMock) as mock_consol:
                    await daemon._process_message(
                        text="hello", sender="user", source="telegram",
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
        session.compaction_count = 0

        mock_result = {"facts_added": 1, "episode_id": "ep-close"}

        with patch("consolidation.get_unprocessed_range", return_value=(0, 5)):
            with patch("consolidation.consolidate_session", new_callable=AsyncMock, return_value=mock_result) as mock_consol:
                with patch.object(daemon, "_get_memory_conn", return_value=MagicMock()):
                    await daemon._consolidate_on_close(session)

        mock_consol.assert_called_once()

    @pytest.mark.asyncio
    async def test_consolidate_on_close_skips_when_no_unprocessed(self, tmp_path):
        """No unprocessed messages → consolidation not called."""
        daemon, provider, session = _make_daemon(tmp_path)
        session.compaction_count = 0

        with patch("consolidation.get_unprocessed_range", return_value=(5, 5)):
            with patch("consolidation.consolidate_session", new_callable=AsyncMock) as mock_consol:
                with patch.object(daemon, "_get_memory_conn", return_value=MagicMock()):
                    await daemon._consolidate_on_close(session)

        mock_consol.assert_not_called()

    @pytest.mark.asyncio
    async def test_consolidate_on_close_failure_does_not_crash(self, tmp_path):
        """Consolidation failure on close is caught — no exception propagated."""
        daemon, provider, session = _make_daemon(tmp_path)
        session.compaction_count = 0

        with patch("consolidation.get_unprocessed_range", side_effect=Exception("DB locked")):
            with patch.object(daemon, "_get_memory_conn", return_value=MagicMock()):
                # Should not raise
                await daemon._consolidate_on_close(session)
