"""Tests for the live API call monitor feature.

Tests cover two modules:
1. lucyd.py — monitor callbacks (_write_monitor, _on_response, _on_tool_results)
   wired in _process_message, writing to config.state_dir/monitor.json
2. bin/lucyd-send — show_monitor() reading and formatting monitor.json

Following LUCYD-MUTATION-TESTING-MANUAL.md:
- Call real functions, not reimplemented copies
- Assert with == not in
- AsyncMock for async functions
- monkeypatch for env/globals
"""

import importlib.util
import json
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lucyd import LucydDaemon

# Import lucyd-send as module (no .py extension)
_BIN_DIR = Path(__file__).resolve().parent.parent / "bin"
if not _BIN_DIR.exists():
    # Mutmut fallback: bin/ isn't copied to mutants/
    _BIN_DIR = Path(__file__).resolve().parent.parent.parent / "bin"
_loader = SourceFileLoader("lucyd_send", str(_BIN_DIR / "lucyd-send"))
_spec = importlib.util.spec_from_loader("lucyd_send", _loader)
lucyd_send = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lucyd_send)

show_monitor = lucyd_send.show_monitor


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


def _make_daemon_for_monitor(tmp_path, monitor_dir):
    """Build a daemon rigged for monitor testing.

    Returns (daemon, provider, session, monitor_path).
    The monitor writes to monitor_dir/monitor.json instead of ~/.lucyd/.
    """
    config = _make_config(tmp_path)
    daemon = LucydDaemon(config)

    provider = MagicMock()
    provider.format_system = MagicMock(return_value=[])
    provider.format_messages = MagicMock(return_value=[])
    provider.format_tools = MagicMock(return_value=[])
    daemon.providers = {"primary": provider}

    session = MagicMock()
    session.id = "mon-test-session"
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
    daemon.config.state_dir = monitor_dir
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
    daemon.config.error_message = "Error"
    daemon.config.message_retries = 0
    daemon.config.message_retry_base_delay = 0.01
    daemon.config.raw = MagicMock(return_value=0.0)

    monitor_path = monitor_dir / "monitor.json"

    return daemon, provider, session, monitor_path


def _make_response(text="ok", stop_reason="end_turn", tool_calls=None,
                   input_tokens=1000, output_tokens=100,
                   cache_read_tokens=0, cache_write_tokens=0):
    """Build a mock LLMResponse with proper usage attrs."""
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


def _make_tool_call(name, call_id="tc-1"):
    """Build a mock ToolCall."""
    tc = MagicMock()
    tc.name = name
    tc.id = call_id
    return tc


# ─── Monitor Callbacks in lucyd.py ───────────────────────────────


