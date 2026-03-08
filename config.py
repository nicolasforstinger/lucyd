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
        self._load_providers()
        self._validate()

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

            with provider_file.open("rb") as f:
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
        env = _deep_get(self._data, "http", "token_env", default="")
        return os.environ.get(env, "") if env else ""

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

    @property
    def http_callback_timeout(self) -> int:
        return _deep_get(self._data, "http", "callback_timeout", default=10)

    @property
    def http_rate_limit(self) -> int:
        return _deep_get(self._data, "http", "rate_limit", default=30)

    @property
    def http_rate_window(self) -> int:
        return _deep_get(self._data, "http", "rate_window", default=60)

    @property
    def http_status_rate_limit(self) -> int:
        return _deep_get(self._data, "http", "status_rate_limit", default=60)

    # --- Models ---

    def model_config(self, name: str) -> dict:
        cfg = _deep_get(self._data, "models", name, default={})
        if not cfg:
            raise ValueError(f"No model config for '{name}'")
        return cfg

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

    @property
    def recall_synthesis_style(self) -> str:
        """Memory synthesis style: 'structured' (raw), 'narrative', or 'factual'."""
        return _deep_get(self._data, "memory", "recall", "personality", "synthesis_style", default="structured")

    # --- Memory Maintenance ---

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

    @property
    def indexer_chunk_size(self) -> int:
        return _deep_get(self._data, "memory", "indexer", "chunk_size_chars", default=1600)

    @property
    def indexer_chunk_overlap(self) -> int:
        return _deep_get(self._data, "memory", "indexer", "chunk_overlap_chars", default=320)

    @property
    def indexer_embed_batch_limit(self) -> int:
        return _deep_get(self._data, "memory", "indexer", "embed_batch_limit", default=100)

    # --- Embedding (Provider-Agnostic) ---

    @property
    def embedding_model(self) -> str:
        """Read from [models.embeddings] (provider file) or [memory] override. Empty = not configured."""
        if "embeddings" in self._data.get("models", {}):
            return self.model_config("embeddings").get("model", "")
        return _deep_get(self._data, "memory", "embedding_model", default="")

    @property
    def embedding_base_url(self) -> str:
        if "embeddings" in self._data.get("models", {}):
            return self.model_config("embeddings").get("base_url", "")
        return _deep_get(self._data, "memory", "embedding_base_url", default="")

    @property
    def embedding_provider(self) -> str:
        if "embeddings" in self._data.get("models", {}):
            return self.model_config("embeddings").get("provider", "")
        return _deep_get(self._data, "memory", "embedding_provider", default="")

    @property
    def embedding_api_key(self) -> str:
        """Resolve API key for the embeddings provider."""
        if "embeddings" in self._data.get("models", {}):
            key_env = self.model_config("embeddings").get("api_key_env", "")
            if key_env:
                return os.environ.get(key_env, "")
        return ""

    @property
    def embedding_timeout(self) -> int:
        return _deep_get(self._data, "memory", "embedding_timeout", default=15)

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
    def subagent_max_turns(self) -> int:
        """Max turns for sub-agents. 0 = use max_turns_per_message."""
        val = _deep_get(self._data, "tools", "subagent_max_turns", default=0)
        return val if val > 0 else self.max_turns

    @property
    def subagent_timeout(self) -> float:
        """Timeout per API call for sub-agents. 0 = use agent_timeout_seconds."""
        val = _deep_get(self._data, "tools", "subagent_timeout", default=0.0)
        return float(val) if float(val) > 0 else self.agent_timeout

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
        return _deep_get(self._data, "tools", "web_search", "provider", default="")

    @property
    def web_search_api_key(self) -> str:
        """Resolve web search API key from [tools.web_search] api_key_env."""
        key_env = _deep_get(self._data, "tools", "web_search", "api_key_env", default="")
        return os.environ.get(key_env, "") if key_env else ""

    @property
    def tts_provider(self) -> str:
        return _deep_get(self._data, "tools", "tts", "provider", default="")

    @property
    def tts_api_key(self) -> str:
        """Resolve TTS API key from [tools.tts] api_key_env."""
        key_env = _deep_get(self._data, "tools", "tts", "api_key_env", default="")
        return os.environ.get(key_env, "") if key_env else ""

    @property
    def tts_timeout(self) -> int:
        return _deep_get(self._data, "tools", "tts", "timeout", default=60)

    @property
    def tts_api_url(self) -> str:
        """TTS API URL template. Empty = provider-specific default."""
        return _deep_get(self._data, "tools", "tts", "api_url", default="")

    @property
    def web_search_timeout(self) -> int:
        return _deep_get(self._data, "tools", "web_search", "timeout", default=15)

    @property
    def web_fetch_timeout(self) -> int:
        return _deep_get(self._data, "tools", "web_fetch", "timeout", default=15)

    @property
    def scheduling_max_scheduled(self) -> int:
        return _deep_get(self._data, "tools", "scheduling", "max_scheduled", default=50)

    @property
    def scheduling_max_delay(self) -> int:
        return _deep_get(self._data, "tools", "scheduling", "max_delay", default=86400)

    @property
    def filesystem_default_read_limit(self) -> int:
        return _deep_get(self._data, "tools", "filesystem", "default_read_limit", default=2000)

    # --- STT (Speech-to-Text) ---

    @property
    def stt_backend(self) -> str:
        return _deep_get(self._data, "stt", "backend", default="")

    @property
    def stt_voice_label(self) -> str:
        return _deep_get(self._data, "stt", "voice_label", default="voice message")

    @property
    def stt_voice_fail_msg(self) -> str:
        return _deep_get(self._data, "stt", "voice_fail_msg",
                         default="voice message — transcription failed")

    @property
    def stt_audio_label(self) -> str:
        return _deep_get(self._data, "stt", "audio_label", default="audio transcription")

    @property
    def stt_audio_fail_msg(self) -> str:
        return _deep_get(self._data, "stt", "audio_fail_msg",
                         default="audio transcription — failed")

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

    # --- Logging ---

    @property
    def log_max_bytes(self) -> int:
        return _deep_get(self._data, "logging", "max_bytes", default=10 * 1024 * 1024)

    @property
    def log_backup_count(self) -> int:
        return _deep_get(self._data, "logging", "backup_count", default=3)

    @property
    def logging_suppress(self) -> list[str]:
        return _deep_get(self._data, "logging", "suppress", default=[])

    # --- Vision ---

    @property
    def vision_max_image_bytes(self) -> int:
        return _deep_get(self._data, "vision", "max_image_bytes", default=5 * 1024 * 1024)

    @property
    def vision_max_dimension(self) -> int:
        return _deep_get(self._data, "vision", "max_dimension", default=1568)

    @property
    def vision_default_caption(self) -> str:
        return _deep_get(self._data, "vision", "default_caption", default="image")

    @property
    def vision_too_large_msg(self) -> str:
        return _deep_get(self._data, "vision", "too_large_msg",
                         default="image too large to display")

    @property
    def vision_jpeg_quality_steps(self) -> list[int]:
        return _deep_get(self._data, "vision", "jpeg_quality_steps", default=[85, 60, 40])

    # --- Behavior ---

    @property
    def silent_tokens(self) -> list[str]:
        return _deep_get(self._data, "behavior", "silent_tokens",
                         default=["NO_REPLY"])

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
    def message_retries(self) -> int:
        return _deep_get(self._data, "behavior", "message_retries", default=2)

    @property
    def message_retry_base_delay(self) -> float:
        return float(_deep_get(self._data, "behavior", "message_retry_base_delay", default=30.0))

    @property
    def audit_truncation_limit(self) -> int:
        return _deep_get(self._data, "behavior", "audit_truncation_limit", default=500)

    @property
    def agent_timeout(self) -> float:
        return float(_deep_get(self._data, "behavior", "agent_timeout_seconds", default=600))

    @property
    def max_turns(self) -> int:
        return _deep_get(self._data, "behavior", "max_turns_per_message", default=50)

    @property
    def max_cost_per_message(self) -> float:
        return float(_deep_get(self._data, "behavior", "max_cost_per_message", default=0.0))

    @property
    def compaction_threshold(self) -> int:
        return _deep_get(self._data, "behavior", "compaction", "threshold_tokens", default=150000)

    @property
    def compaction_max_tokens(self) -> int:
        """Max output tokens for compaction summaries (default: 2048)."""
        return _deep_get(self._data, "behavior", "compaction", "max_tokens", default=2048)

    @property
    def compaction_prompt(self) -> str:
        return _deep_get(self._data, "behavior", "compaction", "prompt", default=(
            "Summarize this conversation for {agent_name}.\n"
            "Rules:\n"
            "1. Return ONLY the summary text — no preamble, no labels, no commentary.\n"
            "2. Write as a dense narrative (200-600 words). Use {agent_name}'s voice.\n"
            "3. NEVER reproduce individual message turns, timestamps, or \"user:\"/\"A:\" labels.\n"
            "4. NEVER invent, fabricate, or extend content beyond what appears in the transcript.\n"
            "5. If something was discussed, state WHAT was discussed and any conclusions reached.\n"
            "6. Hard limit: {max_tokens} tokens. Your output WILL be cut off if you exceed this. Be concise.\n"
            "Preserve with full fidelity:\n"
            "- Emotional moments, relationship dynamics, things said with feeling\n"
            "- Decisions made and reasoning behind them\n"
            "- Commitments, promises, plans (who, what, when)\n"
            "- New information learned about the user or others\n"
            "- The agent's own reflections, opinions, and realizations\n"
            "- Any tasks, reminders, or follow-ups mentioned"
        ))

    @property
    def compaction_keep_pct(self) -> float:
        """Fraction of recent messages to keep verbatim during compaction (0.0–1.0)."""
        val = _deep_get(self._data, "behavior", "compaction", "keep_recent_pct", default=0.33)
        return max(0.05, min(0.9, float(val)))

    @property
    def diary_prompt(self) -> str:
        return _deep_get(self._data, "behavior", "compaction", "diary_prompt", default=(
            "[AUTOMATED DAILY MAINTENANCE]\n\n"
            "Write a memory log for {date}.\n\n"
            "RULES:\n"
            "1. Use the 'write' tool to create the file: memory/{date}.md\n"
            "2. Write 100-400 words summarizing today's conversations.\n"
            "3. Include: topics discussed, decisions made, commitments with deadlines, important moments.\n"
            "4. Write as a first-person diary entry.\n"
            "5. If the file already exists, overwrite it completely.\n"
            "6. Do NOT add any text response. ONLY use the write tool."
        ))

    # --- Compaction Verification ---

    @property
    def verify_enabled(self) -> bool:
        return _deep_get(self._data, "behavior", "compaction", "verify_enabled", default=True)

    @property
    def verify_max_turn_labels(self) -> int:
        return _deep_get(self._data, "behavior", "compaction", "verify_max_turn_labels", default=3)

    @property
    def verify_grounding_threshold(self) -> float:
        return float(_deep_get(self._data, "behavior", "compaction", "verify_grounding_threshold", default=0.5))

    @property
    def primary_sender(self) -> str:
        """Primary session sender for notification routing.

        When set, notifications route to this sender's session instead of
        creating throwaway system sessions.  Empty = disabled (default).
        """
        return _deep_get(self._data, "behavior", "primary_sender", default="")

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

    # --- Raw access ---

    def raw(self, *keys: str, default: Any = None) -> Any:
        return _deep_get(self._data, *keys, default=default)


def _load_dotenv(toml_path: Path) -> None:
    """Load .env file from same directory as lucyd.toml if it exists."""
    env_file = toml_path.parent / ".env"
    if not env_file.exists():
        return
    with env_file.open(encoding="utf-8") as f:
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
    with p.open("rb") as f:
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
