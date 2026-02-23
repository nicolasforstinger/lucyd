"""Configuration loader for Lucyd daemon.

Loads lucyd.toml, applies environment variable overrides for secrets,
loads provider files from providers.d/, validates required fields,
and provides typed access to all settings.
Immutable after load — no runtime config reloading.
"""

import logging
import os
import time
import tomllib
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def today_start_ts() -> int:
    """Return Unix timestamp for start of today (local time)."""
    return int(time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d")))


class ConfigError(Exception):
    """Raised when configuration is invalid or missing."""
    pass


# Environment variable overrides for secrets
_ENV_OVERRIDES = {
    "LUCYD_ANTHROPIC_KEY": ("api_keys", "anthropic"),
    "LUCYD_OPENAI_KEY": ("api_keys", "openai"),
    "LUCYD_BRAVE_KEY": ("api_keys", "brave"),
    "LUCYD_ELEVENLABS_KEY": ("api_keys", "elevenlabs"),
    "LUCYD_HTTP_TOKEN": ("api_keys", "http_token"),
}


def _deep_get(d: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(key, default)
    return d


def _resolve_path(p: str) -> Path:
    return Path(p).expanduser().resolve()


class Config:
    """Immutable configuration loaded from lucyd.toml."""

    def __init__(self, data: dict, config_dir: Path | None = None):
        self._data = data
        self._config_dir = config_dir or Path.cwd()
        self._apply_env_overrides()
        self._load_providers()
        self._validate()

    def _apply_env_overrides(self):
        for env_var, key_path in _ENV_OVERRIDES.items():
            val = os.environ.get(env_var)
            if val:
                section, key = key_path
                if section not in self._data:
                    self._data[section] = {}
                self._data[section][key] = val

    def _load_providers(self):
        """Load provider files from providers.d/ directory.

        Each provider file defines connection details (type, api_key_env,
        base_url) and model sections. Models inherit provider-level settings.
        Files are loaded in the order specified by [providers] load list.
        """
        providers_cfg = self._data.get("providers", {})
        load_list = providers_cfg.get("load", [])
        if not load_list:
            return

        providers_dir = self._config_dir / providers_cfg.get("dir", "providers.d")
        if "models" not in self._data:
            self._data["models"] = {}

        for name in load_list:
            provider_file = providers_dir / f"{name}.toml"
            if not provider_file.exists():
                log.warning("Provider file not found: %s", provider_file)
                continue

            with open(provider_file, "rb") as f:
                pdata = tomllib.load(f)

            # Provider-level settings (inherited by all models in this file)
            provider_type = pdata.get("type", "")
            api_key_env = pdata.get("api_key_env", "")
            base_url = pdata.get("base_url", "")
            # Collect extra provider-level flags for model inheritance
            _reserved = {"type", "api_key_env", "base_url", "models"}
            extra_flags = {k: v for k, v in pdata.items()
                          if k not in _reserved and not isinstance(v, dict)}

            # Merge each model section into self._data["models"]
            for model_name, model_cfg in pdata.get("models", {}).items():
                if not isinstance(model_cfg, dict):
                    continue
                # Inject provider-level settings (model-level overrides win)
                model_cfg.setdefault("provider", provider_type)
                model_cfg.setdefault("api_key_env", api_key_env)
                if base_url:
                    model_cfg.setdefault("base_url", base_url)
                for k, v in extra_flags.items():
                    model_cfg.setdefault(k, v)
                self._data["models"][model_name] = model_cfg

            log.info("Loaded provider '%s': %s (%d models)",
                     name, provider_type,
                     len(pdata.get("models", {})))

    def _validate(self):
        errors = []
        if not _deep_get(self._data, "agent", "name"):
            errors.append("[agent] name is required")
        if not _deep_get(self._data, "agent", "workspace"):
            errors.append("[agent] workspace is required")
        if not _deep_get(self._data, "channel", "type"):
            errors.append("[channel] type is required")
        if not _deep_get(self._data, "models", "primary"):
            errors.append("[models.primary] section is required")
        primary = _deep_get(self._data, "models", "primary", default={})
        if not primary.get("provider"):
            errors.append("[models.primary] provider is required")
        if not primary.get("model"):
            errors.append("[models.primary] model is required")
        ch_type = _deep_get(self._data, "channel", "type")
        if ch_type == "telegram":
            tg = _deep_get(self._data, "channel", "telegram", default={})
            if not tg.get("token_env"):
                errors.append("[channel.telegram] token_env is required")
        if errors:
            raise ConfigError("Configuration errors:\n" + "\n".join(f"  - {e}" for e in errors))

    # --- Agent ---

    @property
    def agent_name(self) -> str:
        return self._data["agent"]["name"]

    @property
    def workspace(self) -> Path:
        return _resolve_path(self._data["agent"]["workspace"])

    @property
    def context_stable(self) -> list[str]:
        return _deep_get(self._data, "agent", "context", "stable", default=[])

    @property
    def context_semi_stable(self) -> list[str]:
        return _deep_get(self._data, "agent", "context", "semi_stable", default=[])

    @property
    def context_tiers(self) -> dict:
        return _deep_get(self._data, "agent", "context", "tiers", default={})

    @property
    def skills_dir(self) -> str:
        return _deep_get(self._data, "agent", "skills", "dir", default="skills")

    @property
    def always_on_skills(self) -> list[str]:
        return _deep_get(self._data, "agent", "skills", "always_on", default=[])

    # --- Channel ---

    @property
    def channel_type(self) -> str:
        return self._data["channel"]["type"]

    @property
    def debounce_ms(self) -> int:
        return _deep_get(self._data, "channel", "debounce_ms", default=500)

    @property
    def telegram_config(self) -> dict:
        return _deep_get(self._data, "channel", "telegram", default={})

    @property
    def contact_names(self) -> list[str]:
        """Contact names from channel config (for tool descriptions)."""
        contacts = _deep_get(
            self._data, "channel", self.channel_type, "contacts", default={},
        )
        return list(contacts.keys())

    # --- HTTP API ---

    @property
    def http_enabled(self) -> bool:
        return _deep_get(self._data, "http", "enabled", default=False)

    @property
    def http_host(self) -> str:
        return _deep_get(self._data, "http", "host", default="127.0.0.1")

    @property
    def http_port(self) -> int:
        return _deep_get(self._data, "http", "port", default=8100)

    @property
    def http_auth_token(self) -> str:
        return self.api_key("http_token")

    @property
    def http_download_dir(self) -> str:
        return _deep_get(self._data, "http", "download_dir", default="/tmp/lucyd-http")  # noqa: S108 — config default; overridden by lucyd.toml

    @property
    def http_max_body_bytes(self) -> int:
        return _deep_get(self._data, "http", "max_body_bytes", default=10 * 1024 * 1024)

    @property
    def http_callback_url(self) -> str:
        return _deep_get(self._data, "http", "callback_url", default="")

    @property
    def http_callback_token(self) -> str:
        env_var = _deep_get(self._data, "http", "callback_token_env", default="")
        return os.environ.get(env_var, "") if env_var else ""

    # --- Models ---

    def model_config(self, name: str) -> dict:
        cfg = _deep_get(self._data, "models", name, default={})
        if not cfg:
            raise ValueError(f"No model config for '{name}'")
        return cfg

    @property
    def all_model_names(self) -> list[str]:
        return list(_deep_get(self._data, "models", default={}).keys())

    # --- Routing ---

    def route_model(self, source: str) -> str:
        return _deep_get(self._data, "routing", source, default="primary")

    # --- Memory ---

    @property
    def memory_db(self) -> str:
        return _deep_get(self._data, "memory", "db", default="")

    @property
    def memory_top_k(self) -> int:
        return _deep_get(self._data, "memory", "search_top_k", default=10)

    # --- Memory Consolidation ---

    @property
    def consolidation_enabled(self) -> bool:
        return _deep_get(self._data, "memory", "consolidation", "enabled", default=False)

    @property
    def consolidation_fact_model(self) -> str:
        return _deep_get(self._data, "memory", "consolidation", "fact_model", default="subagent")

    @property
    def consolidation_episode_model(self) -> str:
        return _deep_get(self._data, "memory", "consolidation", "episode_model", default="primary")

    @property
    def consolidation_min_messages(self) -> int:
        return _deep_get(self._data, "memory", "consolidation", "min_messages", default=4)

    @property
    def consolidation_confidence_threshold(self) -> float:
        return _deep_get(self._data, "memory", "consolidation", "confidence_threshold", default=0.6)

    @property
    def consolidation_max_extraction_chars(self) -> int:
        return _deep_get(self._data, "memory", "consolidation", "max_extraction_chars", default=50000)

    # --- Memory Recall ---

    @property
    def recall_structured_first(self) -> bool:
        return _deep_get(self._data, "memory", "recall", "structured_first", default=True)

    @property
    def recall_decay_rate(self) -> float:
        return _deep_get(self._data, "memory", "recall", "decay_rate", default=0.03)

    @property
    def recall_max_facts(self) -> int:
        return _deep_get(self._data, "memory", "recall", "max_facts_in_context", default=20)

    @property
    def recall_max_dynamic_tokens(self) -> int:
        return _deep_get(self._data, "memory", "recall", "max_dynamic_tokens", default=1500)

    @property
    def recall_max_episodes_at_start(self) -> int:
        return _deep_get(self._data, "memory", "recall", "max_episodes_at_start", default=3)

    # --- Memory Recall Personality ---

    @property
    def recall_priority_vector(self) -> int:
        return _deep_get(self._data, "memory", "recall", "personality", "priority_vector", default=35)

    @property
    def recall_priority_episodes(self) -> int:
        return _deep_get(self._data, "memory", "recall", "personality", "priority_episodes", default=25)

    @property
    def recall_priority_facts(self) -> int:
        return _deep_get(self._data, "memory", "recall", "personality", "priority_facts", default=15)

    @property
    def recall_priority_commitments(self) -> int:
        return _deep_get(self._data, "memory", "recall", "personality", "priority_commitments", default=40)

    @property
    def recall_fact_format(self) -> str:
        return _deep_get(self._data, "memory", "recall", "personality", "fact_format", default="natural")

    @property
    def recall_show_emotional_tone(self) -> bool:
        return _deep_get(self._data, "memory", "recall", "personality", "show_emotional_tone", default=True)

    @property
    def recall_episode_section_header(self) -> str:
        return _deep_get(self._data, "memory", "recall", "personality", "episode_section_header", default="Recent conversations")

    # --- Memory Maintenance ---

    @property
    def maintenance_enabled(self) -> bool:
        return _deep_get(self._data, "memory", "maintenance", "enabled", default=False)

    @property
    def maintenance_stale_threshold_days(self) -> int:
        return _deep_get(self._data, "memory", "maintenance", "stale_threshold_days", default=90)

    # --- Memory Indexer ---

    @property
    def indexer_include_patterns(self) -> list[str]:
        return _deep_get(self._data, "memory", "indexer", "include_patterns",
                         default=["memory/*.md", "MEMORY.md"])

    @property
    def indexer_exclude_dirs(self) -> list[str]:
        return _deep_get(self._data, "memory", "indexer", "exclude_dirs", default=[])

    # --- Tools ---

    @property
    def config_dir(self) -> Path:
        """Directory containing lucyd.toml (for resolving relative paths)."""
        return self._config_dir

    @property
    def plugins_dir(self) -> str:
        return _deep_get(self._data, "tools", "plugins_dir", default="plugins.d")

    @property
    def subagent_deny(self) -> list[str] | None:
        """Custom sub-agent deny list, or None to use hardcoded default."""
        return _deep_get(self._data, "tools", "subagent_deny", default=None)

    @property
    def tools_enabled(self) -> list[str]:
        return _deep_get(self._data, "tools", "enabled", default=[
            "read", "write", "edit", "exec",
        ])

    @property
    def output_truncation(self) -> int:
        return _deep_get(self._data, "tools", "output_truncation", default=30000)

    @property
    def filesystem_allowed_paths(self) -> list[str]:
        explicit = _deep_get(self._data, "tools", "filesystem", "allowed_paths", default=None)
        if explicit is not None:
            return [str(_resolve_path(p)) for p in explicit]
        # Default: workspace dir + /tmp/
        ws = str(self.workspace)
        return [ws, "/tmp/"]  # noqa: S108 — allowed read paths; /tmp needed for TTS and Telegram downloads

    @property
    def exec_timeout(self) -> int:
        return _deep_get(self._data, "tools", "exec_timeout", default=120)

    @property
    def exec_max_timeout(self) -> int:
        return _deep_get(self._data, "tools", "exec_max_timeout", default=600)

    @property
    def web_search_provider(self) -> str:
        return _deep_get(self._data, "tools", "web_search", "provider", default="brave")

    @property
    def tts_provider(self) -> str:
        return _deep_get(self._data, "tools", "tts", "provider", default="elevenlabs")

    # --- STT (Speech-to-Text) ---

    @property
    def stt_backend(self) -> str:
        return _deep_get(self._data, "stt", "backend", default="openai")

    @property
    def stt_voice_label(self) -> str:
        return _deep_get(self._data, "stt", "voice_label", default="voice message")

    @property
    def stt_voice_fail_msg(self) -> str:
        return _deep_get(self._data, "stt", "voice_fail_msg",
                         default="voice message — transcription failed")

    @property
    def stt_local_endpoint(self) -> str:
        return _deep_get(self._data, "stt", "local", "endpoint",
                         default="http://whisper-server:8082/inference")

    @property
    def stt_local_language(self) -> str:
        return _deep_get(self._data, "stt", "local", "language", default="auto")

    @property
    def stt_local_ffmpeg_timeout(self) -> int:
        return _deep_get(self._data, "stt", "local", "ffmpeg_timeout", default=30)

    @property
    def stt_local_request_timeout(self) -> int:
        return _deep_get(self._data, "stt", "local", "request_timeout", default=60)

    @property
    def stt_openai_api_url(self) -> str:
        return _deep_get(self._data, "stt", "openai", "api_url",
                         default="https://api.openai.com/v1/audio/transcriptions")

    @property
    def stt_openai_model(self) -> str:
        return _deep_get(self._data, "stt", "openai", "model", default="whisper-1")

    @property
    def stt_openai_timeout(self) -> int:
        return _deep_get(self._data, "stt", "openai", "timeout", default=60)

    # --- Documents ---

    @property
    def documents_enabled(self) -> bool:
        return _deep_get(self._data, "documents", "enabled", default=True)

    @property
    def documents_max_chars(self) -> int:
        return _deep_get(self._data, "documents", "max_chars", default=30000)

    @property
    def documents_max_file_bytes(self) -> int:
        return _deep_get(self._data, "documents", "max_file_bytes", default=10 * 1024 * 1024)

    @property
    def documents_text_extensions(self) -> list[str]:
        return _deep_get(self._data, "documents", "text_extensions",
                         default=[".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml",
                                  ".html", ".htm", ".py", ".js", ".ts", ".sh", ".toml",
                                  ".ini", ".cfg", ".log", ".sql", ".css"])

    # --- Vision ---

    @property
    def vision_max_image_bytes(self) -> int:
        return _deep_get(self._data, "vision", "max_image_bytes", default=5 * 1024 * 1024)

    @property
    def vision_default_caption(self) -> str:
        return _deep_get(self._data, "vision", "default_caption", default="image")

    @property
    def vision_too_large_msg(self) -> str:
        return _deep_get(self._data, "vision", "too_large_msg",
                         default="image too large to display")

    # --- Behavior ---

    @property
    def silent_tokens(self) -> list[str]:
        return _deep_get(self._data, "behavior", "silent_tokens",
                         default=["HEARTBEAT_OK", "NO_REPLY"])

    @property
    def typing_indicators(self) -> bool:
        return _deep_get(self._data, "behavior", "typing_indicators", default=True)

    @property
    def error_message(self) -> str:
        return _deep_get(self._data, "behavior", "error_message",
                         default="I'm having trouble connecting right now. Try again in a moment.")

    @property
    def api_retries(self) -> int:
        return _deep_get(self._data, "behavior", "api_retries", default=2)

    @property
    def api_retry_base_delay(self) -> float:
        return float(_deep_get(self._data, "behavior", "api_retry_base_delay", default=2.0))

    @property
    def agent_timeout(self) -> float:
        return float(_deep_get(self._data, "behavior", "agent_timeout_seconds", default=600))

    @property
    def max_turns(self) -> int:
        return _deep_get(self._data, "behavior", "max_turns_per_message", default=50)

    @property
    def compaction_threshold(self) -> int:
        return _deep_get(self._data, "behavior", "compaction", "threshold_tokens", default=150000)

    @property
    def compaction_model(self) -> str:
        return _deep_get(self._data, "behavior", "compaction", "model", default="compaction")

    @property
    def compaction_prompt(self) -> str:
        return _deep_get(self._data, "behavior", "compaction", "prompt",
                         default="Summarize this conversation preserving all factual details, decisions, action items, and emotional context.")

    # --- Paths ---

    @property
    def state_dir(self) -> Path:
        return _resolve_path(_deep_get(self._data, "paths", "state_dir", default="~/.lucyd"))

    @property
    def sessions_dir(self) -> Path:
        return _resolve_path(_deep_get(self._data, "paths", "sessions_dir",
                                       default="~/.lucyd/sessions"))

    @property
    def cost_db(self) -> Path:
        return _resolve_path(_deep_get(self._data, "paths", "cost_db",
                                       default="~/.lucyd/cost.db"))

    @property
    def log_file(self) -> Path:
        return _resolve_path(_deep_get(self._data, "paths", "log_file",
                                       default="~/.lucyd/lucyd.log"))

    # --- API Keys ---

    def api_key(self, provider: str) -> str:
        return _deep_get(self._data, "api_keys", provider, default="")

    # --- Raw access ---

    def raw(self, *keys: str, default: Any = None) -> Any:
        return _deep_get(self._data, *keys, default=default)


def _load_dotenv(toml_path: Path) -> None:
    """Load .env file from same directory as lucyd.toml if it exists."""
    env_file = toml_path.parent / ".env"
    if not env_file.exists():
        return
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            # Only set if not already in environment (env takes precedence)
            if key not in os.environ:
                os.environ[key] = val


def load_config(path: str | Path, overrides: dict | None = None) -> Config:
    """Load and validate config from a TOML file.

    Args:
        path: Path to lucyd.toml config file.
        overrides: Dict of overrides to apply to raw TOML data before
                   constructing Config (e.g. CLI args).
    """
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise ConfigError(f"Config file not found: {p}")
    _load_dotenv(p)
    with open(p, "rb") as f:
        data = tomllib.load(f)
    # Apply overrides before validation
    if overrides:
        for key_path, value in overrides.items():
            keys = key_path.split(".")
            d = data
            for k in keys[:-1]:
                d = d.setdefault(k, {})
            d[keys[-1]] = value
    return Config(data, config_dir=p.parent)