class TestMonitorCallbacksWiring:
    """Verify that _process_message wires on_response and on_tool_results
    callbacks into run_agentic_loop and writes monitor.json correctly."""

    @pytest.mark.asyncio
    async def test_monitor_file_written_on_entry(self, tmp_path):
        """Before the agentic loop runs, monitor.json should exist with state=thinking."""
        monitor_dir = tmp_path / "monitor_out"
        monitor_dir.mkdir()
        daemon, provider, session, monitor_path = _make_daemon_for_monitor(tmp_path, monitor_dir)

        response = _make_response()

        captured_kwargs = {}

        async def fake_loop(**kwargs):
            captured_kwargs.update(kwargs)
            # At this point, monitor.json should already exist from the initial write
            assert monitor_path.exists(), "monitor.json should be written before agentic loop"
            data = json.loads(monitor_path.read_text())
            assert data["state"] == "thinking"
            assert data["turn"] == 1
            assert data["contact"] == "TestUser"
            assert data["session_id"] == "mon-test-session"
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello",
                    sender="TestUser",
                    source="telegram",
                )

    @pytest.mark.asyncio
    async def test_callbacks_passed_to_agentic_loop(self, tmp_path):
        """on_response and on_tool_results are passed as callables to run_agentic_loop."""
        monitor_dir = tmp_path / "monitor_out"
        monitor_dir.mkdir()
        daemon, provider, session, monitor_path = _make_daemon_for_monitor(tmp_path, monitor_dir)

        response = _make_response()
        captured_kwargs = {}

        async def fake_loop(**kwargs):
            captured_kwargs.update(kwargs)
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="TestUser", source="telegram",
                )

        assert captured_kwargs["on_response"] is not None
        assert callable(captured_kwargs["on_response"])
        assert captured_kwargs["on_tool_results"] is not None
        assert callable(captured_kwargs["on_tool_results"])

    @pytest.mark.asyncio
    async def test_on_response_end_turn_writes_idle(self, tmp_path):
        """on_response with stop_reason=end_turn writes state=idle."""
        monitor_dir = tmp_path / "monitor_out"
        monitor_dir.mkdir()
        daemon, provider, session, monitor_path = _make_daemon_for_monitor(tmp_path, monitor_dir)

        response = _make_response(stop_reason="end_turn", output_tokens=200)

        async def fake_loop(**kwargs):
            on_resp = kwargs["on_response"]
            on_resp(response)
            data = json.loads(monitor_path.read_text())
            assert data["state"] == "idle"
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="TestUser", source="telegram",
                )

    @pytest.mark.asyncio
    async def test_on_response_tool_use_writes_tools_state(self, tmp_path):
        """on_response with stop_reason=tool_use writes state=tools with tool names."""
        monitor_dir = tmp_path / "monitor_out"
        monitor_dir.mkdir()
        daemon, provider, session, monitor_path = _make_daemon_for_monitor(tmp_path, monitor_dir)

        tc1 = _make_tool_call("memory_search", "tc-1")
        tc2 = _make_tool_call("read", "tc-2")
        response = _make_response(stop_reason="tool_use", tool_calls=[tc1, tc2])

        async def fake_loop(**kwargs):
            on_resp = kwargs["on_response"]
            on_resp(response)
            data = json.loads(monitor_path.read_text())
            assert data["state"] == "tools"
            assert data["tools_in_flight"] == ["memory_search", "read"]
            return _make_response()  # final response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="TestUser", source="telegram",
                )

    @pytest.mark.asyncio
    async def test_on_tool_results_increments_turn_and_writes_thinking(self, tmp_path):
        """on_tool_results increments turn counter and writes state=thinking."""
        monitor_dir = tmp_path / "monitor_out"
        monitor_dir.mkdir()
        daemon, provider, session, monitor_path = _make_daemon_for_monitor(tmp_path, monitor_dir)

        tc = _make_tool_call("exec")
        tool_response = _make_response(stop_reason="tool_use", tool_calls=[tc])
        final_response = _make_response(stop_reason="end_turn")

        async def fake_loop(**kwargs):
            on_resp = kwargs["on_response"]
            on_tool = kwargs["on_tool_results"]
            # Turn 1: API response with tool use
            on_resp(tool_response)
            # Tool execution completes
            on_tool({"role": "tool_results", "results": []})
            data = json.loads(monitor_path.read_text())
            assert data["state"] == "thinking"
            assert data["turn"] == 2
            # Turn 2: Final response
            on_resp(final_response)
            return final_response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="TestUser", source="telegram",
                )

    @pytest.mark.asyncio
    async def test_turns_history_records_all_turns(self, tmp_path):
        """Each on_response call appends to the turns history list."""
        monitor_dir = tmp_path / "monitor_out"
        monitor_dir.mkdir()
        daemon, provider, session, monitor_path = _make_daemon_for_monitor(tmp_path, monitor_dir)

        tc = _make_tool_call("web_search")
        resp1 = _make_response(stop_reason="tool_use", tool_calls=[tc],
                               output_tokens=150, input_tokens=5000,
                               cache_read_tokens=3000, cache_write_tokens=1000)
        resp2 = _make_response(stop_reason="end_turn", output_tokens=300)

        async def fake_loop(**kwargs):
            on_resp = kwargs["on_response"]
            on_tool = kwargs["on_tool_results"]
            on_resp(resp1)
            on_tool({"role": "tool_results", "results": []})
            on_resp(resp2)

            data = json.loads(monitor_path.read_text())
            assert len(data["turns"]) == 2

            # Turn 1
            t1 = data["turns"][0]
            assert t1["output_tokens"] == 150
            assert t1["input_tokens"] == 5000
            assert t1["cache_read_tokens"] == 3000
            assert t1["cache_write_tokens"] == 1000
            assert t1["stop_reason"] == "tool_use"
            assert t1["tools"] == ["web_search"]
            assert t1["duration_ms"] >= 0

            # Turn 2
            t2 = data["turns"][1]
            assert t2["output_tokens"] == 300
            assert t2["stop_reason"] == "end_turn"
            assert t2["tools"] == []

            return resp2

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="TestUser", source="telegram",
                )

    @pytest.mark.asyncio
    async def test_finally_block_writes_idle(self, tmp_path):
        """After _process_message completes (even with error), monitor shows idle."""
        monitor_dir = tmp_path / "monitor_out"
        monitor_dir.mkdir()
        daemon, provider, session, monitor_path = _make_daemon_for_monitor(tmp_path, monitor_dir)

        async def fake_loop(**kwargs):
            raise RuntimeError("API down")

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="TestUser", source="telegram",
                )

        # After the error, finally block should have written idle
        data = json.loads(monitor_path.read_text())
        assert data["state"] == "idle"

    @pytest.mark.asyncio
    async def test_monitor_records_model_from_config(self, tmp_path):
        """Monitor file records the model name from model config."""
        monitor_dir = tmp_path / "monitor_out"
        monitor_dir.mkdir()
        daemon, provider, session, monitor_path = _make_daemon_for_monitor(tmp_path, monitor_dir)

        response = _make_response()

        async def fake_loop(**kwargs):
            data = json.loads(monitor_path.read_text())
            assert data["model"] == "test-model"
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="TestUser", source="telegram",
                )

    @pytest.mark.asyncio
    async def test_monitor_records_contact(self, tmp_path):
        """Monitor file records the contact/sender name."""
        monitor_dir = tmp_path / "monitor_out"
        monitor_dir.mkdir()
        daemon, provider, session, monitor_path = _make_daemon_for_monitor(tmp_path, monitor_dir)

        response = _make_response()

        async def fake_loop(**kwargs):
            data = json.loads(monitor_path.read_text())
            assert data["contact"] == "Nicolas"
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="Nicolas", source="telegram",
                )

    @pytest.mark.asyncio
    async def test_monitor_records_session_id(self, tmp_path):
        """Monitor file records the session ID."""
        monitor_dir = tmp_path / "monitor_out"
        monitor_dir.mkdir()
        daemon, provider, session, monitor_path = _make_daemon_for_monitor(tmp_path, monitor_dir)

        response = _make_response()

        async def fake_loop(**kwargs):
            data = json.loads(monitor_path.read_text())
            assert data["session_id"] == "mon-test-session"
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="TestUser", source="telegram",
                )

    @pytest.mark.asyncio
    async def test_monitor_updated_at_is_recent(self, tmp_path):
        """Monitor updated_at timestamp is close to current time."""
        monitor_dir = tmp_path / "monitor_out"
        monitor_dir.mkdir()
        daemon, provider, session, monitor_path = _make_daemon_for_monitor(tmp_path, monitor_dir)

        response = _make_response()
        before = time.time()

        async def fake_loop(**kwargs):
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="TestUser", source="telegram",
                )

        after = time.time()
        data = json.loads(monitor_path.read_text())
        assert data["updated_at"] >= before
        assert data["updated_at"] <= after

    @pytest.mark.asyncio
    async def test_monitor_atomic_write_via_rename(self, tmp_path):
        """Monitor uses tmp file + rename for atomic writes."""
        monitor_dir = tmp_path / "monitor_out"
        monitor_dir.mkdir()
        daemon, provider, session, monitor_path = _make_daemon_for_monitor(tmp_path, monitor_dir)

        response = _make_response()
        rename_called = []

        original_rename = Path.rename

        def track_rename(self_path, target):
            if str(target).endswith("monitor.json"):
                rename_called.append(str(self_path))
            return original_rename(self_path, target)

        async def fake_loop(**kwargs):
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                with patch.object(Path, "rename", track_rename):
                    await daemon._process_message(
                        text="hello", sender="TestUser", source="telegram",
                    )

        # At least the initial "thinking" + final "idle" writes should have used rename
        assert len(rename_called) >= 2
        # All renames should come from .tmp files
        for path in rename_called:
            assert path.endswith(".tmp")

    @pytest.mark.asyncio
    async def test_monitor_write_failure_does_not_crash(self, tmp_path):
        """If monitor write fails, the daemon continues without crashing."""
        monitor_dir = tmp_path / "monitor_out"
        monitor_dir.mkdir()
        daemon, provider, session, _ = _make_daemon_for_monitor(tmp_path, monitor_dir)

        # Point state_dir to a non-existent directory so monitor write fails
        daemon.config.state_dir = tmp_path / "readonly"

        response = _make_response()

        async def fake_loop(**kwargs):
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                # Should not raise
                await daemon._process_message(
                    text="hello", sender="TestUser", source="telegram",
                )

    @pytest.mark.asyncio
    async def test_on_response_no_tool_calls_empty_tools_list(self, tmp_path):
        """on_response with end_turn and no tool_calls records tools as empty list."""
        monitor_dir = tmp_path / "monitor_out"
        monitor_dir.mkdir()
        daemon, provider, session, monitor_path = _make_daemon_for_monitor(tmp_path, monitor_dir)

        response = _make_response(stop_reason="end_turn", tool_calls=[])

        async def fake_loop(**kwargs):
            on_resp = kwargs["on_response"]
            on_resp(response)
            data = json.loads(monitor_path.read_text())
            assert data["turns"][0]["tools"] == []
            assert data["tools_in_flight"] == []
            return response

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="TestUser", source="telegram",
                )

    @pytest.mark.asyncio
    async def test_multi_turn_sequence_full(self, tmp_path):
        """Full 3-turn sequence: thinking → tools → thinking → tools → thinking → idle."""
        monitor_dir = tmp_path / "monitor_out"
        monitor_dir.mkdir()
        daemon, provider, session, monitor_path = _make_daemon_for_monitor(tmp_path, monitor_dir)

        tc_mem = _make_tool_call("memory_search")
        tc_read = _make_tool_call("read")
        resp1 = _make_response(stop_reason="tool_use", tool_calls=[tc_mem], output_tokens=100)
        resp2 = _make_response(stop_reason="tool_use", tool_calls=[tc_read], output_tokens=200)
        resp3 = _make_response(stop_reason="end_turn", output_tokens=300)

        states_seen = []

        async def fake_loop(**kwargs):
            on_resp = kwargs["on_response"]
            on_tool = kwargs["on_tool_results"]

            # Turn 1
            on_resp(resp1)
            states_seen.append(json.loads(monitor_path.read_text())["state"])

            on_tool({"role": "tool_results", "results": []})
            data = json.loads(monitor_path.read_text())
            states_seen.append(data["state"])
            assert data["turn"] == 2

            # Turn 2
            on_resp(resp2)
            states_seen.append(json.loads(monitor_path.read_text())["state"])

            on_tool({"role": "tool_results", "results": []})
            data = json.loads(monitor_path.read_text())
            states_seen.append(data["state"])
            assert data["turn"] == 3

            # Turn 3
            on_resp(resp3)
            states_seen.append(json.loads(monitor_path.read_text())["state"])

            return resp3

        with patch("lucyd.run_agentic_loop", side_effect=fake_loop):
            with patch("tools.status.set_current_session"):
                await daemon._process_message(
                    text="hello", sender="TestUser", source="telegram",
                )

        assert states_seen == ["tools", "thinking", "tools", "thinking", "idle"]

        # Check final state after finally block
        data = json.loads(monitor_path.read_text())
        assert data["state"] == "idle"
        assert len(data["turns"]) == 3


