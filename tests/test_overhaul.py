"""Tests for the architectural overhaul: config schema, unified tool loader,
SessionManager public API, _MonitorWriter, multi-model routing, backward compat.

Style: call REAL functions, swap module globals with try/finally, assert on output.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from config import Config, ConfigError

# ─── 1. Config Schema ────────────────────────────────────────────


class TestConfigSchemaMinimal:
    """Minimal config constructs successfully; schema defaults work."""

    def test_minimal_config_constructs(self):
        """agent + channel + models.primary only — should succeed."""
        data = {
            "agent": {"name": "Test", "workspace": "/tmp/ws"},
            "channel": {"type": "cli"},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
        }
        cfg = Config(data)
        assert cfg.agent_name == "Test"

    def test_stt_backend_defaults_to_empty(self):
        data = {
            "agent": {"name": "Test", "workspace": "/tmp/ws"},
            "channel": {"type": "cli"},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
        }
        cfg = Config(data)
        assert cfg.stt_backend == ""

    def test_documents_enabled_defaults_to_false(self):
        data = {
            "agent": {"name": "Test", "workspace": "/tmp/ws"},
            "channel": {"type": "cli"},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
        }
        cfg = Config(data)
        assert cfg.documents_enabled is False

    def test_tools_enabled_defaults_to_empty_list(self):
        data = {
            "agent": {"name": "Test", "workspace": "/tmp/ws"},
            "channel": {"type": "cli"},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
        }
        cfg = Config(data)
        assert cfg.tools_enabled == []


class TestConfigSchemaAllErrors:
    """Missing core fields report ALL errors at once."""

    def test_empty_dict_reports_multiple_errors(self):
        with pytest.raises(ConfigError) as exc_info:
            Config({})
        msg = str(exc_info.value)
        assert "agent" in msg.lower()
        assert "primary" in msg.lower()
        # Must have at least 2 distinct error lines (agent name + models primary)
        error_lines = [line for line in msg.split("\n") if line.strip().startswith("-")]
        assert len(error_lines) >= 2


class TestConfigSchemaFloatCoercion:
    """Float coercion: TOML integer -> Python float for float-typed entries."""

    def test_agent_timeout_int_coerced_to_float(self):
        data = {
            "agent": {"name": "Test", "workspace": "/tmp/ws"},
            "channel": {"type": "cli"},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
            "behavior": {"agent_timeout_seconds": 600},
        }
        cfg = Config(data)
        assert cfg.agent_timeout == 600.0
        assert isinstance(cfg.agent_timeout, float)


class TestConfigSchemaPathResolution:
    """Path type entries resolve from string to Path."""

    def test_state_dir_is_path(self):
        data = {
            "agent": {"name": "Test", "workspace": "/tmp/ws"},
            "channel": {"type": "cli"},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
            "paths": {"state_dir": "/tmp/my-state"},
        }
        cfg = Config(data)
        assert isinstance(cfg.state_dir, Path)
        assert cfg.state_dir == Path("/tmp/my-state").resolve()


# ─── 2. Unified Tool Loader (_TOOL_MODULES) ─────────────────────


class TestToolModulesCoverage:
    """_TOOL_MODULES covers all known tool names."""

    def test_all_tool_names_mapped(self, minimal_toml_data):
        """Every tool name in a full config is covered by _TOOL_MODULES."""
        # Import _TOOL_MODULES from the Daemon class
        import lucyd as daemon_mod
        tool_modules = daemon_mod.LucydDaemon._TOOL_MODULES
        # Collect all tool names from _TOOL_MODULES
        all_mapped = set()
        for _, names in tool_modules:
            all_mapped |= names
        # All tools from a full config should be in the mapping
        cfg = Config(minimal_toml_data)
        for tool_name in cfg.tools_enabled:
            assert tool_name in all_mapped, f"Tool '{tool_name}' not in _TOOL_MODULES"


class TestToolModulesImportable:
    """Each module in _TOOL_MODULES is importable and has TOOLS list."""

    def test_modules_have_tools_list(self):
        import importlib

        import lucyd as daemon_mod
        tool_modules = daemon_mod.LucydDaemon._TOOL_MODULES
        for module_path, _ in tool_modules:
            mod = importlib.import_module(module_path)
            tools_list = getattr(mod, "TOOLS", None)
            assert tools_list is not None, f"{module_path} missing TOOLS"
            assert isinstance(tools_list, list), f"{module_path}.TOOLS is not a list"


# ─── 3. SessionManager Public API ───────────────────────────────


class TestSessionManagerPublicAPI:
    """SessionManager: has_session, list_contacts, session_count, save_state."""

    def test_has_session_false_for_unknown(self, tmp_path):
        from session import SessionManager
        mgr = SessionManager(tmp_path / "sessions")
        assert mgr.has_session("nobody") is False

    def test_has_session_true_after_get_or_create(self, tmp_path):
        from session import SessionManager
        mgr = SessionManager(tmp_path / "sessions")
        mgr.get_or_create("alice")
        assert mgr.has_session("alice") is True

    def test_list_contacts_after_sessions(self, tmp_path):
        from session import SessionManager
        mgr = SessionManager(tmp_path / "sessions")
        mgr.get_or_create("alice")
        mgr.get_or_create("bob")
        contacts = mgr.list_contacts()
        assert "alice" in contacts
        assert "bob" in contacts
        assert len(contacts) == 2

    def test_session_count_matches_index(self, tmp_path):
        from session import SessionManager
        mgr = SessionManager(tmp_path / "sessions")
        assert mgr.session_count() == 0
        mgr.get_or_create("alice")
        assert mgr.session_count() == 1
        mgr.get_or_create("bob")
        assert mgr.session_count() == 2

    def test_save_state_calls_session_save(self, tmp_path):
        from session import SessionManager
        mgr = SessionManager(tmp_path / "sessions")
        session = mgr.get_or_create("alice")
        session.messages.append({"role": "user", "content": "hello"})
        mgr.save_state(session)
        # Verify the state file was written
        assert session.state_path.exists()
        state = json.loads(session.state_path.read_text())
        assert len(state["messages"]) == 1
        assert state["messages"][0]["content"] == "hello"


# ─── 4. _MonitorWriter ──────────────────────────────────────────


class TestMonitorWriter:
    """_MonitorWriter: write, on_response, on_tool_results."""

    def _make_writer(self, tmp_path):
        from monitor import MonitorWriter
        path = tmp_path / "monitor.json"
        return MonitorWriter(
            path=path,
            contact="alice",
            session_id="sess-123",
            trace_id="trace-456",
            model="test-model",
        )

    def test_write_creates_json_file(self, tmp_path):
        writer = self._make_writer(tmp_path)
        writer.write("thinking")
        path = tmp_path / "monitor.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["state"] == "thinking"
        assert data["contact"] == "alice"
        assert data["session_id"] == "sess-123"
        assert data["trace_id"] == "trace-456"
        assert data["model"] == "test-model"

    def test_on_response_increments_turns(self, tmp_path):
        from providers import LLMResponse, Usage
        writer = self._make_writer(tmp_path)
        response = LLMResponse(
            text="hello",
            tool_calls=[],
            stop_reason="end_turn",
            usage=Usage(input_tokens=100, output_tokens=50),
        )
        writer.on_response(response)
        assert len(writer._turns) == 1
        assert writer._turns[0]["input_tokens"] == 100
        assert writer._turns[0]["output_tokens"] == 50
        assert writer._turns[0]["stop_reason"] == "end_turn"

    def test_on_tool_results_increments_turn_counter(self, tmp_path):
        writer = self._make_writer(tmp_path)
        assert writer._turn == 1
        writer.on_tool_results({"role": "tool_results", "results": []})
        assert writer._turn == 2
        # Verify it also writes state
        path = tmp_path / "monitor.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["state"] == "thinking"


# ─── 5. Multi-Model Routing Config Properties ───────────────────


class TestMultiModelRoutingDefaults:
    """Model routing properties default to empty string."""

    def test_compaction_model_default(self):
        data = {
            "agent": {"name": "Test", "workspace": "/tmp/ws"},
            "channel": {"type": "cli"},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
        }
        cfg = Config(data)
        assert cfg.compaction_model == ""

    def test_consolidation_model_default(self):
        data = {
            "agent": {"name": "Test", "workspace": "/tmp/ws"},
            "channel": {"type": "cli"},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
        }
        cfg = Config(data)
        assert cfg.consolidation_model == ""

    def test_subagent_model_default(self):
        data = {
            "agent": {"name": "Test", "workspace": "/tmp/ws"},
            "channel": {"type": "cli"},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
        }
        cfg = Config(data)
        assert cfg.subagent_model == ""


class TestMultiModelRoutingOverrides:
    """Model routing properties from config data."""

    def test_routing_overrides_from_config(self):
        data = {
            "agent": {"name": "Test", "workspace": "/tmp/ws"},
            "channel": {"type": "cli"},
            "models": {
                "primary": {"provider": "anthropic-compat", "model": "opus"},
                "routing": {
                    "compaction": "haiku",
                    "consolidation": "sonnet",
                    "subagent": "sonnet",
                },
            },
        }
        cfg = Config(data)
        assert cfg.compaction_model == "haiku"
        assert cfg.consolidation_model == "sonnet"
        assert cfg.subagent_model == "sonnet"


# ─── 6. configure() Direct Tests ─────────────────────────────────


class TestConfigureMemoryRead:
    """tools.memory_read.configure sets module globals directly."""

    def test_configure_memory_sets_global(self):
        import tools.memory_read as mod
        original = mod._memory
        fake = MagicMock()
        try:
            mod.configure(memory=fake)
            assert mod._memory is fake
        finally:
            mod._memory = original


class TestConfigureStatus:
    """tools.status.configure sets session_getter."""

    def test_configure_session_getter_sets_global(self):
        import tools.status as mod
        original = mod._session_getter
        fake = MagicMock()
        try:
            mod.configure(session_getter=fake)
            assert mod._session_getter is fake
        finally:
            mod._session_getter = original
