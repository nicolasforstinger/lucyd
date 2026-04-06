# Configuration Reference

All configuration lives in `lucyd.toml`. API keys are loaded from `.env` in the same directory.

## [agent]

Agent identity and workspace.

```toml
[agent]
name = "YourAgent"                         # Agent name (used in logs)
id = ""                                    # Agent ID for metering (default: agent name)
workspace = "~/.lucyd/workspace"           # Workspace root (personality files, skills, memory logs)
strategy = "tool_use"                      # Agent strategy: "tool_use" (multi-turn) or "single_shot" (one call, no tools)
```

### [agent.context]

Files loaded into the system prompt, organized by cache tier. Stable files change rarely (cached aggressively). Semi-stable files change occasionally.

```toml
[agent.context]
stable = ["SOUL.md", "AGENTS.md", "USER.md", "IDENTITY.md", "TOOLS.md"]
semi_stable = ["MEMORY.md"]
# max_system_tokens = 0              # Cap system prompt size (0 = unlimited). Semi-stable trimmed first, then dynamic. Stable never trimmed.
```

All paths are relative to `workspace`.

### [agent.skills]

Skill loading configuration.

```toml
[agent.skills]
dir = "skills"                                           # Subdirectory of workspace
always_on = ["compute-routing", "natural-conversation"]  # Injected into every system prompt
```

Skills not in `always_on` appear in an index. The agent loads them on demand via the `load_skill` tool.

## Channels

Channels are standalone bridge processes with their own config files. Each bridge config has the same structure:

```toml
[daemon]
url = "http://127.0.0.1:8100"       # Where to find the daemon
token_env = "LUCYD_HTTP_TOKEN"       # Env var for API auth

[<protocol>]
# Protocol-specific settings
```

| Bridge | Config file | Env var override | Template |
|--------|-------------|-----------------|----------|
| Telegram | `telegram.toml` | `LUCYD_TELEGRAM_CONFIG` | `channels/telegram.toml.example` |
| Email | `email.toml` | `LUCYD_EMAIL_CONFIG` | `channels/email.toml.example` |
| CLI | env vars only | `LUCYD_URL` | — |

All bridges fall back to environment variables if no config file is found. The `debounce_ms` setting in `[behavior]` controls message batching for queued messages on the daemon side.

## [http]

HTTP API server. Always starts — there is no `enabled` toggle.

```toml
[http]
host = "127.0.0.1"          # Listen address (code default: 0.0.0.0 — set to 127.0.0.1 for localhost only)
port = 8100                  # Listen port (default: 8100)
token_env = "LUCYD_HTTP_TOKEN"  # Env var containing the bearer token
trust_localhost = false      # Skip auth for 127.0.0.1/::1 (default: false)
download_dir = "/tmp/lucyd-http"  # Temp dir for HTTP attachment downloads
max_body_bytes = 10485760    # Max request body size in bytes (default: 10 MB)
rate_limit = 30              # Max requests per rate_window per sender (default: 30)
rate_window = 60             # Rate limit window in seconds (default: 60)
status_rate_limit = 60       # Max /status requests per rate_window (default: 60)
max_attachment_bytes = 52428800  # Max size for base64-decoded attachments (default: 50 MB)
```

Auth token is loaded from the environment variable named by `token_env` (default: `LUCYD_HTTP_TOKEN`). All protected endpoints require a valid `Bearer` token. The `/api/v1/status` and `/metrics` endpoints are always auth-exempt.

**`trust_localhost`:** When `true`, requests from `127.0.0.1` / `::1` bypass auth (no token required). Default is `false` — all requests require a bearer token. Enable this for Docker bridge networks where channel bridges (Telegram, email) and cron jobs (`lucydctl`) run as separate containers on the same host. When `false`, bridges and `lucydctl` must provide a valid token via `LUCYD_HTTP_TOKEN`.