# ─── show_monitor (bin/lucyd-send) ──────────────────────────────


class TestShowMonitorNoFile:
    """show_monitor when monitor.json doesn't exist."""

    def test_no_file_prints_no_data(self, tmp_path, capsys):
        show_monitor(tmp_path)
        out = capsys.readouterr().out
        assert out == "Lucy \u2014 no monitor data\n"

    def test_corrupt_json_prints_no_data(self, tmp_path, capsys):
        (tmp_path / "monitor.json").write_text("{invalid json")
        show_monitor(tmp_path)
        out = capsys.readouterr().out
        assert out == "Lucy \u2014 no monitor data\n"


class TestShowMonitorIdle:
    """show_monitor with state=idle."""

    def test_idle_output(self, tmp_path, capsys):
        data = {"state": "idle", "contact": "", "model": "", "turn": 0,
                "turn_started_at": 0, "updated_at": time.time(),
                "tools_in_flight": [], "turns": []}
        (tmp_path / "monitor.json").write_text(json.dumps(data))

        show_monitor(tmp_path)
        out = capsys.readouterr().out
        assert "Lucy \u2014 idle" in out
        assert "\u2500" in out  # separator line

    def test_idle_does_not_show_contact(self, tmp_path, capsys):
        """Idle state should not display contact/model info."""
        data = {"state": "idle", "contact": "Nicolas", "model": "test-model",
                "turn": 0, "turn_started_at": 0, "updated_at": time.time(),
                "tools_in_flight": [], "turns": []}
        (tmp_path / "monitor.json").write_text(json.dumps(data))

        show_monitor(tmp_path)
        out = capsys.readouterr().out
        assert "Contact:" not in out


