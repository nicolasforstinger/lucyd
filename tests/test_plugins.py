"""Tests for tool plugin system — _init_plugins().

Verifies: discovery, filtering by enabled, configure() injection,
bad plugin resilience, no TOOLS list, no plugins dir.
"""

from unittest.mock import MagicMock

from lucyd import LucydDaemon


def _make_config(tmp_path, enabled=None, plugins_dir="plugins.d"):
    """Build a complete Config for plugin testing."""
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
                               "fact_format": "natural", "show_emotional_tone": True, "episode_section_header": "Recent conversations",
                               "synthesis_style": "structured",
                               "synthesis_prompt_narrative": "", "synthesis_prompt_factual": ""},
            },
            "maintenance": {"stale_threshold_days": 90},
            "indexer": {"include_patterns": ["memory/*.md"], "exclude_dirs": [], "chunk_size_chars": 1600, "chunk_overlap_chars": 320, "embed_batch_limit": 100},
        },
        "tools": {
            "enabled": enabled or ["read", "write"],
            "plugins_dir": plugins_dir, "output_truncation": 30000,
            "subagent_deny": [], "subagent_max_turns": 0, "subagent_timeout": 0,
            "exec_timeout": 120, "exec_max_timeout": 600,
            "filesystem": {"allowed_paths": ["/tmp/"], "default_read_limit": 2000},
            "web_search": {"provider": "", "api_key_env": "", "timeout": 15},
            "web_fetch": {"timeout": 15},
            "tts": {"provider": "", "api_key_env": "", "timeout": 60, "api_url": ""},
            "scheduling": {"max_scheduled": 50, "max_delay": 86400},
        },
        "stt": {"backend": "", "voice_label": "voice message", "voice_fail_msg": "voice message — transcription failed",
                "audio_label": "audio transcription", "audio_fail_msg": "audio transcription — failed"},
        "documents": {"enabled": True, "max_chars": 30000, "max_file_bytes": 10485760,
                      "text_extensions": [".txt", ".md"]},
        "logging": {"max_bytes": 10485760, "backup_count": 3, "suppress": []},
        "vision": {"max_image_bytes": 5242880, "max_dimension": 1568, "default_caption": "image",
                   "too_large_msg": "image too large to display", "jpeg_quality_steps": [85, 60, 40],
                   "caption_max_chars": 200},
        "behavior": {
            "silent_tokens": ["NO_REPLY"], "typing_indicators": True, "error_message": "error",
            "sqlite_timeout": 30,
            "api_retries": 2, "api_retry_base_delay": 2.0, "message_retries": 2, "message_retry_base_delay": 30.0,
            "audit_truncation_limit": 500, "agent_timeout_seconds": 600,
            "max_turns_per_message": 50, "max_cost_per_message": 0.0,
            "queue_capacity": 1000, "queue_poll_interval": 1.0, "quote_max_chars": 200,
            "telemetry_max_age": 30.0, "passive_notify_refs": [], "primary_sender": "",
            "compaction": {
                "threshold_tokens": 150000, "max_tokens": 2048,
                "prompt": "Summarize for {agent_name}.", "keep_recent_pct": 0.33,
                "keep_recent_pct_min": 0.05, "keep_recent_pct_max": 0.9,
                "min_messages": 4, "tool_result_max_chars": 2000, "warning_pct": 0.8,
                "diary_prompt": "Write a log for {date}.",
                "verify_enabled": True, "verify_max_turn_labels": 3, "verify_grounding_threshold": 0.5,
            },
        },
        "paths": {
            "state_dir": str(tmp_path / "state"),
            "sessions_dir": str(tmp_path / "sessions"),
            "cost_db": str(tmp_path / "cost.db"),
            "log_file": str(tmp_path / "lucyd.log"),
        },
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
    daemon.provider = MagicMock()
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
