"""Tests for the live API call monitor feature.

Tests cover monitor callbacks (_write_monitor, _on_response, _on_tool_results)
wired in _process_message, writing to config.state_dir/monitor.json.
"""

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lucyd import LucydDaemon



# ─── Helpers ──────────────────────────────────────────────────────


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
        "channel": {"type": "cli", "debounce_ms": 500},
        "http": {
            "enabled": False, "host": "127.0.0.1", "port": 8100, "token_env": "",
            "download_dir": "/tmp/lucyd-http", "max_body_bytes": 10485760,
            "callback_url": "", "callback_token_env": "", "callback_timeout": 10,
            "callback_max_failures": 10, "max_attachment_bytes": 52428800,
            "rate_limit": 30, "rate_window": 60, "status_rate_limit": 60,
            "rate_limit_cleanup_threshold": 1000,
        },
        "models": {
            "primary": {
                "provider": "anthropic-compat", "model": "test-model",
                "max_tokens": 1024, "cost_per_mtok": [1.0, 5.0, 0.1],
            },
        },
        "memory": {
            "db": "", "search_top_k": 10, "vector_search_limit": 10000,
            "fts_min_results": 3, "embedding_timeout": 15,
            "consolidation": {"enabled": False, "min_messages": 4, "confidence_threshold": 0.6, "max_extraction_chars": 50000},
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
        "logging": {"max_bytes": 10485760, "backup_count": 3, "suppress": []},
        "vision": {"max_image_bytes": 5242880, "max_dimension": 1568,
                   "jpeg_quality_steps": [85, 60, 40],
                   },
        "behavior": {
            "silent_tokens": ["NO_REPLY"], "typing_indicators": True, "error_message": "error",
            "sqlite_timeout": 30,
            "api_retries": 2, "api_retry_base_delay": 2.0, "message_retries": 2, "message_retry_base_delay": 30.0,
            "audit_truncation_limit": 500, "agent_timeout_seconds": 600,
            "max_turns_per_message": 50, "max_cost_per_message": 0.0,
            "queue_capacity": 1000, "queue_poll_interval": 1.0, "quote_max_chars": 200,
            "notify_target": "",
            "compaction": {
                "threshold_tokens": 150000, "max_tokens": 2048,
                "prompt": "Summarize for {agent_name}.", "keep_recent_pct": 0.33,
                "keep_recent_pct_min": 0.05, "keep_recent_pct_max": 0.9,
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
    daemon.provider = provider
    daemon._providers = {"primary": provider}

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
    session.save_state = MagicMock()

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
    daemon.config.model_config = MagicMock(return_value={
        "model": "test-model", "cost_per_mtok": [1.0, 5.0, 0.1],
    })
    daemon.config.typing_indicators = False
    daemon.config.max_turns = 10
    daemon.config.agent_timeout = 30
    daemon.config.agent_id = "test"
    daemon.config.agent_name = "TestAgent"
    daemon.config.metering_currency = "EUR"
    daemon.config.silent_tokens = []
    daemon.config.compaction_threshold = 150000
    daemon.config.always_on_skills = []
    daemon.config.error_message = "Error"
    daemon.config.message_retries = 0
    daemon.config.message_retry_base_delay = 0.01
    daemon.config.raw = MagicMock(return_value=0.0)
    daemon.config.http_callback_max_failures = 10

    from metering import MeteringDB
    daemon.metering_db = MeteringDB(str(tmp_path / "metering.db"))

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
            await daemon._process_message(
                text="hello", sender="TestUser", source="telegram",
            )

        assert states_seen == ["tools", "thinking", "tools", "thinking", "idle"]

        # Check final state after finally block
        data = json.loads(monitor_path.read_text())
        assert data["state"] == "idle"
        assert len(data["turns"]) == 3