class TestShowMonitorThinking:
    """show_monitor with state=thinking."""

    def test_thinking_shows_turn_and_elapsed(self, tmp_path, capsys):
        now = time.time()
        data = {"state": "thinking", "contact": "Nicolas",
                "model": "claude-sonnet-4-5-20250929", "turn": 3,
                "turn_started_at": now - 4.2, "updated_at": now,
                "tools_in_flight": [], "turns": [
                    {"duration_ms": 3200, "output_tokens": 156,
                     "stop_reason": "tool_use", "tools": ["memory_search"]},
                    {"duration_ms": 4500, "output_tokens": 342,
                     "stop_reason": "tool_use", "tools": ["read"]},
                ]}
        (tmp_path / "monitor.json").write_text(json.dumps(data))

        show_monitor(tmp_path)
        out = capsys.readouterr().out

        assert "thinking" in out
        assert "turn 3" in out
        assert "Contact:  Nicolas" in out
        assert "Model:    claude-sonnet-4-5-20250929" in out
        # Turn history
        assert "T1" in out
        assert "156" in out
        assert "memory_search" in out
        assert "T2" in out
        assert "342" in out
        assert "read" in out
        # Current thinking turn
        assert "T3" in out
        assert "...thinking" in out

    def test_thinking_current_turn_shows_elapsed(self, tmp_path, capsys):
        """When state=thinking and turn > len(turns), show current thinking turn."""
        now = time.time()
        data = {"state": "thinking", "contact": "X", "model": "m", "turn": 2,
                "turn_started_at": now - 5.0, "updated_at": now,
                "tools_in_flight": [],
                "turns": [{"duration_ms": 1000, "output_tokens": 50,
                           "stop_reason": "tool_use", "tools": ["exec"]}]}
        (tmp_path / "monitor.json").write_text(json.dumps(data))

        show_monitor(tmp_path)
        out = capsys.readouterr().out
        assert "T2" in out
        assert "...thinking" in out


