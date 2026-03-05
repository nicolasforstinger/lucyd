"""Tests for tool plugin system — _init_plugins().

Verifies: discovery, filtering by enabled, configure() injection,
bad plugin resilience, no TOOLS list, no plugins dir.
"""

from unittest.mock import MagicMock

from lucyd import LucydDaemon


def _make_config(tmp_path, enabled=None, plugins_dir="plugins.d"):
    """Build a minimal Config for plugin testing."""
    from config import Config

    base = {
        "agent": {
            "name": "TestAgent",
            "workspace": str(tmp_path / "workspace"),
            "context": {"stable": ["SOUL.md"], "semi_stable": []},
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
        "tools": {
            "enabled": enabled or ["read", "write"],
            "plugins_dir": plugins_dir,
        },
        "paths": {
            "state_dir": str(tmp_path / "state"),
            "sessions_dir": str(tmp_path / "sessions"),
            "cost_db": str(tmp_path / "cost.db"),
            "log_file": str(tmp_path / "lucyd.log"),
        },
        "behavior": {"compaction": {"threshold_tokens": 150000}},
    }

    (tmp_path / "workspace").mkdir(exist_ok=True)
    (tmp_path / "workspace" / "SOUL.md").write_text("# Test Soul")
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "sessions").mkdir(exist_ok=True)

    return Config(base, config_dir=tmp_path)


def _make_daemon(tmp_path, enabled=None, plugins_dir="plugins.d"):
    """Build a daemon with tool registry for plugin testing."""
    from tools import ToolRegistry

    config = _make_config(tmp_path, enabled=enabled, plugins_dir=plugins_dir)
    daemon = LucydDaemon(config)
    daemon.tool_registry = ToolRegistry()
    daemon.channel = MagicMock()
    daemon.session_mgr = MagicMock()
    daemon.providers = {"primary": MagicMock()}
    return daemon


def _write_plugin(plugins_dir, filename, content):
    """Write a plugin file to the plugins directory."""
    plugins_dir.mkdir(parents=True, exist_ok=True)
    (plugins_dir / filename).write_text(content)


SIMPLE_PLUGIN = '''\
def my_tool(text: str = "") -> str:
    return f"plugin:{text}"

TOOLS = [
    {
        "name": "my_tool",
        "description": "A simple plugin tool",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
        },
        "function": my_tool,
    },
]
'''

CONFIGURABLE_PLUGIN = '''\
_config_val = None

def configure(config):
    global _config_val
    _config_val = config

def cfg_tool() -> str:
    return str(_config_val)

TOOLS = [
    {
        "name": "cfg_tool",
        "description": "Tool that uses configure()",
        "input_schema": {"type": "object", "properties": {}},
        "function": cfg_tool,
    },
]
'''

MULTI_DEP_PLUGIN = '''\
_received = {}

def configure(config, channel, session_mgr):
    global _received
    _received = {"config": config, "channel": channel, "session_mgr": session_mgr}

def multi_tool() -> str:
    return str(list(_received.keys()))

TOOLS = [
    {
        "name": "multi_tool",
        "description": "Tool with multi-dep configure",
        "input_schema": {"type": "object", "properties": {}},
        "function": multi_tool,
    },
]
'''

BAD_PLUGIN = '''\
raise RuntimeError("Plugin load failure")
'''

NO_TOOLS_PLUGIN = '''\
# A Python file without TOOLS list
def helper():
    pass
'''

TWO_TOOLS_PLUGIN = '''\
def tool_a() -> str:
    return "a"

def tool_b() -> str:
    return "b"

TOOLS = [
    {
        "name": "tool_a",
        "description": "Tool A",
        "input_schema": {"type": "object", "properties": {}},
        "function": tool_a,
    },
    {
        "name": "tool_b",
        "description": "Tool B",
        "input_schema": {"type": "object", "properties": {}},
        "function": tool_b,
    },
]
'''


