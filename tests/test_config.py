"""Tests for config.py — TOML loading, validation, property access."""

import pytest

from config import Config, ConfigError, load_config

MINIMAL_TOML = """\
[agent]
name = "TestAgent"
workspace = "/tmp/test-workspace"

[channel]
type = "cli"

[models.primary]
provider = "anthropic-compat"
model = "claude-haiku-4-5-20251001"
"""


class TestValidConfig:
    def test_loads_primary_and_embeddings(self, minimal_toml_data):
        """Primary and embeddings model sections parse."""
        cfg = Config(minimal_toml_data)
        assert cfg.model_config("primary")["model"] == "claude-opus-4-6"
        assert "embeddings" in cfg._data.get("models", {})

    def test_model_cost_per_mtok_is_three_elements(self, minimal_toml_data):
        """cost_per_mtok must be [input, output, cache_read]."""
        cfg = Config(minimal_toml_data)
        primary = cfg.model_config("primary")
        rates = primary["cost_per_mtok"]
        assert isinstance(rates, list)
        assert len(rates) == 3
        assert rates == [5.0, 25.0, 0.5]

    def test_thinking_config_on_primary(self, minimal_toml_data):
        """Primary model has thinking_enabled and thinking_budget."""
        cfg = Config(minimal_toml_data)
        primary = cfg.model_config("primary")
        assert primary["thinking_enabled"] is True
        assert primary["thinking_budget"] == 10000


class TestMissingFields:
    def test_missing_agent_name_raises(self):
        """Missing [agent] name should raise ConfigError."""
        data = {
            "agent": {"workspace": "/tmp"},
            "channel": {"type": "telegram", "telegram": {"token_env": "LUCYD_TELEGRAM_TOKEN"}},
            "models": {"primary": {"provider": "anthropic-compat", "model": "x"}},
        }
        with pytest.raises(ConfigError, match="name"):
            Config(data)

    def test_missing_primary_model_raises(self):
        """Missing [models.primary] should raise ConfigError."""
        data = {
            "agent": {"name": "Test", "workspace": "/tmp"},
            "channel": {"type": "telegram", "telegram": {"token_env": "LUCYD_TELEGRAM_TOKEN"}},
            "models": {},
        }
        with pytest.raises(ConfigError, match="primary"):
            Config(data)

    def test_missing_channel_type_raises(self):
        data = {
            "agent": {"name": "Test", "workspace": "/tmp"},
            "channel": {},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
        }
        with pytest.raises(ConfigError, match="channel.*type"):
            Config(data)

    def test_telegram_token_env_validated_by_channel(self):
        """Telegram token_env validation lives in TelegramChannel, not Config."""
        from channels.telegram import TelegramChannel
        with pytest.raises(ConfigError, match="token_env"):
            TelegramChannel({})


class TestToolFiltering:
    def test_tools_enabled_list(self, minimal_toml_data):
        """Only tools in enabled list are returned."""
        cfg = Config(minimal_toml_data)
        enabled = cfg.tools_enabled
        assert "read" in enabled
        assert "write" in enabled
        assert "message" in enabled
        # Not in our minimal list
        assert "web_search" not in enabled


class TestModelConfig:
    def test_model_config_missing_raises(self):
        cfg = Config({
            "agent": {"name": "Test", "workspace": "/tmp/test"},
            "channel": {"type": "cli"},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
        })
        with pytest.raises(ValueError, match="nonexistent"):
            cfg.model_config("nonexistent")