class TestShowMonitorTools:
    """show_monitor with state=tools."""

    def test_tools_state_shows_running(self, tmp_path, capsys):
        now = time.time()
        data = {"state": "tools", "contact": "Nicolas", "model": "test",
                "turn": 2, "turn_started_at": now, "updated_at": now,
                "tools_in_flight": ["exec"],
                "turns": [
                    {"duration_ms": 2000, "output_tokens": 100,
                     "stop_reason": "tool_use", "tools": ["read"]},
                    {"duration_ms": 1500, "output_tokens": 200,
                     "stop_reason": "tool_use", "tools": ["exec"]},
                ]}
        (tmp_path / "monitor.json").write_text(json.dumps(data))

        show_monitor(tmp_path)
        out = capsys.readouterr().out
        assert "tools (turn 2)" in out
        assert "(running)" in out

    def test_tools_in_flight_names(self, tmp_path, capsys):
        now = time.time()
        data = {"state": "tools", "contact": "X", "model": "m",
                "turn": 1, "turn_started_at": now, "updated_at": now,
                "tools_in_flight": ["web_search", "read"],
                "turns": [
                    {"duration_ms": 1000, "output_tokens": 50,
                     "stop_reason": "tool_use", "tools": ["web_search", "read"]},
                ]}
        (tmp_path / "monitor.json").write_text(json.dumps(data))

        show_monitor(tmp_path)
        out = capsys.readouterr().out
        assert "web_search" in out
        assert "read" in out


