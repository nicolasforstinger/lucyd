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
    def test_loads_all_model_sections(self, minimal_toml_data):
        """All model sections (primary, subagent, compaction, embeddings) parse."""
        cfg = Config(minimal_toml_data)
        names = cfg.all_model_names
        assert "primary" in names
        assert "subagent" in names
        assert "compaction" in names
        assert "embeddings" in names

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

    def test_telegram_requires_token_env(self):
        data = {
            "agent": {"name": "Test", "workspace": "/tmp"},
            "channel": {"type": "telegram", "telegram": {}},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
        }
        with pytest.raises(ConfigError, match="token_env"):
            Config(data)


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


class TestRouteModel:
    def test_route_default(self):
        cfg = Config({
            "agent": {"name": "Test", "workspace": "/tmp/test"},
            "channel": {"type": "cli"},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
        })
        assert cfg.route_model("unknown_source") == "primary"

    def test_route_configured(self):
        cfg = Config({
            "agent": {"name": "Test", "workspace": "/tmp/test"},
            "channel": {"type": "cli"},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
            "routing": {"system": "subagent"},
        })
        assert cfg.route_model("system") == "subagent"


class TestFilesystemAllowedPaths:
    def test_default_includes_workspace_and_tmp(self, minimal_toml_data):
        """When no explicit allowed_paths, defaults to workspace + /tmp/."""
        cfg = Config(minimal_toml_data)
        paths = cfg.filesystem_allowed_paths
        assert "/tmp/" in paths or any("/tmp" in p for p in paths)
        assert any("test-workspace" in p for p in paths)

    def test_explicit_overrides_default(self, minimal_toml_data):
        """Explicit allowed_paths replaces the default."""
        minimal_toml_data["tools"]["filesystem"] = {"allowed_paths": ["/data/"]}
        cfg = Config(minimal_toml_data)
        paths = cfg.filesystem_allowed_paths
        assert len(paths) == 1
        assert paths[0].startswith("/data")


