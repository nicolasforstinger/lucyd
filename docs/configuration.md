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

## [user]

The single user the agent serves. `[user] name` is required and pins the `sender` for `talker=user` inbound messages — session key is always `f"user:{name}"`. `[user] timezone` (IANA name, default `UTC`) is the wall-clock zone the scheduling tools (`remind_user` / `schedule_self_task`) interpret an absolute `when` in, so the model never does timezone/offset math.

```toml
[user]
name = "YourName"
timezone = "Europe/Vienna"
```

## [database]

PostgreSQL connection. Required: the daemon needs a DB for sessions, memory, and metering.

```toml
[database]
url_env = "LUCYD_DATABASE_URL"   # Env var holding the asyncpg DSN (postgres://user:pass@host:port/db)
pool_min = 2                      # Minimum pool connections (default: 2)
pool_max = 10                     # Maximum pool connections (default: 10)
```

Schema is applied at startup from `schema/*.sql` (forward-only, version tracked in `public.schema_version`). pgvector extension is created automatically.

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

Channel bridges are standalone processes that read their config from the same `lucyd.toml` as the daemon (path via the `LUCYD_CONFIG` env var). Each bridge owns its own section.

| Bridge | Section in `lucyd.toml` | Bot/login secret env var | Inbound endpoint | Outbound port |
|--------|--------|------|------|------|
| Telegram | `[telegram]` | `LUCYD_TELEGRAM_TOKEN` | `POST /api/v1/inbound/telegram` | `127.0.0.1:8101` |
| Email | `[email]` | depends on backend (Proton bridge / SMTP creds) | `POST /api/v1/inbound/email` | `127.0.0.1:8102` |

Both bridges authenticate to the daemon with `LUCYD_HTTP_TOKEN`. The outbound `POST /send` listener uses the same token. The `debounce_ms` setting in `[behavior]` controls message batching for queued messages on the daemon side.

### `[telegram.contacts]` format

Contact entries map a name to a Telegram user/chat id. **The format is
`name = chat_id`, not `chat_id = name`** — TOML keys are strings, but the
loader expects an integer chat_id as the value (so `ID_TO_NAME[chat_id] = name`
matches against `user_id` from the Telegram API):

```toml
[telegram.contacts]
Nicolas = 8211983408     # correct: name (string key) = chat_id (int value)
# 8211983408 = "Nicolas" # WRONG: this writes ID_TO_NAME["Nicolas"] = "8211983408",
                         # contact lookup against user_id (int) silently always fails.
```

## [bridges]

Selects which bridge is the active outbound target for proactive
messages from the agent (via `send_message` tool or
`POST /api/v1/outbound/send`).

```toml
[bridges]
primary = "telegram"   # one of: "telegram", "email" (no fanout — single primary)
```

Each bridge runs a `POST /send` listener on a conventional localhost
port (telegram=8101, email=8102). The daemon's `bridge_client.send_to_user`
calls the listener of whichever bridge is `primary`. Per-bridge
attachment caps are hardcoded in `bridge_client.BRIDGE_LIMITS` (telegram:
50 MB, email: 20 MB) — when an attachment exceeds the cap, the
`send_message` tool returns an actionable error directing the agent to
move the file to `/mnt/share/` and link to it in text instead.

If `[bridges]` is absent or `primary` is empty, `send_message` and
`/api/v1/outbound/send` fail with "no primary bridge configured" — they
do not silently swallow.

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

**`trust_localhost`:** When `true`, requests from `127.0.0.1` / `::1` bypass auth (no token required). Default is `false` — all requests require a bearer token. When `false`, bridges and the entrypoint cron jobs must present a valid token via `LUCYD_HTTP_TOKEN`.