class TestShowMonitorTurnHistory:
    """show_monitor turn history formatting."""

    def test_turn_duration_format(self, tmp_path, capsys):
        """Duration is displayed in seconds (e.g., '3.2s')."""
        now = time.time()
        data = {"state": "idle", "contact": "", "model": "", "turn": 0,
                "turn_started_at": 0, "updated_at": now,
                "tools_in_flight": [],
                "turns": [{"duration_ms": 3200, "output_tokens": 100,
                           "stop_reason": "end_turn", "tools": []}]}
        (tmp_path / "monitor.json").write_text(json.dumps(data))

        show_monitor(tmp_path)
        out = capsys.readouterr().out
        assert "3.2s" in out

    def test_end_turn_stop_reason_displayed(self, tmp_path, capsys):
        """stop_reason=end_turn is shown directly."""
        now = time.time()
        data = {"state": "idle", "contact": "", "model": "", "turn": 0,
                "turn_started_at": 0, "updated_at": now,
                "tools_in_flight": [],
                "turns": [{"duration_ms": 1000, "output_tokens": 50,
                           "stop_reason": "end_turn", "tools": []}]}
        (tmp_path / "monitor.json").write_text(json.dumps(data))

        show_monitor(tmp_path)
        out = capsys.readouterr().out
        assert "end_turn" in out

    def test_tool_use_with_arrow_format(self, tmp_path, capsys):
        """tool_use shows 'tool_use -> tool_name' with arrow."""
        now = time.time()
        data = {"state": "idle", "contact": "", "model": "", "turn": 0,
                "turn_started_at": 0, "updated_at": now,
                "tools_in_flight": [],
                "turns": [{"duration_ms": 2000, "output_tokens": 100,
                           "stop_reason": "tool_use",
                           "tools": ["memory_search"]}]}
        (tmp_path / "monitor.json").write_text(json.dumps(data))

        show_monitor(tmp_path)
        out = capsys.readouterr().out
        assert "tool_use" in out
        assert "\u2192" in out  # arrow
        assert "memory_search" in out

    def test_multiple_tools_comma_separated(self, tmp_path, capsys):
        """Multiple tools in a turn are comma-separated."""
        now = time.time()
        data = {"state": "idle", "contact": "", "model": "", "turn": 0,
                "turn_started_at": 0, "updated_at": now,
                "tools_in_flight": [],
                "turns": [{"duration_ms": 2000, "output_tokens": 100,
                           "stop_reason": "tool_use",
                           "tools": ["read", "web_search"]}]}
        (tmp_path / "monitor.json").write_text(json.dumps(data))

        show_monitor(tmp_path)
        out = capsys.readouterr().out
        assert "read, web_search" in out