class TestEnvOverrides:
    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("LUCYD_ANTHROPIC_KEY", "sk-test-key")
        cfg = Config({
            "agent": {"name": "Test", "workspace": "/tmp/test"},
            "channel": {"type": "cli"},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
        })
        assert cfg.api_key("anthropic") == "sk-test-key"


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
    """STT section defaults and overrides."""

    def test_defaults_when_section_absent(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.stt_backend == ""
        assert cfg.stt_voice_label == "voice message"
        assert isinstance(cfg.stt_voice_fail_msg, str)
        assert cfg.stt_local_endpoint == "http://whisper-server:8082/inference"
        assert cfg.stt_local_language == "auto"
        assert cfg.stt_local_ffmpeg_timeout == 30
        assert cfg.stt_local_request_timeout == 60
        assert "openai.com" in cfg.stt_openai_api_url
        assert cfg.stt_openai_model == "whisper-1"
        assert cfg.stt_openai_timeout == 60

    def test_local_backend_overrides(self, minimal_toml_data):
        minimal_toml_data["stt"] = {
            "backend": "local",
            "voice_label": "Sprachnachricht",
            "local": {
                "endpoint": "http://localhost:9090/inference",
                "language": "de",
                "ffmpeg_timeout": 15,
                "request_timeout": 45,
            },
        }
        cfg = Config(minimal_toml_data)
        assert cfg.stt_backend == "local"
        assert cfg.stt_voice_label == "Sprachnachricht"
        assert cfg.stt_local_endpoint == "http://localhost:9090/inference"
        assert cfg.stt_local_language == "de"
        assert cfg.stt_local_ffmpeg_timeout == 15
        assert cfg.stt_local_request_timeout == 45

    def test_openai_backend_overrides(self, minimal_toml_data):
        minimal_toml_data["stt"] = {
            "backend": "openai",
            "openai": {
                "api_url": "https://custom.api/v1/transcriptions",
                "model": "whisper-large-v3",
                "timeout": 120,
            },
        }
        cfg = Config(minimal_toml_data)
        assert cfg.stt_openai_api_url == "https://custom.api/v1/transcriptions"
        assert cfg.stt_openai_model == "whisper-large-v3"
        assert cfg.stt_openai_timeout == 120


class TestPluginConfig:
    """Plugin system config properties."""

    def test_plugins_dir_default(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.plugins_dir == "plugins.d"

    def test_plugins_dir_override(self, minimal_toml_data):
        minimal_toml_data["tools"]["plugins_dir"] = "my_plugins"
        cfg = Config(minimal_toml_data)
        assert cfg.plugins_dir == "my_plugins"

    def test_subagent_deny_default_none(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.subagent_deny is None

    def test_subagent_deny_custom(self, minimal_toml_data):
        minimal_toml_data["tools"]["subagent_deny"] = ["sessions_spawn", "tts"]
        cfg = Config(minimal_toml_data)
        assert cfg.subagent_deny == ["sessions_spawn", "tts"]

    def test_subagent_deny_empty_list(self, minimal_toml_data):
        minimal_toml_data["tools"]["subagent_deny"] = []
        cfg = Config(minimal_toml_data)
        assert cfg.subagent_deny == []

    def test_subagent_model_default(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.subagent_model == "primary"

    def test_subagent_model_override(self, minimal_toml_data):
        minimal_toml_data["tools"]["subagent_model"] = "subagent"
        cfg = Config(minimal_toml_data)
        assert cfg.subagent_model == "subagent"

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

    def test_defaults_when_section_absent(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.vision_max_image_bytes == 5 * 1024 * 1024
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
        minimal_toml_data.setdefault("tools", {})["tts"] = {"provider": "elevenlabs"}
        cfg = Config(minimal_toml_data)
        assert cfg.tts_provider == "elevenlabs"

    def test_tts_api_key_from_explicit_env(self, minimal_toml_data, monkeypatch):
        minimal_toml_data.setdefault("tools", {})["tts"] = {"api_key_env": "MY_TTS_KEY"}
        monkeypatch.setenv("MY_TTS_KEY", "sk-custom-tts")
        cfg = Config(minimal_toml_data)
        assert cfg.tts_api_key == "sk-custom-tts"

    def test_tts_api_key_falls_back_to_provider_key(self, minimal_toml_data, monkeypatch):
        minimal_toml_data.setdefault("tools", {})["tts"] = {"provider": "elevenlabs"}
        monkeypatch.setenv("LUCYD_ELEVENLABS_KEY", "sk-el-key")
        cfg = Config(minimal_toml_data)
        assert cfg.tts_api_key == "sk-el-key"

    def test_tts_api_key_empty_when_no_provider(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.tts_api_key == ""

    def test_tts_api_key_explicit_env_overrides_provider(self, minimal_toml_data, monkeypatch):
        minimal_toml_data.setdefault("tools", {})["tts"] = {
            "provider": "elevenlabs", "api_key_env": "MY_TTS_KEY",
        }
        monkeypatch.setenv("LUCYD_ELEVENLABS_KEY", "sk-el-key")
        monkeypatch.setenv("MY_TTS_KEY", "sk-custom")
        cfg = Config(minimal_toml_data)
        assert cfg.tts_api_key == "sk-custom"


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


class TestTelegramReconnectConfig:
    """Telegram reconnect backoff config."""

    def test_defaults(self, minimal_toml_data):
        cfg = Config(minimal_toml_data)
        assert cfg.telegram_reconnect_initial == 1.0
        assert cfg.telegram_reconnect_max == 10.0
        assert cfg.telegram_reconnect_factor == 2.0
        assert cfg.telegram_reconnect_jitter == 0.2

    def test_overrides(self, minimal_toml_data):
        minimal_toml_data["channel"]["telegram"].update({
            "reconnect_initial": 2.0,
            "reconnect_max": 30.0,
            "reconnect_factor": 3.0,
            "reconnect_jitter": 0.5,
        })
        cfg = Config(minimal_toml_data)
        assert cfg.telegram_reconnect_initial == 2.0
        assert cfg.telegram_reconnect_max == 30.0
        assert cfg.telegram_reconnect_factor == 3.0
        assert cfg.telegram_reconnect_jitter == 0.5