See [operations — HTTP API](operations.md#http-api) for endpoint details.

## [providers]

Model definitions are loaded from external provider files rather than inlined in `lucyd.toml`. This keeps the main config clean and lets you swap providers by editing the `load` list.

```toml
[providers]
load = ["anthropic", "openai"]     # Provider files to load (from providers.d/)
# dir = "providers.d"              # Directory for provider files (default: providers.d/ relative to lucyd.toml)
```

Each entry in `load` corresponds to a file `providers.d/{name}.toml`. Provider files define the connection type, API key, and one or more `[models.*]` sections. On load, model sections are merged into the main config as if they were defined inline.

### Provider File Format

Each provider file (`providers.d/*.toml`) has top-level connection settings and `[models.*]` sections:

```toml
# providers.d/anthropic.toml
type = "anthropic"
api_key_env = "LUCYD_ANTHROPIC_KEY"
currency = "USD"

[models.primary]
model = "claude-sonnet-4-6"
max_tokens = 65536
max_context_tokens = 200000
cost_per_mtok = [3.0, 15.0, 0.3, 3.75]   # [input, output, cache_read, cache_write]
cache_control = true
thinking_enabled = true
thinking_mode = "adaptive"          # "adaptive" | "budgeted" | "disabled"

```

```toml
# providers.d/openai.toml
type = "openai"
api_key_env = "LUCYD_OPENAI_KEY"
base_url = "https://api.openai.com/v1"
currency = "USD"

[models.embeddings]
model = "text-embedding-3-small"
cost_per_mtok = [0.02, 0.0, 0.0, 0.0]
```

The `type` and `api_key_env` from the provider file are inherited by each `[models.*]` section in that file, so individual models don't need to repeat them.

## [models.*]

Model names (`primary`, `embeddings`) are referenced by behavior settings and the tool system. No alias table — model IDs resolve directly to API calls. All chat operations use the `primary` model; `embeddings` is the only separate model (fundamentally different API type).

**Common options (all providers):**

| Option | Purpose |
|---|---|
| `model` | Model identifier passed to the provider API (e.g., `"claude-sonnet-4-6"`) |
| `max_tokens` | Maximum output tokens per API call |
| `max_context_tokens` | Maximum input context window size (used by `session_status` tool for context % display) |
| `cost_per_mtok` | Cost per million tokens as `[input, output, cache_read, cache_write]` — used for cost tracking |
| `supports_vision` | Enable vision/image input for this model (default: `false` — must be declared per-model in provider files) |

**Provider-level options (inherited by all models in the file):**

| Option | Purpose |
|---|---|
| `currency` | Billing currency for this provider (e.g., `"USD"`, `"EUR"`). Default: `"EUR"`. Used with `[conversion]` to convert costs to EUR. |

**Provider-specific options:**

| Option | Provider | Purpose |
|---|---|---|
| `cache_control` | anthropic | Enable prompt caching via cache tier metadata |
| `thinking_enabled` | anthropic | Enable extended thinking (chain-of-thought) |
| `thinking_budget` | anthropic | Max tokens for thinking block |
| `thinking_mode` | anthropic | Thinking mode: `"adaptive"` (model decides depth), `"budgeted"` (uses `thinking_budget`), `"disabled"` |
| `thinking_effort` | anthropic | Thinking effort level (empty = default) |
| `base_url` | openai | API base URL |
| `slot_id` | openai | Pin to llama-server slot for prompt cache affinity (-1 = auto) |

### [models.routing]

Override the model role used for specific tasks. Empty string (default) = use primary model.

```toml
[models.routing]
compaction = ""       # Model role for compaction summaries (default: "" = primary)
consolidation = ""    # Model role for structured data extraction (default: "" = primary)
subagent = ""         # Model role for sub-agents (default: "" = primary)
```

## [memory]

Long-term memory configuration.

```toml
[memory]
db = "~/.lucyd/memory/main.sqlite"            # SQLite DB with FTS5 + embeddings
search_top_k = 10                             # Default result limit for memory searches
embedding_timeout = 15                        # Embedding API request timeout (seconds)
vector_search_limit = 10000                   # Raw DB query cap for vector search
```

The memory DB is optional. If the path is empty or the file does not exist, memory tools are not registered.

### [memory.consolidation]

Structured data extraction from session transcripts and workspace files.

```toml
[memory.consolidation]
enabled = true                        # Enable structured memory extraction (code default: false)
confidence_threshold = 0.6            # Minimum confidence for extracted facts
```

When enabled, `lucydctl --consolidate` (cron at `:15`) extracts facts, episodes, commitments, and entity aliases from workspace files. Also triggers on session close and pre-compaction. See `memory_schema.py` for table definitions.

### [memory.maintenance]

Periodic cleanup of structured memory.

```toml
[memory.maintenance]
stale_threshold_days = 90             # Remove unaccessed facts older than this (default: 90)
```

Runs via `lucydctl --maintain` (cron daily at `04:05`).

### Memory evolution

Evolution is triggered via `bin/lucydctl --evolve` (cron daily at `04:20`, after maintenance) or `POST /api/v1/evolve`. No config section — evolution behavior is entirely defined by the `evolution` skill file in workspace. The daemon queues a message, and the agent loads the skill and rewrites workspace files through the full agentic loop.

### [memory.recall]

Controls how structured memory is injected into session context at startup.

```toml
[memory.recall]
decay_rate = 0.03                    # Time-decay factor for relevance scoring
max_facts_in_context = 20            # Maximum facts injected into context
max_dynamic_tokens = 1500            # Token budget for dynamic recall content (default: 0 = unlimited)
max_episodes_at_start = 3            # Maximum episodes injected at session start
```

Recall runs at session start and enriches `memory_search` results with structured data. Budget-aware: prioritizes commitments > vector > episodes > facts (under budget pressure, clinical facts drop first).

Recall priority and formatting are hardcoded constants in `memory.py`. Drop order under budget pressure: facts (15) → episodes (25) → vector (35) → commitments (40).

### [memory.indexer]

Controls which workspace files are indexed into the FTS5 + vector memory DB.

```toml
[memory.indexer]
include_patterns = ["memory/*.md", "MEMORY.md"]   # Glob patterns relative to workspace
exclude_dirs = []                                  # Directories to skip
chunk_size_chars = 1600                            # Characters per text chunk (default: 1600)
chunk_overlap_chars = 320                          # Overlap between chunks (default: 320)
embed_batch_limit = 100                            # Max chunks per embedding API batch (default: 100)
```

The indexer (`lucydctl --index`) runs hourly at `:10` via cron. Incremental — skips files whose content hash hasn't changed.

## [tools]

Tool registration and execution settings.

```toml
[tools]
enabled = [
    "read", "write", "edit", "exec",
    "web_search", "web_fetch",
    "memory_search", "memory_get",
    "memory_write", "memory_forget", "commitment_update",
    "session_status", "sessions_spawn", "load_skill",
]
output_truncation = 30000    # Truncate tool output beyond this many characters
plugins_dir = "plugins.d"   # Directory for custom tool plugins
exec_timeout = 120           # Default exec tool timeout (seconds)
exec_max_timeout = 600       # Maximum allowed exec timeout (seconds)
subagent_deny = ["sessions_spawn"]                         # Tools denied to sub-agents
subagent_max_turns = 0       # Max turns for sub-agents (0 = use max_turns_per_message)
subagent_timeout = 0         # Timeout for sub-agents in seconds (0 = use agent_timeout_seconds)
# tool_call_retry = false    # Retry tool calls with guidance when args are invalid (for small models, default: false)
# tool_success_warn_threshold = 0.5  # Warn when tool success rate drops below this (0.0–1.0, default: 0.5)
```

The `subagent_deny` list controls which tools are blocked for sub-agents spawned via `sessions_spawn`. When omitted, the default deny-list applies: `sessions_spawn` (prevents recursion). Sub-agents CAN load skills by default. Set to `[]` to allow all tools.

Tools are only registered if they appear in `enabled` AND their dependencies are met (e.g., `memory_search` requires a configured `memory.db`).

### [tools.filesystem]

```toml
[tools.filesystem]
allowed_paths = ["~/.lucyd/workspace", "/tmp/"]    # Path prefixes the agent can read/write (defaults to workspace + /tmp/)
default_read_limit = 2000                          # Max lines returned by the read tool (default: 2000)
```

### [tools.web_search]

```toml
[tools.web_search]
provider = "brave"              # Web search provider (currently only "brave")
api_key_env = "LUCYD_BRAVE_KEY" # Env var for web search API key
timeout = 15                    # Request timeout in seconds (default: 15)
```

### [tools.web_fetch]

```toml
[tools.web_fetch]
timeout = 15          # Request timeout in seconds (default: 15)
```

## Plugin Config

TTS and STT config has moved from `lucyd.toml` to plugin-local TOML files in `plugins.d/`:

- **ElevenLabs TTS**: `plugins.d/elevenlabs.toml` (see `elevenlabs.toml.example`)
- **Whisper STT**: `plugins.d/whisper.toml` (see `whisper.toml.example`)

Each plugin loads its own config via `tomllib` at startup. Core does not reference plugin config keys.

## [documents]

Document attachment processing. Extracts text from attachments (PDF, text files) so the agent sees content, not just `[attachment: file, type]` labels. Text-layer PDF support requires `pypdf`. Scanned PDFs with no extractable text fall back to rendering pages as images for vision-capable models (requires `poppler-utils`).

```toml
[documents]
enabled = true                  # Enable document text extraction (code default: false)
max_chars = 30000               # Truncation limit for extracted text (default: 30000)
max_file_bytes = 10485760       # Skip files larger than this (default: 10 MB)
text_extensions = [
    ".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml",
    ".html", ".htm", ".py", ".js", ".ts", ".sh", ".toml",
    ".ini", ".cfg", ".log", ".sql", ".css",
]

# Scanned PDF fallback — render first N pages as images for vision-capable models.
# Requires poppler-utils (pdftoppm). Only triggers when text extraction yields nothing.
pdf_max_render_pages = 5          # Max pages to render (default: 5)
```

Files are matched by extension (for text) or MIME type (for PDF). Non-extractable formats fall through to label-only. Scanned PDFs (no text layer) are rendered as images and injected into the vision pipeline when the model supports vision.

## [vision]

Image processing settings for inbound images.

```toml
[vision]
max_image_bytes = 5242880              # Skip inbound images larger than this (bytes, default 5 MB)
max_dimension = 1568                   # Max px on longest side (default: 1568)
jpeg_quality_steps = [85, 60, 40]      # JPEG quality reduction steps for fitting oversized images
```

When an image exceeds `max_image_bytes`, the daemon tries dimension scaling first, then iterates through `jpeg_quality_steps` to reduce JPEG quality. If the image still exceeds the limit after all steps (e.g., PNG which can't be quality-reduced), a fallback text label is used.

## [behavior]

Runtime behavior tuning.

```toml
[behavior]
silent_tokens = ["NO_REPLY"]                                           # Replies starting/ending with these are not delivered
typing_indicators = true                                               # Send typing indicator before processing
error_message = "I'm having trouble connecting right now. Try again in a moment."  # Sent when agentic loop fails
agent_timeout_seconds = 600                                            # Timeout per API call in the agentic loop
max_turns_per_message = 50                                             # Max tool-use iterations per inbound message
max_cost_per_message = 5.0                                             # USD circuit breaker per message (0.0 = disabled)
api_retries = 2                                                        # Retry attempts for transient API errors (429, 5xx, connection). Default: 2
api_retry_base_delay = 2.0                                             # Initial backoff delay in seconds (exponential with jitter). Default: 2.0
message_retries = 2                                                    # Message-level retries on persistent failure (default: 2)
message_retry_base_delay = 30                                          # Base delay between message retries in seconds (default: 30)
sqlite_timeout = 30                                                    # SQLite connection timeout in seconds for all DBs (default: 30)
notify_target = ""                                                    # Route all notifications to this sender's session (default: "" = disabled)
# max_context_for_tools = 0                                             # Inject wrap-up hint when context exceeds this during tool use (0 = disabled)
# thinking_concise_hint = false                                         # Inject "respond concisely" hint after tool results to reduce thinking overhead
```

**`notify_target`:** When set, all notifications (`/notify`, `--notify`) route to the named sender's session instead of creating throwaway sessions per source. Keeps notification context in the agent's main conversation. Empty string disables (backward compatible).

**Two-tier retry architecture:** `api_retries` handles transient errors (429, 5xx, connection) within a single agentic loop call (fast, 1–8s backoff). `message_retries` retries the entire message processing when the agentic loop fails after exhausting API retries (slower, 30–60s backoff with jitter).

### [behavior.compaction]

Session compaction (summarization of old messages to free context window).

```toml
[behavior.compaction]
threshold_tokens = 150000    # Trigger compaction when last input_tokens exceeds this
warning_pct = 0.8            # Fraction of threshold at which to inject context warning (default: 0.8)
min_messages = 4             # Minimum messages before compaction can trigger (default: 4)
keep_recent_pct = 0.33       # Keep newest fraction of messages verbatim (default: 0.33)
keep_recent_pct_min = 0.05   # Floor for keep_recent_pct (default: 0.05)
keep_recent_pct_max = 0.9    # Ceiling for keep_recent_pct (default: 0.9)
max_tokens = 2048            # Max output tokens for compaction summary (caps the primary model's default)
tool_result_max_chars = 2000 # Max chars per tool result kept in compacted context (default: 2000)
prompt = "..."               # Compaction prompt (supports {agent_name}, {max_tokens}). See lucyd.toml.example
diary_prompt = "..."         # Diary prompt for forced compact (supports {date}). See lucyd.toml.example
```

Compaction takes the oldest messages (1 - `keep_recent_pct`), summarizes them, and replaces them with the summary. The JSONL audit trail retains the full history.

**Forced compact:** `lucydctl --compact` / `POST /api/v1/compact` sends a diary prompt to the primary session. The agent writes a daily memory log via the `write` tool, then compaction fires regardless of token threshold.

## [logging]

Log format and suppression settings. Rotation is hardcoded (10 MB, 3 backups).

```toml
[logging]
suppress = ["httpx", "httpcore", "anthropic", "openai"]  # Third-party loggers suppressed to WARNING
format = "text"         # "text" (default) or "json" (one JSON object per line, for Docker log drivers)
```

## [metering]

Cost tracking configuration.

```toml
[metering]
currency = "EUR"           # Display currency for cost reports (default: EUR)
retention_months = 12      # Delete metering records older than this via lucydctl --maintain (default: 12)
```

## [conversion]

Currency conversion for multi-currency cost tracking. Converts provider-native costs (e.g., USD for Anthropic/OpenAI) to EUR using an FX rate API. Requires `currency` to be set in provider config files.

```toml
[conversion]
api_url = "https://api.frankfurter.dev/v2/rate/EUR/USD"  # FX rate API endpoint ("" = static-only)
static_rate = 1.15                                       # Fallback rate when API is unavailable (1 EUR = X foreign)
```

| Option | Purpose |
|---|---|
| `api_url` | FX rate API endpoint. Any JSON API returning `{"rate": float}`. Empty string disables API fetching. |
| `static_rate` | Fallback rate used when `api_url` is empty or the API is unreachable. `1.0` = no conversion. |

## [media]

Media download lifecycle.

```toml
[media]
ttl_hours = 24          # Downloaded media files older than this are deleted at startup (default: 24)
```

## [paths]

File paths for runtime state. All paths derive from `data_dir` by default.

```toml
[paths]
data_dir = "/data"                     # Root for all persistent state (env: LUCYD_DATA_DIR, default: /data)
state_dir = "/data"                    # Default: $data_dir
sessions_dir = "/data/sessions"        # Default: $data_dir/sessions
metering_db = "/data/metering.db"      # Default: $data_dir/metering.db (cost metering)
log_file = "/data/logs/lucyd.log"      # Default: $data_dir/logs/lucyd.log
```

All paths support `~` expansion. If individual paths are not set, they derive from `data_dir`. `LUCYD_DATA_DIR` env var overrides the TOML value.

## Plugin System (`plugins.d/`)

Plugins are Python files in `plugins.d/` exporting `TOOLS` (tool definitions) and/or `PREPROCESSORS` (attachment transformers). Tools are gated by `[tools] enabled` — only listed tools are registered. Preprocessors register unconditionally when the plugin loads. Plugins access their config via `config.raw()` — core never imports from `plugins.d/`.

See [Plugin & Channel Guide](plugins.md) for the full developer reference.

## Environment Variables

API keys are loaded from `.env` in the same directory as `lucyd.toml` (also loaded by the systemd unit via `EnvironmentFile`). The `.env` file uses `KEY=value` format, one per line.

| Variable | Purpose | Required |
|---|---|---|
| `LUCYD_DATA_DIR` | Root directory for all persistent state (default: `/data`) | No |
| `LUCYD_ANTHROPIC_KEY` | Anthropic API key (Claude models) | Yes (if using anthropic provider) |
| `LUCYD_TELEGRAM_TOKEN` | Telegram Bot API token | Yes (if using telegram channel) |
| `LUCYD_OPENAI_KEY` | OpenAI API key (embeddings) | For memory/embeddings |
| `LUCYD_BRAVE_KEY` | Brave Search API key | For web_search tool |
| `LUCYD_HTTP_TOKEN` | HTTP API bearer token | Required for all protected endpoints (unless `trust_localhost = true`) |

Environment variables take precedence over `.env` file values. The config loader reads `.env` first, then applies environment overrides.