class TestShowMonitorStaleDetection:
    """show_monitor stale detection (>60s without update)."""

    def test_no_warning_when_idle(self, tmp_path, capsys):
        """No stale warning when state is idle, even if old."""
        data = {"state": "idle", "contact": "", "model": "", "turn": 0,
                "turn_started_at": 0, "updated_at": time.time() - 300,
                "tools_in_flight": [], "turns": []}
        (tmp_path / "monitor.json").write_text(json.dumps(data))

        show_monitor(tmp_path)
        out = capsys.readouterr().out
        assert "\u26a0" not in out

    def test_warning_when_thinking_and_stale_seconds(self, tmp_path, capsys):
        """Warning appears when thinking state is >60s but <120s old."""
        data = {"state": "thinking", "contact": "X", "model": "m", "turn": 1,
                "turn_started_at": time.time() - 90, "updated_at": time.time() - 90,
                "tools_in_flight": [], "turns": []}
        (tmp_path / "monitor.json").write_text(json.dumps(data))

        show_monitor(tmp_path)
        out = capsys.readouterr().out
        assert "\u26a0" in out
        assert "90s" in out
        assert "daemon may be stuck or dead" in out

    def test_warning_when_thinking_and_stale_minutes(self, tmp_path, capsys):
        """Warning shows minutes when >120s old."""
        data = {"state": "thinking", "contact": "X", "model": "m", "turn": 1,
                "turn_started_at": time.time() - 200, "updated_at": time.time() - 200,
                "tools_in_flight": [], "turns": []}
        (tmp_path / "monitor.json").write_text(json.dumps(data))

        show_monitor(tmp_path)
        out = capsys.readouterr().out
        assert "\u26a0" in out
        assert "3m" in out
        assert "daemon may be stuck or dead" in out

    def test_no_warning_when_recent(self, tmp_path, capsys):
        """No warning when updated_at is recent (<60s)."""
        data = {"state": "thinking", "contact": "X", "model": "m", "turn": 1,
                "turn_started_at": time.time() - 5, "updated_at": time.time() - 5,
                "tools_in_flight": [], "turns": []}
        (tmp_path / "monitor.json").write_text(json.dumps(data))

        show_monitor(tmp_path)
        out = capsys.readouterr().out
        assert "\u26a0" not in out

    def test_warning_when_tools_state_stale(self, tmp_path, capsys):
        """Warning also triggers for tools state, not just thinking."""
        data = {"state": "tools", "contact": "X", "model": "m", "turn": 1,
                "turn_started_at": time.time() - 120, "updated_at": time.time() - 120,
                "tools_in_flight": ["exec"], "turns": [
                    {"duration_ms": 1000, "output_tokens": 50,
                     "stop_reason": "tool_use", "tools": ["exec"]},
                ]}
        (tmp_path / "monitor.json").write_text(json.dumps(data))

        show_monitor(tmp_path)
        out = capsys.readouterr().out
        assert "\u26a0" in out


class TestShowMonitorUnknownState:
    """show_monitor with unexpected/unknown state values."""

    def test_unknown_state_displayed(self, tmp_path, capsys):
        data = {"state": "custom_state", "contact": "", "model": "", "turn": 0,
                "turn_started_at": 0, "updated_at": time.time(),
                "tools_in_flight": [], "turns": []}
        (tmp_path / "monitor.json").write_text(json.dumps(data))

        show_monitor(tmp_path)
        out = capsys.readouterr().out
        assert "custom_state" in out