Endpoint definitions live in `api.py` (`HTTPApi.start()`).

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
search_top_k = 10                             # Default result limit for memory searches
embedding_timeout = 15                        # Embedding API request timeout (seconds)
vector_search_limit = 10000                   # Raw DB query cap for vector search
```

Memory uses the shared PostgreSQL database configured in `[database]`. Memory tools are registered when a database connection is available.

### [memory.consolidation]

Structured data extraction from session transcripts and workspace files.

```toml
[memory.consolidation]
enabled = true                        # Enable structured memory extraction (code default: false)
confidence_threshold = 0.6            # Minimum confidence for extracted facts
```

When enabled, the hourly cron at `:15` calls `POST /api/v1/consolidate` to extract facts, episodes, commitments, and entity aliases from workspace files. Also triggers on session close and pre-compaction. Schema lives in `schema/001_initial.sql` (`knowledge` schema).

### [memory.maintenance]

Periodic cleanup of structured memory (the mechanical half of `POST /api/v1/maintain`).

```toml
[memory.maintenance]
stale_threshold_days = 90             # Remove unaccessed facts older than this (default: 90)
```

Runs on every `POST /api/v1/maintain` call (cron hourly at `:35`), independent of the `[maintain]` heartbeat gate below.

### [maintain]

The self-maintenance heartbeat. When enabled, the hourly `POST /api/v1/maintain` cron dispatches a periodic `system:maintenance` LLM turn that reads the agent's workspace `MAINTAIN.md` and tends its own memory (diary, MEMORY.md, USER.md, notes, structured facts) with full tool access on the primary model. The pass fires only when the elapsed time since the last pass exceeds a randomized interval in `[interval_min_minutes, interval_max_minutes]`; the marker lives in `/data/maintain/state.json`.

```toml
[maintain]
enabled = false                       # Master switch (default: false — no LLM cost until on)
interval_min_minutes = 240            # Earliest the next pass may fire (default: 240)
interval_max_minutes = 480            # Latest the next pass may fire (default: 480)
idle_minutes = 360                    # Reported in the brief; the agent gates its reach-out on it (default: 360)
```

The pass brief is `MAINTAIN.md` preceded by a generated header: current local time, last-pass marker, the diff of changed workspace files + facts created since the last pass, how long since the user last messaged, and the ask-ledger path (`notes/maintenance-log.md`). The agent decides per its protocol whether to fix anything or reach out — most passes end quiet. `MAINTAIN.md` is one of the operator-owned files the agent cannot write (see [tools.filesystem]).

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

The indexer runs hourly at `:10` via cron (`POST /api/v1/index`). Incremental — skips files whose content hash hasn't changed.

## [tools]

Tool registration and execution settings.

```toml
[tools]
enabled = [
    "read", "write", "edit", "send_file",
    "exec",
    "web_search", "web_fetch",
    "memory_search", "memory_get",
    "memory_write", "memory_forget", "commitment_update",
    "session_status", "sessions_spawn", "load_skill",
    "remind_user", "schedule_self_task", "list_scheduled", "cancel_scheduled", "send_message",
    "gdpr_search", "gdpr_redact", "pdf_read",
]
output_truncation = 30000        # Truncate tool output beyond this many characters
plugins_dir = "plugins.d"        # Directory for custom tool plugins
exec_timeout = 120               # Default exec tool timeout (seconds)
exec_max_timeout = 600           # Maximum allowed exec timeout (seconds)
subagent_deny = ["sessions_spawn"]   # Tools denied to sub-agents (default: [])
subagent_max_turns = 0           # Max turns for sub-agents (0 = use max_turns_per_message)
subagent_timeout = 0             # Timeout for sub-agents in seconds (0 = use agent_timeout_seconds)
tool_call_retry = false          # Retry tool calls with guidance when args are invalid (default: false)
```

The `subagent_deny` list controls which tools are blocked for sub-agents spawned via `sessions_spawn`. Recommended value: `["sessions_spawn"]` to prevent recursion. Set to `[]` to allow all tools. Sub-agents can load skills by default.

Tools are only registered if they appear in `enabled` AND their dependencies are met (e.g., `memory_search` requires a configured database).

### [tools.filesystem]

```toml
[tools.filesystem]
allowed_paths = ["~/.lucyd/workspace", "/tmp/"]    # Path prefixes the agent can read/write (defaults to workspace + /tmp/)
default_read_limit = 2000                          # Max lines returned by the read tool (default: 2000)
```

**Operator-owned write-guard.** The `write` and `edit` tools unconditionally refuse any file whose basename is `SOUL.md`, `AGENTS.md`, `TOOLS.md`, or `MAINTAIN.md` — the agent's identity and protocol files. The refusal is independent of `allowed_paths` (it fires even for a path that would otherwise be allowed) and applies in every session, including the maintenance pass. These files are operator-managed; the agent raises changes with the operator rather than editing them directly. Reading them is unaffected.

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

Document attachment processing. When enabled, extracts text from non-PDF attachments inline so the agent sees content rather than just an attachment label. PDFs are always handed to the agent as a label; the agent uses the `pdf_read` tool for explicit, page-controlled extraction (requires `pypdf`).

```toml
[documents]
enabled = true                  # Enable inline text extraction for non-PDF docs (code default: false)
max_chars = 30000               # Truncation limit for extracted text (default: 30000)
max_file_bytes = 10485760       # Skip files larger than this (default: 10 MB)
text_extensions = [
    ".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml",
    ".html", ".htm", ".py", ".js", ".ts", ".sh", ".toml",
    ".ini", ".cfg", ".log", ".sql", ".css",
]
```

Files are matched by extension. Non-extractable formats fall through to label-only.

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
debounce_ms = 500                                                     # Message batching window in ms (group rapid consecutive inbound messages)
# max_context_for_tools = 0                                             # Inject wrap-up hint when context exceeds this during tool use (0 = disabled)
# thinking_concise_hint = false                                         # Inject "respond concisely" hint after tool results to reduce thinking overhead
```