class TestPluginDiscovery:
    """Plugin files are found and loaded from plugins.d/."""

    def test_loads_simple_plugin(self, tmp_path):
        """Plugin with TOOLS list gets registered."""
        _write_plugin(tmp_path / "plugins.d", "simple.py", SIMPLE_PLUGIN)
        daemon = _make_daemon(tmp_path, enabled=["my_tool"])
        daemon._init_plugins()
        assert "my_tool" in daemon.tool_registry.tool_names

    def test_no_plugins_dir_is_silent(self, tmp_path):
        """Missing plugins.d/ directory doesn't raise."""
        daemon = _make_daemon(tmp_path)
        daemon._init_plugins()  # Should not raise
        assert daemon.tool_registry.tool_names == []

    def test_custom_plugins_dir(self, tmp_path):
        """Custom plugins_dir path works."""
        _write_plugin(tmp_path / "my_plugins", "simple.py", SIMPLE_PLUGIN)
        daemon = _make_daemon(tmp_path, enabled=["my_tool"], plugins_dir="my_plugins")
        daemon._init_plugins()
        assert "my_tool" in daemon.tool_registry.tool_names


class TestPluginFiltering:
    """Only enabled tools from plugins are registered."""

    def test_only_enabled_tools_registered(self, tmp_path):
        """Plugin tools not in enabled list are skipped."""
        _write_plugin(tmp_path / "plugins.d", "two.py", TWO_TOOLS_PLUGIN)
        daemon = _make_daemon(tmp_path, enabled=["tool_a"])
        daemon._init_plugins()
        assert "tool_a" in daemon.tool_registry.tool_names
        assert "tool_b" not in daemon.tool_registry.tool_names

    def test_no_enabled_tools_skips_all(self, tmp_path):
        """Plugin with no matching enabled tools registers nothing."""
        _write_plugin(tmp_path / "plugins.d", "simple.py", SIMPLE_PLUGIN)
        daemon = _make_daemon(tmp_path, enabled=["read", "write"])
        daemon._init_plugins()
        assert "my_tool" not in daemon.tool_registry.tool_names


class TestPluginConfigure:
    """configure() function is called with requested deps."""

    def test_configure_receives_config(self, tmp_path):
        """configure(config) gets the Config object."""
        _write_plugin(tmp_path / "plugins.d", "cfg.py", CONFIGURABLE_PLUGIN)
        daemon = _make_daemon(tmp_path, enabled=["cfg_tool"])
        daemon._init_plugins()
        assert "cfg_tool" in daemon.tool_registry.tool_names

    def test_configure_receives_multiple_deps(self, tmp_path):
        """configure(config, channel, session_mgr) gets all requested deps."""
        _write_plugin(tmp_path / "plugins.d", "multi.py", MULTI_DEP_PLUGIN)
        daemon = _make_daemon(tmp_path, enabled=["multi_tool"])
        daemon._init_plugins()
        assert "multi_tool" in daemon.tool_registry.tool_names


class TestPluginResilience:
    """Bad plugins don't block other plugins."""

    def test_bad_plugin_doesnt_block_others(self, tmp_path):
        """A plugin that raises during import doesn't prevent other plugins."""
        plugins_dir = tmp_path / "plugins.d"
        _write_plugin(plugins_dir, "aaa_bad.py", BAD_PLUGIN)
        _write_plugin(plugins_dir, "zzz_good.py", SIMPLE_PLUGIN)
        daemon = _make_daemon(tmp_path, enabled=["my_tool"])
        daemon._init_plugins()
        assert "my_tool" in daemon.tool_registry.tool_names

    def test_no_tools_list_skipped(self, tmp_path):
        """Plugin without TOOLS list is silently skipped."""
        _write_plugin(tmp_path / "plugins.d", "no_tools.py", NO_TOOLS_PLUGIN)
        daemon = _make_daemon(tmp_path, enabled=["read"])
        daemon._init_plugins()  # Should not raise
        assert daemon.tool_registry.tool_names == []
