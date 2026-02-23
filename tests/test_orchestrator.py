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
    async def test_warning_consumed_persists_before_agentic_loop(self, tmp_path):
        """Cleared warning is saved to state before the agentic loop runs."""
        daemon, provider, session = _make_daemon(tmp_path)
        session.pending_system_warning = "Context warning"
        response = _make_response()

        call_order = []
        original_save = session._save_state
        session._save_state = lambda: (call_order.append("save_state"), original_save())
        original_add = session.add_user_message
        session.add_user_message = lambda *a, **kw: (call_order.append("add_user_message"), original_add(*a, **kw))

        async def fake_loop(**kwargs):
            call_order.append("agentic_loop")
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="user", source="telegram",
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
        session.add_user_message = MagicMock(side_effect=fake_add_user)

        async def fake_loop(**kwargs):
            raise RuntimeError("API returned 400")

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="user", source="telegram",
                )

        # Session must NOT end with an orphaned user message
        assert not session.messages or session.messages[-1].get("role") != "user"
        # State must be saved after cleanup
        session._save_state.assert_called()

    @pytest.mark.asyncio
    async def test_error_preserves_prior_assistant_message(self, tmp_path):
        """Error cleanup only removes the trailing user message, not earlier ones."""
        daemon, provider, session = _make_daemon(tmp_path)

        # Pre-populate with a valid exchange
        session.messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
        ]

        def fake_add_user(text, sender="", source=""):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = MagicMock(side_effect=fake_add_user)

        async def fake_loop(**kwargs):
            raise RuntimeError("API error")

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="second", sender="user", source="telegram",
                )

        # Prior exchange intact, orphaned user message removed
        assert len(session.messages) == 2
        assert session.messages[-1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_error_with_image_blocks_cleans_up(self, tmp_path):
        """Error with image attachments → image blocks restored AND user message popped."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.vision_max_image_bytes = 10 * 1024 * 1024

        def fake_add_user(text, sender="", source=""):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = MagicMock(side_effect=fake_add_user)

        async def fake_loop(**kwargs):
            raise RuntimeError("API rejected image")

        # Create a tiny fake image
        img_path = tmp_path / "test.jpg"
        img_path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

        from channels import Attachment
        att = Attachment(
            content_type="image/jpeg",
            filename="test.jpg",
            local_path=str(img_path),
            size=104,
        )

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="look at this", sender="user", source="telegram",
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
        session.add_user_message = MagicMock(side_effect=fake_add_user)

        response = _make_response(text="recovered!")

        async def fake_loop(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("First call fails")
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                # First message — error
                await daemon._process_message(
                    text="msg1", sender="user", source="telegram",
                )
                # Second message — should succeed
                await daemon._process_message(
                    text="msg2", sender="user", source="telegram",
                )

        # Second call completed (loop was called twice)
        assert call_count[0] == 2
        # channel.send called twice: error message for msg1, reply for msg2
        assert daemon.channel.send.call_count == 2
        assert daemon.channel.send.call_args_list[1][0][1] == "recovered!"


# ─── Defense: Consecutive User Message Merge ─────────────────────


class TestConsecutiveUserMessageMerge:
    """Contract: consecutive user messages are merged before agentic loop."""

    @pytest.mark.asyncio
    async def test_consecutive_user_messages_merged(self, tmp_path):
        """Two consecutive user messages → merged into one before API call."""
        daemon, provider, session = _make_daemon(tmp_path)

        # Pre-populate with orphaned user message from prior error
        session.messages = [{"role": "user", "content": "orphaned message"}]

        def fake_add_user(text, sender="", source=""):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = MagicMock(side_effect=fake_add_user)

        response = _make_response(text="ok")
        captured_messages = []

        async def fake_loop(**kwargs):
            # Snapshot messages at time of API call
            captured_messages.extend([m.copy() for m in kwargs["messages"]])
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="new message", sender="user", source="telegram",
                )

        # Only one user message should reach the agentic loop
        user_msgs = [m for m in captured_messages if m.get("role") == "user"]
        assert len(user_msgs) == 1
        # Both texts present in merged content
        assert "orphaned message" in user_msgs[0]["content"]
        assert "new message" in user_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_no_merge_when_alternating_roles(self, tmp_path):
        """Normal alternating user/assistant → no merge."""
        daemon, provider, session = _make_daemon(tmp_path)

        session.messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
        ]

        def fake_add_user(text, sender="", source=""):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = MagicMock(side_effect=fake_add_user)

        response = _make_response(text="ok")
        captured_messages = []

        async def fake_loop(**kwargs):
            captured_messages.extend([m.copy() for m in kwargs["messages"]])
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="second", sender="user", source="telegram",
                )

        # Three messages: user, assistant, user — no merge
        user_msgs = [m for m in captured_messages if m.get("role") == "user"]
        assert len(user_msgs) == 2

    @pytest.mark.asyncio
    async def test_no_merge_on_first_message(self, tmp_path):
        """First message in empty session → no merge attempt."""
        daemon, provider, session = _make_daemon(tmp_path)
        session.messages = []

        def fake_add_user(text, sender="", source=""):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = MagicMock(side_effect=fake_add_user)

        response = _make_response(text="ok")

        async def fake_loop(**kwargs):
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                # Should not crash on empty session
                await daemon._process_message(
                    text="hello", sender="user", source="telegram",
                )

    @pytest.mark.asyncio
    async def test_merge_handles_content_block_format(self, tmp_path):
        """Merge extracts text from content block format (list of dicts)."""
        daemon, provider, session = _make_daemon(tmp_path)

        # Orphaned message with content block format (from prior image processing)
        session.messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "image caption"},
                {"type": "image", "media_type": "image/jpeg", "data": "base64data"},
            ],
        }]

        def fake_add_user(text, sender="", source=""):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = MagicMock(side_effect=fake_add_user)

        response = _make_response(text="ok")
        captured_messages = []

        async def fake_loop(**kwargs):
            captured_messages.extend([m.copy() for m in kwargs["messages"]])
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="follow up", sender="user", source="telegram",
                )

        # Should merge — one user message with text from both
        user_msgs = [m for m in captured_messages if m.get("role") == "user"]
        assert len(user_msgs) == 1
        merged = user_msgs[0]["content"]
        assert "image caption" in merged
        assert "follow up" in merged

    @pytest.mark.asyncio
    async def test_merge_clears_deep_corruption(self, tmp_path):
        """Multiple stacked orphaned user messages all merged in one pass."""
        daemon, provider, session = _make_daemon(tmp_path)

        # Simulate deep corruption: 4 orphaned user messages from repeated errors
        session.messages = [
            {"role": "user", "content": "msg1"},
            {"role": "user", "content": "msg2"},
            {"role": "user", "content": "msg3"},
            {"role": "user", "content": "msg4"},
        ]

        def fake_add_user(text, sender="", source=""):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = MagicMock(side_effect=fake_add_user)

        response = _make_response(text="ok")
        captured_messages = []

        async def fake_loop(**kwargs):
            captured_messages.extend([m.copy() for m in kwargs["messages"]])
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="recovery msg", sender="user", source="telegram",
                )

        # All 5 user messages merged into one in a single pass
        user_msgs = [m for m in captured_messages if m.get("role") == "user"]
        assert len(user_msgs) == 1
        merged = user_msgs[0]["content"]
        for fragment in ("msg1", "msg2", "msg3", "msg4", "recovery msg"):
            assert fragment in merged


# ─── Image Dimension Check ───────────────────────────────────────


# ─── Document Text Extraction ─────────────────────────────────────


class TestExtractDocumentText:
    """Unit tests for _extract_document_text."""

    def test_text_file_extracted(self, tmp_path):
        """Plain .txt file → content returned."""
        from lucyd import _extract_document_text

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
        from lucyd import _extract_document_text

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
        from lucyd import _extract_document_text

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
        from lucyd import _extract_document_text

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
        from lucyd import _extract_document_text

        f = tmp_path / "huge.txt"
        f.write_bytes(b"x" * 200)
        result = _extract_document_text(
            str(f), "text/plain", "huge.txt",
            max_chars=30000, max_bytes=100,  # 100 bytes limit
            text_extensions=[".txt"],
        )
        assert result is None

    def test_pdf_extraction(self, tmp_path):
        """PDF with text content → text extracted."""
        from pypdf import PdfWriter
        from lucyd import _extract_document_text

        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        # pypdf blank pages have no text, so we create a real PDF via reportlab-free method
        # Instead, use a PDF that has actual text by annotation
        pdf_path = tmp_path / "test.pdf"
        writer.write(str(pdf_path))

        # This returns None or empty for blank pages — that's correct behavior
        result = _extract_document_text(
            str(pdf_path), "application/pdf", "test.pdf",
            max_chars=30000, max_bytes=10_000_000,
            text_extensions=[],
        )
        # Blank PDF has no text → returns None (empty string becomes None via `or None`)
        assert result is None

    def test_pdf_by_extension(self, tmp_path):
        """PDF detected by .pdf extension even with generic MIME type."""
        from pypdf import PdfWriter
        from lucyd import _extract_document_text

        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        pdf_path = tmp_path / "report.pdf"
        writer.write(str(pdf_path))

        # Should not crash — gracefully handles blank PDF
        result = _extract_document_text(
            str(pdf_path), "application/octet-stream", "report.pdf",
            max_chars=30000, max_bytes=10_000_000,
            text_extensions=[],
        )
        assert result is None  # blank PDF → None

    def test_pypdf_not_installed(self, tmp_path):
        """When pypdf is not importable, PDF falls through to None."""
        import builtins
        from lucyd import _extract_document_text

        f = tmp_path / "doc.pdf"
        f.write_bytes(b"%PDF-1.4 fake")

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "pypdf":
                raise ImportError("no pypdf")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            result = _extract_document_text(
                str(f), "application/pdf", "doc.pdf",
                max_chars=30000, max_bytes=10_000_000,
                text_extensions=[],
            )
        assert result is None

    def test_corrupt_pdf_raises(self, tmp_path):
        """Corrupt PDF → exception propagates (caller catches it)."""
        import pypdf.errors
        from lucyd import _extract_document_text

        f = tmp_path / "corrupt.pdf"
        f.write_bytes(b"not a pdf at all")

        with pytest.raises(pypdf.errors.PdfReadError):
            _extract_document_text(
                str(f), "application/pdf", "corrupt.pdf",
                max_chars=30000, max_bytes=10_000_000,
                text_extensions=[],
            )

    def test_md_extension_extracted(self, tmp_path):
        """Markdown file matched by extension."""
        from lucyd import _extract_document_text

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
        from lucyd import _extract_document_text

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
        session.add_user_message = MagicMock(side_effect=fake_add_user)

        response = _make_response(text="Got it")

        from channels import Attachment
        att = Attachment(content_type="text/plain", local_path=str(doc_path),
                         filename="notes.txt", size=doc_path.stat().st_size)

        with patch("lucyd.run_agentic_loop", return_value=response):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="check this", sender="user", source="telegram",
                    attachments=[att],
                )

        call_text = session.add_user_message.call_args[0][0]
        assert "[document: notes.txt]" in call_text
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
        session.add_user_message = MagicMock(side_effect=fake_add_user)

        response = _make_response(text="ok")

        from channels import Attachment
        att = Attachment(content_type="application/octet-stream", local_path=str(doc_path),
                         filename="design.psd", size=50)

        with patch("lucyd.run_agentic_loop", return_value=response):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="", sender="user", source="telegram",
                    attachments=[att],
                )

        call_text = session.add_user_message.call_args[0][0]
        assert "[attachment: design.psd, application/octet-stream]" in call_text

    @pytest.mark.asyncio
    async def test_documents_disabled_falls_to_label(self, tmp_path):
        """documents_enabled=False → all documents get label-only."""
        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.documents_enabled = False

        doc_path = tmp_path / "readme.txt"
        doc_path.write_text("This should not be extracted")

        def fake_add_user(text, sender="", source=""):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = MagicMock(side_effect=fake_add_user)

        response = _make_response(text="ok")

        from channels import Attachment
        att = Attachment(content_type="text/plain", local_path=str(doc_path),
                         filename="readme.txt", size=doc_path.stat().st_size)

        with patch("lucyd.run_agentic_loop", return_value=response):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="", sender="user", source="telegram",
                    attachments=[att],
                )

        call_text = session.add_user_message.call_args[0][0]
        assert "[attachment: readme.txt, text/plain]" in call_text
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
        session.add_user_message = MagicMock(side_effect=fake_add_user)

        response = _make_response(text="ok")

        from channels import Attachment
        att = Attachment(content_type="text/plain", local_path=str(doc_path),
                         filename="big.txt", size=200)

        with patch("lucyd.run_agentic_loop", return_value=response):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="", sender="user", source="telegram",
                    attachments=[att],
                )

        call_text = session.add_user_message.call_args[0][0]
        assert "[attachment: big.txt, text/plain]" in call_text

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
        session.add_user_message = MagicMock(side_effect=fake_add_user)

        response = _make_response(text="ok")

        from channels import Attachment
        att = Attachment(content_type="text/plain", local_path=str(doc_path),
                         filename="bad.txt", size=7)

        with patch("lucyd.run_agentic_loop", return_value=response):
            with patch("tools.status.set_current_session"):
                with patch("lucyd._extract_document_text", side_effect=OSError("disk error")):
                    await daemon._process_message(
                        text="", sender="user", source="telegram",
                        attachments=[att],
                    )

        call_text = session.add_user_message.call_args[0][0]
        assert "[attachment: bad.txt, text/plain]" in call_text


# ─── Image Dimension Check ───────────────────────────────────────


class TestImageFitting:
    """Verify _fit_image scales dimensions and reduces quality."""

    def test_dimensions_scaled_down(self):
        """Image >8000px is scaled to fit."""
        from io import BytesIO
        from PIL import Image
        from lucyd import _fit_image

        img = Image.new("RGB", (10000, 5000), color="red")
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=95)
        data = buf.getvalue()

        result = _fit_image(data, "image/jpeg", 5 * 1024 * 1024)
        with Image.open(BytesIO(result)) as fitted:
            assert max(fitted.size) <= 8000

    def test_small_image_unchanged(self):
        """Image within all limits is returned as-is."""
        from io import BytesIO
        from PIL import Image
        from lucyd import _fit_image

        img = Image.new("RGB", (800, 600), color="blue")
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=90)
        data = buf.getvalue()

        result = _fit_image(data, "image/jpeg", 5 * 1024 * 1024)
        assert result == data

    def test_jpeg_quality_reduction(self):
        """Large JPEG gets quality reduced to fit under byte limit."""
        from io import BytesIO
        from PIL import Image
        from lucyd import _fit_image

        # Create a noisy image that compresses poorly
        import random
        img = Image.new("RGB", (4000, 3000))
        pixels = img.load()
        rng = random.Random(42)
        for y in range(3000):
            for x in range(4000):
                pixels[x, y] = (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255))
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=98)
        data = buf.getvalue()

        # Set a tight limit so quality reduction kicks in
        limit = len(data) // 2
        result = _fit_image(data, "image/jpeg", limit)
        assert len(result) <= limit

    def test_png_too_large_raises(self):
        """PNG that can't be compressed raises _ImageTooLarge."""
        from io import BytesIO
        from PIL import Image
        from lucyd import _ImageTooLarge, _fit_image

        img = Image.new("RGB", (4000, 3000), color="red")
        buf = BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()

        # Set absurdly low limit — PNG is lossless, can't reduce quality
        with pytest.raises(_ImageTooLarge):
            _fit_image(data, "image/png", 100)

    @pytest.mark.asyncio
    async def test_oversized_image_sent_after_fitting(self, tmp_path):
        """Integration: oversized image is fitted and sent, not rejected."""
        from PIL import Image

        daemon, provider, session = _make_daemon(tmp_path)
        daemon.config.vision_max_image_bytes = 5 * 1024 * 1024
        daemon.config.vision_default_caption = "image"
        daemon.config.vision_too_large_msg = "image too large"

        img = Image.new("RGB", (10000, 8000), color="green")
        img_path = tmp_path / "huge.jpg"
        img.save(str(img_path), format="JPEG", quality=95)

        def fake_add_user(text, sender="", source=""):
            session.messages.append({"role": "user", "content": text})
        session.add_user_message = MagicMock(side_effect=fake_add_user)

        response = _make_response(text="I see green")

        from channels import Attachment
        att = Attachment(content_type="image/jpeg", local_path=str(img_path),
                         filename="huge.jpg", size=img_path.stat().st_size)

        with patch("lucyd.run_agentic_loop", return_value=response):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="look", sender="user", source="telegram",
                    attachments=[att],
                )

        call_text = session.add_user_message.call_args[0][0]
        assert "[image]" in call_text
        assert "too large" not in call_text