**Two-tier retry architecture:** `api_retries` handles transient errors (429, 5xx, connection) within a single agentic loop call (fast, 1–8s backoff). `message_retries` retries the entire message processing when the agentic loop fails after exhausting API retries (slower, 30–60s backoff with jitter).

### [behavior.compaction]

Session compaction (summarization of old messages to free context window).

```toml
[behavior.compaction]
threshold_tokens = 150000    # Trigger compaction when last input_tokens exceeds this (default: 150000)
keep_recent_pct = 0.33       # Keep newest fraction of messages verbatim (default: 0.33; adaptive to 0.5 if max_context_tokens <= 32k)
keep_recent_pct_min = 0.05   # Floor for keep_recent_pct (default: 0.05)
keep_recent_pct_max = 0.9    # Ceiling for keep_recent_pct (default: 0.9)
max_tokens = 2048            # Max output tokens for compaction summary (default: 2048)
prompt = "..."               # Compaction prompt (supports {agent_name}, {max_tokens}). See lucyd.toml.example
diary_prompt = "..."         # Diary prompt for forced compact (supports {date}). See lucyd.toml.example
```

Compaction takes the oldest messages (1 - `keep_recent_pct`), summarizes them via the `compaction` model role, and replaces them with the summary. The PostgreSQL `sessions.events` table retains the full audit trail.

A context-pressure warning is injected at 80% of `threshold_tokens` and `tool_result_max_chars` (2000) is enforced on tool results kept in compacted context — both are framework constants, not config knobs.

**Forced compact:** `POST /api/v1/compact` sends the `diary_prompt` to the primary user session. The agent writes a daily memory log via the `write` tool, then compaction fires regardless of token threshold.

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
retention_months = 84      # Delete metering records older than this via POST /api/v1/maintain (default: 84 — 7 years for BAO §132 compliance)
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

Media download lifecycle. There is no configurable `[media]` section — downloaded
media files are swept at startup with a hardcoded 24-hour TTL
(`lucyd.py::_sweep_expired_media`).

## [paths]

File paths for runtime state. All paths derive from `data_dir` by default.

```toml
[paths]
data_dir = "/data"                     # Root for all persistent state (env: LUCYD_DATA_DIR, default: /data)
state_dir = "/data"                    # Default: $data_dir
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
| `LUCYD_DATABASE_URL` | PostgreSQL DSN consumed via `[database] url_env` | Yes (sessions/memory/metering all live in PG) |
| `LUCYD_HTTP_TOKEN` | HTTP API bearer token | Required for all protected endpoints (unless `trust_localhost = true`) |
| `LUCYD_ANTHROPIC_KEY` | Anthropic API key (Claude models) | Yes (if using anthropic provider) |
| `LUCYD_OPENAI_KEY` | OpenAI API key (embeddings + GPT models if loaded) | For memory/embeddings |
| `LUCYD_MISTRAL_KEY` | Mistral API key | If using mistral provider |
| `LUCYD_BRAVE_KEY` | Brave Search API key | For `web_search` tool |
| `LUCYD_TELEGRAM_TOKEN` | Telegram Bot API token | Yes (if using telegram bridge) |
| `LUCYD_ELEVENLABS_KEY` | ElevenLabs API key | If using `plugins.d/elevenlabs.py` (TTS) |
| `LUCYD_CONFIG` | Path to `lucyd.toml` (used by bridges to locate the same config the daemon reads) | Yes for bridges |

Environment variables take precedence over `.env` file values. The config loader reads `.env` first, then applies environment overrides.