class TestCompactionMaxTokens:
    def test_default(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.compaction_max_tokens == 2048

    def test_override(self, minimal_toml_data):
        minimal_toml_data.setdefault("behavior", {}).setdefault("compaction", {})["max_tokens"] = 4096
        cfg = Config(minimal_toml_data)
        assert cfg.compaction_max_tokens == 4096


class TestFilesystemAllowedPaths:
    def test_reads_allowed_paths_from_config(self, minimal_toml_data):
        """allowed_paths are read from config and resolved."""
        cfg = Config(minimal_toml_data)
        paths = cfg.filesystem_allowed_paths
        assert any("/tmp" in p for p in paths)

    def test_explicit_overrides_default(self, minimal_toml_data):
        """Explicit allowed_paths replaces the default."""
        minimal_toml_data["tools"]["filesystem"] = {"allowed_paths": ["/data/"]}
        cfg = Config(minimal_toml_data)
        paths = cfg.filesystem_allowed_paths
        assert len(paths) == 1
        assert paths[0].startswith("/data")


class TestWebSearchApiKey:
    def test_web_search_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("MY_BRAVE_KEY", "sk-brave-123")
        cfg = Config({
            "agent": {"name": "Test", "workspace": "/tmp/test"},
            "channel": {"type": "cli"},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
            "tools": {"web_search": {"api_key_env": "MY_BRAVE_KEY"}},
        })
        assert cfg.web_search_api_key == "sk-brave-123"

    def test_web_search_api_key_missing_env(self):
        cfg = Config({
            "agent": {"name": "Test", "workspace": "/tmp/test"},
            "channel": {"type": "cli"},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
            "tools": {"web_search": {"api_key_env": "NONEXISTENT_KEY"}},
        })
        assert cfg.web_search_api_key == ""

    def test_web_search_api_key_empty_when_no_env_var(self, minimal_toml_data):
        """Empty api_key_env returns empty string."""
        cfg = Config(minimal_toml_data)
        assert cfg.web_search_api_key == ""


class TestLoadConfig:
    def test_load_valid(self, tmp_path):
        toml_file = tmp_path / "test.toml"
        toml_file.write_text(MINIMAL_TOML)
        cfg = load_config(str(toml_file))
        assert cfg.agent_name == "TestAgent"

    def test_load_missing_file(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            load_config(str(tmp_path / "nonexistent.toml"))

    def test_load_with_overrides(self, tmp_path):
        toml_file = tmp_path / "test.toml"
        toml_file.write_text(MINIMAL_TOML)
        cfg = load_config(str(toml_file), overrides={"agent.name": "Override"})
        assert cfg.agent_name == "Override"


# ─── today_start_ts ──────────────────────────────────────────────


class TestTodayStartTs:
    """QUALITY-1: Shared timestamp calculation utility."""

    def test_returns_midnight_timestamp(self):
        """today_start_ts returns a timestamp at midnight."""
        import time

        from config import today_start_ts
        ts = today_start_ts()
        # Convert back and check it's midnight
        t = time.localtime(ts)
        assert t.tm_hour == 0
        assert t.tm_min == 0
        assert t.tm_sec == 0

    def test_returns_today(self):
        """Returned timestamp is for today, not some other day."""
        import time

        from config import today_start_ts
        ts = today_start_ts()
        today = time.strftime("%Y-%m-%d")
        ts_day = time.strftime("%Y-%m-%d", time.localtime(ts))
        assert ts_day == today


# ─── Vision Config ────────────────────────────────────────────────


class TestSTTConfig:
    """STT generic properties — backend-specific config read via raw()."""

    def test_values_from_config(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.stt_backend == ""
        assert cfg.stt_voice_label == "voice message"
        assert isinstance(cfg.stt_voice_fail_msg, str)

    def test_stt_raw_config(self, minimal_toml_data):
        minimal_toml_data["stt"] = {
            "backend": "openai",
            "api_key_env": "MY_STT_KEY",
            "openai": {"model": "whisper-large-v3"},
        }
        cfg = Config(minimal_toml_data)
        assert cfg.stt_backend == "openai"
        raw = cfg.raw("stt", default={})
        assert raw["openai"]["model"] == "whisper-large-v3"
        assert raw["api_key_env"] == "MY_STT_KEY"


class TestPluginConfig:
    """Plugin system config properties."""

    def test_plugins_dir_default(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.plugins_dir == "plugins.d"

    def test_plugins_dir_override(self, minimal_toml_data):
        minimal_toml_data["tools"]["plugins_dir"] = "my_plugins"
        cfg = Config(minimal_toml_data)
        assert cfg.plugins_dir == "my_plugins"

    def test_subagent_deny_default_empty(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.subagent_deny == []

    def test_subagent_deny_custom(self, minimal_toml_data):
        minimal_toml_data["tools"]["subagent_deny"] = ["sessions_spawn", "tts"]
        cfg = Config(minimal_toml_data)
        assert cfg.subagent_deny == ["sessions_spawn", "tts"]

    def test_subagent_deny_empty_list(self, minimal_toml_data):
        minimal_toml_data["tools"]["subagent_deny"] = []
        cfg = Config(minimal_toml_data)
        assert cfg.subagent_deny == []

    def test_subagent_max_turns_default(self, minimal_toml_data):
        """0 (default) resolves to parent's max_turns_per_message."""
        cfg = Config(minimal_toml_data)
        assert cfg.subagent_max_turns == cfg.max_turns

    def test_subagent_max_turns_override(self, minimal_toml_data):
        minimal_toml_data["tools"]["subagent_max_turns"] = 25
        cfg = Config(minimal_toml_data)
        assert cfg.subagent_max_turns == 25

    def test_subagent_timeout_default(self, minimal_toml_data):
        """0 (default) resolves to parent's agent_timeout."""
        cfg = Config(minimal_toml_data)
        assert cfg.subagent_timeout == cfg.agent_timeout

    def test_subagent_timeout_override(self, minimal_toml_data):
        minimal_toml_data["tools"]["subagent_timeout"] = 300.0
        cfg = Config(minimal_toml_data)
        assert cfg.subagent_timeout == 300.0

    def test_config_dir_exposed(self, minimal_toml_data):
        from pathlib import Path
        cfg = Config(minimal_toml_data, config_dir=Path("/some/dir"))
        assert cfg.config_dir == Path("/some/dir")

    def test_config_dir_defaults_to_cwd(self, minimal_toml_data):
        from pathlib import Path
        cfg = Config(minimal_toml_data)
        assert cfg.config_dir == Path.cwd()


class TestVisionConfig:
    """Vision section defaults and overrides."""

    def test_values_from_config(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.vision_max_image_bytes == 5242880
        assert cfg.vision_max_dimension == 1568
        assert cfg.vision_default_caption == "image"
        assert cfg.vision_too_large_msg == "image too large to display"

    def test_overrides(self, minimal_toml_data):
        minimal_toml_data["vision"] = {
            "max_image_bytes": 5242880,
            "max_dimension": 768,
            "default_caption": "Foto vom Kunden",
            "too_large_msg": "Zu groß.",
        }
        cfg = Config(minimal_toml_data)
        assert cfg.vision_max_image_bytes == 5242880
        assert cfg.vision_max_dimension == 768
        assert cfg.vision_default_caption == "Foto vom Kunden"
        assert cfg.vision_too_large_msg == "Zu groß."

    def test_jpeg_quality_steps_default(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.vision_jpeg_quality_steps == [85, 60, 40]

    def test_jpeg_quality_steps_override(self, minimal_toml_data):
        minimal_toml_data.setdefault("vision", {})["jpeg_quality_steps"] = [90, 70]
        cfg = Config(minimal_toml_data)
        assert cfg.vision_jpeg_quality_steps == [90, 70]


class TestEmbeddingConfig:
    """Embedding config — provider-agnostic, model system as source of truth."""

    def test_embedding_model_from_model_system(self, minimal_toml_data):
        """When [models.embeddings] exists, reads model from there."""
        cfg = Config(minimal_toml_data)
        assert cfg.embedding_model == "text-embedding-3-small"

    def test_embedding_base_url_from_model_system(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.embedding_base_url == "https://api.openai.com/v1"

    def test_embedding_provider_from_model_system(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.embedding_provider == "openai-compat"

    def test_embedding_model_empty_when_no_models_section(self, minimal_toml_data):
        del minimal_toml_data["models"]["embeddings"]
        cfg = Config(minimal_toml_data)
        assert cfg.embedding_model == ""

    def test_embedding_base_url_empty_when_no_models_section(self, minimal_toml_data):
        del minimal_toml_data["models"]["embeddings"]
        cfg = Config(minimal_toml_data)
        assert cfg.embedding_base_url == ""

    def test_embedding_fallback_to_memory_section(self, minimal_toml_data):
        del minimal_toml_data["models"]["embeddings"]
        minimal_toml_data["memory"] = {"embedding_model": "local-embed"}
        cfg = Config(minimal_toml_data)
        assert cfg.embedding_model == "local-embed"

    def test_embedding_api_key_from_env(self, minimal_toml_data, monkeypatch):
        minimal_toml_data["models"]["embeddings"]["api_key_env"] = "TEST_EMBED_KEY"
        monkeypatch.setenv("TEST_EMBED_KEY", "sk-test-123")
        cfg = Config(minimal_toml_data)
        assert cfg.embedding_api_key == "sk-test-123"

    def test_embedding_api_key_empty_when_no_env(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.embedding_api_key == ""

    def test_embedding_timeout_default(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.embedding_timeout == 15

    def test_embedding_timeout_override(self, minimal_toml_data):
        minimal_toml_data.setdefault("memory", {})["embedding_timeout"] = 30
        cfg = Config(minimal_toml_data)
        assert cfg.embedding_timeout == 30


class TestTtsApiKey:
    """TTS API key resolution — provider-agnostic."""

    def test_tts_provider_default_empty(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.tts_provider == ""

    def test_tts_provider_from_config(self, minimal_toml_data):
        minimal_toml_data["tools"]["tts"]["provider"] = "elevenlabs"
        cfg = Config(minimal_toml_data)
        assert cfg.tts_provider == "elevenlabs"

    def test_tts_api_key_from_explicit_env(self, minimal_toml_data, monkeypatch):
        minimal_toml_data["tools"]["tts"]["api_key_env"] = "MY_TTS_KEY"
        monkeypatch.setenv("MY_TTS_KEY", "sk-custom-tts")
        cfg = Config(minimal_toml_data)
        assert cfg.tts_api_key == "sk-custom-tts"

    def test_tts_api_key_empty_when_no_api_key_env(self, minimal_toml_data):
        minimal_toml_data["tools"]["tts"]["provider"] = "elevenlabs"
        minimal_toml_data["tools"]["tts"]["api_key_env"] = ""
        cfg = Config(minimal_toml_data)
        assert cfg.tts_api_key == ""

    def test_tts_api_key_empty_when_no_config(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.tts_api_key == ""


class TestSttBackendDefault:
    """STT backend default is empty (provider-agnostic)."""

    def test_stt_backend_default_empty(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.stt_backend == ""

    def test_stt_backend_from_config(self, minimal_toml_data):
        minimal_toml_data["stt"] = {"backend": "local"}
        cfg = Config(minimal_toml_data)
        assert cfg.stt_backend == "local"


class TestIndexerConfig:
    """Memory indexer config defaults and overrides."""

    def test_defaults(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.indexer_chunk_size == 1600
        assert cfg.indexer_chunk_overlap == 320
        assert cfg.indexer_embed_batch_limit == 100

    def test_overrides(self, minimal_toml_data):
        minimal_toml_data.setdefault("memory", {})["indexer"] = {
            "chunk_size_chars": 800,
            "chunk_overlap_chars": 200,
            "embed_batch_limit": 50,
        }
        cfg = Config(minimal_toml_data)
        assert cfg.indexer_chunk_size == 800
        assert cfg.indexer_chunk_overlap == 200
        assert cfg.indexer_embed_batch_limit == 50


class TestHttpConfig:
    """HTTP section config — callback timeout, rate limits."""

    def test_defaults(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.http_callback_timeout == 10
        assert cfg.http_rate_limit == 30
        assert cfg.http_rate_window == 60
        assert cfg.http_status_rate_limit == 60

    def test_overrides(self, minimal_toml_data):
        minimal_toml_data["http"] = {
            "callback_timeout": 5,
            "rate_limit": 100,
            "rate_window": 120,
            "status_rate_limit": 200,
        }
        cfg = Config(minimal_toml_data)
        assert cfg.http_callback_timeout == 5
        assert cfg.http_rate_limit == 100
        assert cfg.http_rate_window == 120
        assert cfg.http_status_rate_limit == 200


class TestLoggingConfig:
    """Logging section defaults and overrides."""

    def test_defaults(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.log_max_bytes == 10 * 1024 * 1024
        assert cfg.log_backup_count == 3

    def test_overrides(self, minimal_toml_data):
        minimal_toml_data["logging"] = {
            "max_bytes": 5_000_000,
            "backup_count": 5,
        }
        cfg = Config(minimal_toml_data)
        assert cfg.log_max_bytes == 5_000_000
        assert cfg.log_backup_count == 5


class TestBehaviorAuditTruncation:
    """Audit truncation limit config."""

    def test_default(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.audit_truncation_limit == 500

    def test_override(self, minimal_toml_data):
        minimal_toml_data.setdefault("behavior", {})["audit_truncation_limit"] = 1000
        cfg = Config(minimal_toml_data)
        assert cfg.audit_truncation_limit == 1000


class TestPrimarySender:
    """Primary sender config for notification routing."""

    def test_default_empty(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.primary_sender == ""

    def test_from_config(self, minimal_toml_data):
        minimal_toml_data.setdefault("behavior", {})["primary_sender"] = "Nicolas"
        cfg = Config(minimal_toml_data)
        assert cfg.primary_sender == "Nicolas"


class TestPassiveNotifyRefs:
    """Passive notification refs for telemetry buffering."""

    def test_default_empty(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.passive_notify_refs == []

    def test_from_config(self, minimal_toml_data):
        minimal_toml_data.setdefault("behavior", {})["passive_notify_refs"] = ["hr-telemetry"]
        cfg = Config(minimal_toml_data)
        assert cfg.passive_notify_refs == ["hr-telemetry"]

    def test_multiple_refs(self, minimal_toml_data):
        minimal_toml_data.setdefault("behavior", {})["passive_notify_refs"] = [
            "hr-telemetry", "temperature",
        ]
        cfg = Config(minimal_toml_data)
        assert cfg.passive_notify_refs == ["hr-telemetry", "temperature"]


class TestWebTimeouts:
    """Web search/fetch timeout config."""

    def test_defaults(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.web_search_timeout == 15
        assert cfg.web_fetch_timeout == 15

    def test_overrides(self, minimal_toml_data):
        minimal_toml_data.setdefault("tools", {})["web_search"] = {"timeout": 30}
        minimal_toml_data["tools"]["web_fetch"] = {"timeout": 25}
        cfg = Config(minimal_toml_data)
        assert cfg.web_search_timeout == 30
        assert cfg.web_fetch_timeout == 25


class TestTtsConfig:
    """TTS timeout and API URL config — provider-agnostic."""

    def test_defaults(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.tts_timeout == 60
        assert cfg.tts_api_url == ""

    def test_overrides(self, minimal_toml_data):
        minimal_toml_data.setdefault("tools", {})["tts"] = {
            "timeout": 30,
            "api_url": "https://custom-tts.example.com/v1/{voice_id}",
        }
        cfg = Config(minimal_toml_data)
        assert cfg.tts_timeout == 30
        assert cfg.tts_api_url == "https://custom-tts.example.com/v1/{voice_id}"


class TestSchedulingConfig:
    """Scheduling tool config."""

    def test_defaults(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.scheduling_max_scheduled == 50
        assert cfg.scheduling_max_delay == 86400

    def test_overrides(self, minimal_toml_data):
        minimal_toml_data.setdefault("tools", {})["scheduling"] = {
            "max_scheduled": 100,
            "max_delay": 172800,
        }
        cfg = Config(minimal_toml_data)
        assert cfg.scheduling_max_scheduled == 100
        assert cfg.scheduling_max_delay == 172800


class TestFilesystemConfig:
    """Filesystem tool config."""

    def test_default(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.filesystem_default_read_limit == 2000

    def test_override(self, minimal_toml_data):
        minimal_toml_data.setdefault("tools", {})["filesystem"] = {
            "default_read_limit": 500,
        }
        cfg = Config(minimal_toml_data)
        assert cfg.filesystem_default_read_limit == 500


class TestChannelRawConfig:
    """Channel config is accessed via raw() — no typed properties."""

    def test_telegram_raw_config(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        tg = cfg.raw("channel", "telegram", default={})
        assert isinstance(tg, dict)
        assert tg.get("token_env") == "LUCYD_TELEGRAM_TOKEN"

    def test_channel_type(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.channel_type == "telegram"


class TestCompactionVerificationConfig:
    """Compaction verification config properties."""

    def test_verify_enabled_default(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.verify_enabled is True

    def test_verify_enabled_override(self, minimal_toml_data):
        minimal_toml_data.setdefault("behavior", {}).setdefault("compaction", {})["verify_enabled"] = False
        cfg = Config(minimal_toml_data)
        assert cfg.verify_enabled is False

    def test_verify_max_turn_labels_default(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.verify_max_turn_labels == 3

    def test_verify_max_turn_labels_override(self, minimal_toml_data):
        minimal_toml_data.setdefault("behavior", {}).setdefault("compaction", {})["verify_max_turn_labels"] = 5
        cfg = Config(minimal_toml_data)
        assert cfg.verify_max_turn_labels == 5

    def test_verify_grounding_threshold_default(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.verify_grounding_threshold == 0.5

    def test_verify_grounding_threshold_override(self, minimal_toml_data):
        minimal_toml_data.setdefault("behavior", {}).setdefault("compaction", {})["verify_grounding_threshold"] = 0.7
        cfg = Config(minimal_toml_data)
        assert cfg.verify_grounding_threshold == 0.7

    def test_compaction_prompt_includes_agent_name_placeholder(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert "{agent_name}" in cfg.compaction_prompt

    def test_compaction_prompt_includes_max_tokens_placeholder(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert "{max_tokens}" in cfg.compaction_prompt
