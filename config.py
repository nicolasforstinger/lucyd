"""Configuration loader for Lucyd daemon.

Loads lucyd.toml, applies environment variable overrides for secrets,
loads provider files from providers.d/, validates required fields,
and provides typed access to all settings.
Immutable after load — no runtime config reloading.

Schema-driven: _SCHEMA maps property names to (key_path, type, default).
Attribute access via __getattr__. ~17 custom @property methods with special
logic (env vars, path lists, fallbacks, clamping) take precedence.
"""

import logging
import os
import time
from pathlib import Path
from typing import Any

import tomllib

log = logging.getLogger(__name__)


def today_start_ts() -> int:
    """Unix timestamp for midnight today (local time)."""
    return int(time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d")))


class ConfigError(Exception):
    pass


# ─── Helpers ─────────────────────────────────────────────────────


def _deep_get(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(key, default)
    return d


def _resolve_path(p: str) -> Path:
    return Path(p).expanduser().resolve()


# ─── Schema ──────────────────────────────────────────────────────
#
# Declarative schema for all config properties.
# Each entry: property_name → (key_path, type, default)
#
# Types:
#   str, int, float, bool, list  — standard Python types
#   Path                         — str in TOML, auto-resolved to absolute Path
#
# float entries auto-cast int → float (TOML integers become Python int).
# Path entries auto-resolve via _resolve_path.
#
# Missing keys silently use the default. Feature gating is implicit:
# if a TOML section (stt, vision, documents) is absent, all its
# properties get defaults — no crash, no validation error.

# Adding a new config property:
#   1. Add an entry here: "prop_name": (("section", "key"), type, default)
#   2. Type can be str, int, float, bool, list, or Path (auto-resolved)
#   3. Attribute access is handled by Config.__getattr__ — no boilerplate needed
#   4. If the property needs custom logic (env vars, clamping, fallbacks),
#      add an explicit @property method in Config — it takes precedence
#   5. Feature-gated sections (stt, vision, etc.) don't need special handling:
#      missing sections silently use defaults

_SCHEMA: dict[str, tuple[tuple[str, ...], type, Any]] = {
    # ── Agent ────────────────────────────────────────────────────
    "context_stable":       (("agent", "context", "stable"),       list,  []),
    "context_semi_stable":  (("agent", "context", "semi_stable"),  list,  []),
    "max_system_tokens":    (("agent", "context", "max_system_tokens"), int, 0),
    "skills_dir":           (("agent", "skills", "dir"),           str,   "skills"),
    "always_on_skills":     (("agent", "skills", "always_on"),     list,  []),

    # ── HTTP API (always on) ─────────────────────────────────────
    "http_host":            (("http", "host"),                     str,   "0.0.0.0"),  # noqa: S104 — intentional: operator overrides via config
    "http_port":            (("http", "port"),                     int,   8100),
    "http_download_dir":    (("http", "download_dir"),             str,   ""),
    "http_max_body_bytes":  (("http", "max_body_bytes"),           int,   10485760),
    "http_max_attachment_bytes": (("http", "max_attachment_bytes"), int,   52428800),
    "http_rate_limit":      (("http", "rate_limit"),               int,   30),
    "http_rate_window":     (("http", "rate_window"),              int,   60),
    "http_status_rate_limit":    (("http", "status_rate_limit"),   int,   60),
    "http_trust_localhost":      (("http", "trust_localhost"),     bool,  False),

    # ── Database ─────────────────────────────────────────────────
    "database_pool_min":    (("database", "pool_min"),             int,   2),
    "database_pool_max":    (("database", "pool_max"),             int,   10),

    # ── Memory ───────────────────────────────────────────────────
    "memory_top_k":         (("memory", "search_top_k"),           int,   10),
    "vector_search_limit":  (("memory", "vector_search_limit"),    int,   10000),

    # ── Memory: Consolidation ────────────────────────────────────
    "consolidation_enabled":            (("memory", "consolidation", "enabled"),            bool,  False),
    "consolidation_confidence_threshold":(("memory", "consolidation", "confidence_threshold"), float, 0.6),

    # ── Memory: Recall ───────────────────────────────────────────
    "recall_decay_rate":            (("memory", "recall", "decay_rate"),            float, 0.03),
    "recall_max_facts":             (("memory", "recall", "max_facts_in_context"), int,   20),
    "recall_max_dynamic_tokens":    (("memory", "recall", "max_dynamic_tokens"),   int,   0),
    "recall_max_episodes_at_start": (("memory", "recall", "max_episodes_at_start"), int,  3),

    # ── Memory: Maintenance ──────────────────────────────────────
    "maintenance_stale_threshold_days": (("memory", "maintenance", "stale_threshold_days"), int, 90),

    # ── Memory: Indexer ──────────────────────────────────────────
    "indexer_include_patterns": (("memory", "indexer", "include_patterns"),   list, []),
    "indexer_exclude_dirs":     (("memory", "indexer", "exclude_dirs"),       list, []),
    "indexer_chunk_size":       (("memory", "indexer", "chunk_size_chars"),   int,  1600),
    "indexer_chunk_overlap":    (("memory", "indexer", "chunk_overlap_chars"), int, 320),
    "indexer_embed_batch_limit":(("memory", "indexer", "embed_batch_limit"), int,  100),

    # ── Memory: Search ──────────────────────────────────────────
    "fts_min_results":      (("memory", "fts_min_results"),     int,   3),

    # ── Embedding ────────────────────────────────────────────────
    "embedding_timeout":    (("memory", "embedding_timeout"),   int,   15),

    # ── Tools ────────────────────────────────────────────────────
    "tools_enabled":        (("tools", "enabled"),              list,  []),
    "plugins_dir":          (("tools", "plugins_dir"),          str,   "plugins.d"),
    "output_truncation":    (("tools", "output_truncation"),    int,   30000),
    "exec_timeout":         (("tools", "exec_timeout"),         int,   120),
    "exec_max_timeout":     (("tools", "exec_max_timeout"),     int,   600),
    "subagent_deny":        (("tools", "subagent_deny"),        list,  []),
    "tool_call_retry":      (("tools", "tool_call_retry"),      bool,  False),
    "filesystem_default_read_limit": (("tools", "filesystem", "default_read_limit"), int, 2000),

    # ── Tools: Web ───────────────────────────────────────────────
    "web_search_provider":  (("tools", "web_search", "provider"),  str,  ""),
    "web_search_api_url":   (("tools", "web_search", "api_url"),   str,  ""),
    "web_search_timeout":   (("tools", "web_search", "timeout"),   int,  15),
    "web_fetch_timeout":    (("tools", "web_fetch", "timeout"),    int,  15),

    # ── Documents ────────────────────────────────────────────────
    "documents_enabled":        (("documents", "enabled"),        bool, False),
    "documents_max_chars":      (("documents", "max_chars"),      int,  30000),
    "documents_max_file_bytes": (("documents", "max_file_bytes"), int,  10485760),
    "documents_text_extensions":(("documents", "text_extensions"), list, []),
    "documents_pdf_max_render_pages": (("documents", "pdf_max_render_pages"), int, 5),

    # ── Vision ───────────────────────────────────────────────────
    "vision_max_image_bytes":   (("vision", "max_image_bytes"),  int,  5242880),
    "vision_max_dimension":     (("vision", "max_dimension"),    int,  1568),
    "vision_jpeg_quality_steps":(("vision", "jpeg_quality_steps"), list, [85, 60, 40]),
    # ── Logging ──────────────────────────────────────────────────
    "logging_suppress": (("logging", "suppress"),     list, []),
    "log_format":       (("logging", "format"),        str,  "text"),
    # ── Strategy ────────────────────────────────────────────────
    "agent_strategy":           (("agent", "strategy"),                    str,   "tool_use"),

    # ── Behavior ─────────────────────────────────────────────────
    "debounce_ms":              (("behavior", "debounce_ms"),              int,   500),
    "silent_tokens":            (("behavior", "silent_tokens"),            list,  ["NO_REPLY"]),
    "typing_indicators":        (("behavior", "typing_indicators"),        bool,  True),
    "error_message":            (("behavior", "error_message"),            str,   "connection error"),
    "api_retries":              (("behavior", "api_retries"),              int,   2),
    "api_retry_base_delay":     (("behavior", "api_retry_base_delay"),     float, 2.0),
    "message_retries":          (("behavior", "message_retries"),          int,   2),
    "message_retry_base_delay": (("behavior", "message_retry_base_delay"), float, 30.0),
    "agent_timeout":            (("behavior", "agent_timeout_seconds"),     float, 600.0),
    "max_turns":                (("behavior", "max_turns_per_message"),     int,   50),
    "max_cost_per_message":     (("behavior", "max_cost_per_message"),     float, 0.0),
    "notify_target":            (("behavior", "notify_target"),            str,   ""),
    "max_context_for_tools":    (("behavior", "max_context_for_tools"),    int,   0),

    # ── Behavior: Compaction ─────────────────────────────────────
    "compaction_threshold":     (("behavior", "compaction", "threshold_tokens"),       int,   150000),
    "compaction_max_tokens":    (("behavior", "compaction", "max_tokens"),             int,   2048),
    "compaction_prompt":        (("behavior", "compaction", "prompt"),                 str,   "Summarize this conversation for {agent_name}. Keep it under {max_tokens} tokens."),
    "diary_prompt":             (("behavior", "compaction", "diary_prompt"),           str,   ""),
    # ── Agent Identity ─────────────────────────────────────────
    "agent_id":                 (("agent", "id"),                    str, ""),
    "client_id":                (("agent", "client_id"),             str, ""),

    # ── Model Routing ────────────────────────────────────────────
    # Override model role for specific tasks. "" = use primary.
    "compaction_model":     (("models", "routing", "compaction"),     str, ""),
    "consolidation_model":  (("models", "routing", "consolidation"),  str, ""),
    "subagent_model":       (("models", "routing", "subagent"),       str, ""),

    # ── Paths ────────────────────────────────────────────────────
    # Empty string = derive from data_dir at resolution time.
    "state_dir":    (("paths", "state_dir"),    Path, ""),
    "sessions_dir": (("paths", "sessions_dir"), Path, ""),
    "log_file":              (("paths", "log_file"),             Path, ""),

    # ── Metering ───────────────────────────────────────────────
    "metering_retention_months": (("metering", "retention_months"), int, 84),  # 7 years (BAO §132)

    # ── Conversion ──────────────────────────────────────────────
    "conversion_api_url":     (("conversion", "api_url"),     str,   ""),
    "conversion_static_rate": (("conversion", "static_rate"), float, 1.0),
}


# ─── Config Class ────────────────────────────────────────────────


class Config:
    """Immutable configuration loaded from lucyd.toml.

    Schema-driven: _SCHEMA entries are accessed via __getattr__.
    Custom @property methods handle the ~17 properties that need special
    logic (env var resolution, path list expansion, value clamping,
    cross-field fallbacks) and take precedence over __getattr__.
    """

    def __init__(self, data: dict[str, Any], config_dir: Path | None = None):
        self._data = data
        self._config_dir = config_dir or Path.cwd()
        self._load_providers()
        self._values: dict[str, Any] = {}
        self._explicit_keys: set[str] = set()
        self._validate()

    def __getattr__(self, name: str) -> Any:
        """Schema-driven attribute access for _SCHEMA entries."""
        try:
            return self._values[name]
        except KeyError:
            raise AttributeError(name) from None

    # ── Schema Resolution ────────────────────────────────────────

    def _validate(self) -> None:
        """Validate core requirements, resolve schema, check ranges.

        All errors are collected and reported at once — no one-at-a-time
        crashes hours after startup.
        """
        errors: list[str] = []

        # Core field presence (must be non-empty)
        if not _deep_get(self._data, "agent", "name"):
            errors.append("[agent] name is required")
        if not _deep_get(self._data, "agent", "workspace"):
            errors.append("[agent] workspace is required")
        if not _deep_get(self._data, "models", "primary"):
            errors.append("[models.primary] section is required")
        primary = _deep_get(self._data, "models", "primary", default={})
        if not primary.get("provider"):
            errors.append("[models.primary] provider is required")
        if not primary.get("model"):
            errors.append("[models.primary] model is required")

        # Resolve all schema entries into _values
        for name, (key_path, typ, default) in _SCHEMA.items():
            val = _deep_get(self._data, *key_path)
            if val is None:
                # Key absent — use default
                if typ is Path and isinstance(default, str):
                    self._values[name] = _resolve_path(default) if default else Path()
                else:
                    self._values[name] = default
            else:
                # Key present — type coercion + mark as explicitly set
                if typ is float and isinstance(val, (int, float)) and not isinstance(val, bool):
                    val = float(val)
                elif typ is Path and isinstance(val, str):
                    val = _resolve_path(val)
                self._values[name] = val
                self._explicit_keys.add(name)

        # Derive unset paths from data_dir (env: LUCYD_DATA_DIR, default: /data)
        self._resolve_data_dir_paths()

        # Numeric range validation
        if "max_context_tokens" in primary:
            v = primary["max_context_tokens"]
            if isinstance(v, (int, float)) and v <= 0:
                errors.append("[models.primary] max_context_tokens must be > 0")
        for key in ("agent_timeout_seconds", "api_retry_base_delay",
                     "message_retry_base_delay"):
            val = _deep_get(self._data, "behavior", key)
            if val is not None and (not isinstance(val, (int, float)) or val < 0):
                errors.append(f"[behavior] {key} must be >= 0")
        for key in ("api_retries", "message_retries"):
            val = _deep_get(self._data, "behavior", key)
            if val is not None and (not isinstance(val, (int, float)) or val < 0):
                errors.append(f"[behavior] {key} must be >= 0")
        threshold = _deep_get(self._data, "behavior", "compaction", "threshold_tokens")
        if threshold is not None and (not isinstance(threshold, (int, float)) or threshold < 1):
            errors.append("[behavior.compaction] threshold_tokens must be >= 1")
        pdf_pages = _deep_get(self._data, "documents", "pdf_max_render_pages")
        if pdf_pages is not None and (not isinstance(pdf_pages, int) or pdf_pages < 1):
            errors.append("[documents] pdf_max_render_pages must be >= 1")
        # Vision quality steps — must be descending
        steps = _deep_get(self._data, "vision", "jpeg_quality_steps")
        if steps is not None and isinstance(steps, list) and steps != sorted(steps, reverse=True):
            errors.append(f"vision.jpeg_quality_steps must be in descending order, got {steps}")

        if errors:
            raise ConfigError("Configuration errors:\n" + "\n".join(f"  - {e}" for e in errors))

    def _resolve_data_dir_paths(self) -> None:
        """Derive unset path defaults from data_dir.

        data_dir source priority: LUCYD_DATA_DIR env var > [paths] data_dir in TOML > /data
        All paths (state_dir, sessions_dir, log_file, http_download_dir)
        derive from data_dir if not explicitly set.
        """
        toml_data_dir = _deep_get(self._data, "paths", "data_dir", default="")
        self._data_dir = Path(
            os.environ.get("LUCYD_DATA_DIR", "") or toml_data_dir or "/data",
        ).resolve()

        path_defaults = {
            "state_dir":        self._data_dir,
            "sessions_dir":     self._data_dir / "sessions",
            "log_file":         self._data_dir / "logs" / "lucyd.log",
        }
        for name, default_path in path_defaults.items():
            if name not in self._explicit_keys:
                self._values[name] = default_path

        if "http_download_dir" not in self._explicit_keys or not self._values.get("http_download_dir"):
            self._values["http_download_dir"] = str(self._data_dir / "downloads")

        # Validate all resolved paths are absolute (catches misconfigured data_dir)
        for name in ("state_dir", "sessions_dir", "log_file"):
            val = self._values.get(name)
            if isinstance(val, Path) and not val.is_absolute():
                log.warning("Resolved path '%s' is not absolute: %s — may indicate misconfigured data_dir", name, val)

    @property
    def data_dir(self) -> Path:
        """Single configurable root for all persistent state."""
        return self._data_dir

    # ── Provider Loading ──────────────────────────────────────────

    def _load_providers(self) -> None:
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
                # Validate: every model must have a non-empty provider for
                # accurate Prometheus labels.  Empty provider labels create
                # orphaned metrics that pollute dashboards.
                if not model_cfg.get("provider"):
                    raise ConfigError(
                        f"Model '{model_name}' in provider '{name}' has no "
                        f"provider type — check 'type' in {provider_file}"
                    )
                self._data["models"][model_name] = model_cfg

            log.info("Loaded provider '%s': %s (%d models)",
                     name, provider_type,
                     len(pdata.get("models", {})))

    # ── Core Properties (validated in _validate) ─────────────────

    @property
    def agent_name(self) -> str:
        return str(self._data["agent"]["name"])

    @property
    def workspace(self) -> Path:
        return _resolve_path(self._data["agent"]["workspace"])

    @property
    def config_dir(self) -> Path:
        """Directory containing lucyd.toml (for resolving relative paths)."""
        return self._config_dir

    # ── Custom Properties (complex logic) ────────────────────────

    @property
    def database_url(self) -> str:
        """Resolve database URL from env var named by [database] url_env."""
        env = _deep_get(self._data, "database", "url_env", default="")
        return os.environ.get(env, "") if env else ""

    @property
    def http_auth_token(self) -> str:
        env = _deep_get(self._data, "http", "token_env", default="")
        return os.environ.get(env, "") if env else ""

    @property
    def web_search_api_key(self) -> str:
        """Resolve web search API key from [tools.web_search] api_key_env."""
        key_env = _deep_get(self._data, "tools", "web_search", "api_key_env", default="")
        return os.environ.get(key_env, "") if key_env else ""

    # ── Embedding (fallback: models.embeddings → memory section) ─

    @property
    def embedding_model(self) -> str:
        """Read from [models.embeddings] (provider file) or [memory] override. Empty = not configured."""
        if "embeddings" in self._data.get("models", {}):
            return str(self.model_config("embeddings").get("model", ""))
        return str(_deep_get(self._data, "memory", "embedding_model", default=""))

    @property
    def embedding_base_url(self) -> str:
        if "embeddings" in self._data.get("models", {}):
            return str(self.model_config("embeddings").get("base_url", ""))
        return str(_deep_get(self._data, "memory", "embedding_base_url", default=""))

    @property
    def embedding_provider(self) -> str:
        if "embeddings" in self._data.get("models", {}):
            return str(self.model_config("embeddings").get("provider", ""))
        return str(_deep_get(self._data, "memory", "embedding_provider", default=""))

    @property
    def embedding_api_key(self) -> str:
        """Resolve API key for the embeddings provider."""
        if "embeddings" in self._data.get("models", {}):
            key_env = self.model_config("embeddings").get("api_key_env", "")
            if key_env:
                return os.environ.get(key_env, "")
        return ""

    @property
    def embedding_cost_rates(self) -> list[float]:
        """Cost rates for the embeddings model from provider config."""
        if "embeddings" in self._data.get("models", {}):
            return list(self.model_config("embeddings").get("cost_per_mtok", []))
        return []

    @property
    def embedding_currency(self) -> str:
        """Billing currency for the embeddings provider."""
        if "embeddings" in self._data.get("models", {}):
            return str(self.model_config("embeddings").get("currency", "EUR"))
        return "EUR"

    # ── Filesystem (path list resolution) ────────────────────────

    @property
    def filesystem_allowed_paths(self) -> list[str]:
        paths = _deep_get(self._data, "tools", "filesystem", "allowed_paths", default=[])
        return [str(_resolve_path(p)) for p in paths]

    # ── Compaction (clamping + cross-field) ──────────────────────

    @property
    def compaction_keep_pct(self) -> float:
        """Fraction of recent messages to keep verbatim during compaction (0.0–1.0).

        Adaptive: for small contexts (< 32k tokens), defaults to 0.5 instead
        of 0.3 — shorter compaction inputs produce faster compaction calls on CPU.
        """
        raw = _deep_get(self._data, "behavior", "compaction", "keep_recent_pct", default=None)
        if raw is not None:
            val = float(raw)
        else:
            # Adaptive default based on context size
            max_ctx = _deep_get(self._data, "models", "primary", "max_context_tokens", default=0)
            val = 0.5 if max_ctx and max_ctx <= 32768 else 0.3
        keep_min = _deep_get(self._data, "behavior", "compaction", "keep_recent_pct_min", default=0.05)
        keep_max = _deep_get(self._data, "behavior", "compaction", "keep_recent_pct_max", default=0.9)
        return float(max(keep_min, min(keep_max, val)))

    # ── Sub-agents (cross-field fallback) ────────────────────────

    @property
    def resolved_client_id(self) -> str:
        """Client identity — falls back to agent_name when client_id is unset."""
        return self.client_id or self.agent_name

    @property
    def resolved_agent_id(self) -> str:
        """Agent identity — falls back to agent_name when agent_id is unset."""
        return self.agent_id or self.agent_name

    @property
    def subagent_max_turns(self) -> int:
        """Max turns for sub-agents. 0 = use max_turns_per_message."""
        val = int(_deep_get(self._data, "tools", "subagent_max_turns", default=0))
        return val if val > 0 else int(self.max_turns)

    @property
    def subagent_timeout(self) -> float:
        """Timeout per API call for sub-agents. 0 = use agent_timeout_seconds."""
        val = float(_deep_get(self._data, "tools", "subagent_timeout", default=0))
        return val if val > 0 else self.agent_timeout

    # ── Methods ──────────────────────────────────────────────────

    def model_config(self, name: str) -> dict[str, Any]:
        cfg = _deep_get(self._data, "models", name, default={})
        if not cfg:
            raise ValueError(f"No model config for '{name}'")
        return dict(cfg)

    def raw(self, *keys: str, default: Any = None) -> Any:
        return _deep_get(self._data, *keys, default=default)


# ─── File Loading ────────────────────────────────────────────────


def _load_dotenv(toml_path: Path) -> None:
    """Load .env file from same directory as lucyd.toml if it exists."""
    env_file = toml_path.parent / ".env"
    if not env_file.exists():
        return
    try:
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
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"Failed to read .env file ({env_file}): {e}") from e


def load_config(path: str | Path, overrides: dict[str, Any] | None = None) -> Config:
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
