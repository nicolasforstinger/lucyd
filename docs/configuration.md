# Configuration Reference

All configuration lives in `lucyd.toml`. API keys are loaded from `.env` in the same directory.

## [agent]

Agent identity and workspace.

```toml
[agent]
name = "YourAgent"                         # Agent name (used in logs)
workspace = "~/.lucyd/workspace"           # Workspace root (personality files, skills, memory logs)
```

### [agent.context]

Files loaded into the system prompt, organized by cache tier. Stable files change rarely (cached aggressively). Semi-stable files change occasionally.

```toml
[agent.context]
stable = ["SOUL.md", "AGENTS.md", "USER.md", "IDENTITY.md", "TOOLS.md"]
semi_stable = ["MEMORY.md"]
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

## [channel]

Messaging transport configuration.

```toml
[channel]
type = "telegram"    # "telegram" or "cli"
debounce_ms = 500    # Collect messages from same sender within this window before processing
```

### [channel.telegram]

Telegram-specific settings. Only required when `type = "telegram"`.

```toml
[channel.telegram]
token_env = "LUCYD_TELEGRAM_TOKEN"           # Env var containing the bot token
allow_from = [123456789]                     # Allowed sender Telegram user IDs (numeric)
text_chunk_limit = 4000                      # Max chars per Telegram message (splits longer replies)
download_dir = "/tmp/lucyd-telegram"         # Directory for downloaded attachments
```

The bot token is loaded from the environment variable specified by `token_env` (default: `LUCYD_TELEGRAM_TOKEN`, set in `.env`). No external daemon is needed — Lucyd connects directly to the Telegram Bot API via httpx long polling.

Reconnect backoff parameters control retry behavior when the Telegram connection drops:

```toml
[channel.telegram]
reconnect_initial = 1.0      # Initial backoff delay (seconds, default: 1.0)
reconnect_max = 10.0          # Maximum backoff delay (seconds, default: 10.0)
reconnect_factor = 2.0        # Exponential multiplier (default: 2.0)
reconnect_jitter = 0.2        # Random jitter fraction (0.0–1.0, default: 0.2)
```

### [channel.telegram.contacts]

Name-to-ID mapping for outbound messages. Allows the agent to send messages using contact names instead of raw Telegram user IDs.

```toml
[channel.telegram.contacts]
Alice = 123456789
Bob = 987654321
```

Contact name lookup is case-insensitive.

## [http]

Optional HTTP API server for external integrations.

```toml
[http]
enabled = false              # Enable HTTP API (default: false)
host = "127.0.0.1"          # Listen address (default: 127.0.0.1 — localhost only)
port = 8100                  # Listen port (default: 8100)
token_env = "LUCYD_HTTP_TOKEN"  # Env var containing the bearer token
download_dir = "/tmp/lucyd-http"  # Temp dir for HTTP attachment downloads
max_body_bytes = 10485760    # Max request body size in bytes (default: 10 MB)
callback_url = ""            # Webhook URL — POST after every processed message (default: "" = disabled)
callback_token_env = ""      # Env var name containing the webhook bearer token (default: "" = no auth)
callback_timeout = 10        # Webhook callback timeout in seconds (default: 10)
rate_limit = 30              # Max requests per rate_window per sender (default: 30)
rate_window = 60             # Rate limit window in seconds (default: 60)
status_rate_limit = 60       # Max /status requests per rate_window (default: 60)
rate_limit_cleanup_threshold = 1000  # Evict stale rate limit entries above this count
```

Auth token is loaded from the environment variable named by `token_env` (default: `LUCYD_HTTP_TOKEN`). Webhook callback token is loaded from the env var named in `callback_token_env`. See [operations — HTTP API](operations.md#http-api) for endpoint details and [webhook callback](operations.md#webhook-callback) for the callback payload format.

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
type = "anthropic-compat"
api_key_env = "LUCYD_ANTHROPIC_KEY"

[models.primary]
model = "claude-sonnet-4-6"
max_tokens = 65536
max_context_tokens = 200000
cost_per_mtok = [3.0, 15.0, 0.3]   # [input, output, cache_read]
cache_control = true
thinking_enabled = true
thinking_mode = "adaptive"          # "adaptive" | "budgeted" | "disabled"

```

```toml
# providers.d/openai.toml
type = "openai-compat"
api_key_env = "LUCYD_OPENAI_KEY"
base_url = "https://api.openai.com/v1"

[models.embeddings]
model = "text-embedding-3-small"
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
| `cost_per_mtok` | Cost per million tokens as `[input, output, cache_read]` — used for cost tracking |
| `supports_vision` | Enable vision/image input for this model (default: `false` — must be declared per-model in provider files) |

**Provider-specific options:**

| Option | Provider | Purpose |
|---|---|---|
| `cache_control` | anthropic-compat | Enable prompt caching via cache tier metadata |
| `thinking_enabled` | anthropic-compat | Enable extended thinking (chain-of-thought) |
| `thinking_budget` | anthropic-compat | Max tokens for thinking block |
| `thinking_mode` | anthropic-compat | Thinking mode: `"adaptive"` (model decides depth), `"budgeted"` (uses `thinking_budget`), `"disabled"` |
| `thinking_effort` | anthropic-compat | Thinking effort level (empty = default) |
| `base_url` | openai-compat | API base URL |

## [memory]

Long-term memory configuration.

```toml
[memory]
db = "~/.lucyd/memory/main.sqlite"            # SQLite DB with FTS5 + embeddings
search_top_k = 10                             # Default result limit for memory searches
embedding_timeout = 15                        # Embedding API request timeout (seconds)
vector_search_limit = 10000                   # Raw DB query cap for vector search
fts_min_results = 3                           # FTS results threshold before vector fallback
```

The memory DB is optional. If the path is empty or the file does not exist, memory tools are not registered.

### [memory.consolidation]

Structured data extraction from session transcripts and workspace files.

```toml
[memory.consolidation]
enabled = true                        # Enable structured memory extraction
min_messages = 4                      # Minimum messages in session before extracting
confidence_threshold = 0.6            # Minimum confidence for extracted facts
max_extraction_chars = 50000          # Truncation limit for session text fed to extraction LLM
```

When enabled, `bin/lucyd-consolidate` (cron at `:15`) extracts facts, episodes, commitments, and entity aliases from session transcripts. Also triggers on session close and pre-compaction. See `memory_schema.py` for table definitions.

### [memory.maintenance]

Periodic cleanup of structured memory.

```toml
[memory.maintenance]
stale_threshold_days = 90             # Remove unaccessed facts older than this (default: 90)
```

Runs via `bin/lucyd-consolidate --maintain` (cron daily at `04:05`).

### [memory.evolution]

Daily rewriting of workspace understanding files using accumulated daily logs, structured memory, and an identity anchor file.

```toml
[memory.evolution]
enabled = true                        # Enable memory evolution (default: true)
model = "primary"                     # Model role to use (default: "primary")
files = ["MEMORY.md", "USER.md"]      # Workspace files to evolve (order matters — earlier files rewritten first)
anchor_file = "IDENTITY.md"           # Identity anchor — read but never modified
max_log_chars = 80000                 # Max chars of daily logs fed to evolution (default: 80000)
max_facts = 50                        # Max structured facts included in context (default: 50)
max_episodes = 20                     # Max episodes included in context (default: 20)
```

Triggered via `bin/lucyd-send --evolve` (cron daily at `04:20`, after maintenance) or `POST /api/v1/evolve`. Both paths queue a self-driven evolution message to the daemon. The agent loads an `evolution` skill from workspace, reads daily logs and current files, and rewrites the configured files through the full agentic loop with persona context.

### [memory.recall]

Controls how structured memory is injected into session context at startup.

```toml
[memory.recall]
decay_rate = 0.03                    # Time-decay factor for relevance scoring
max_facts_in_context = 20            # Maximum facts injected into context
max_dynamic_tokens = 1500            # Token budget for dynamic recall content (default: 1500; 0 = unlimited)
max_episodes_at_start = 3            # Maximum episodes injected at session start
```

Recall runs at session start and enriches `memory_search` results with structured data. Budget-aware: prioritizes commitments > vector > episodes > facts (under budget pressure, clinical facts drop first).

### [memory.recall.personality]

Config-driven priority and formatting for recall injection. Higher priority = kept longer under budget pressure.

```toml
[memory.recall.personality]
priority_vector = 35                 # Priority for vector search results (default: 35)
priority_episodes = 25               # Priority for episode blocks (default: 25)
priority_facts = 15                  # Priority for fact blocks (default: 15)
priority_commitments = 40            # Priority for commitment blocks (default: 40)
fact_format = "natural"              # "natural" (readable) or "compact"
show_emotional_tone = true           # Include emotional tone in episode display
episode_section_header = "Recent conversations"  # Header for episode section
synthesis_style = "structured"       # "structured" (raw), "narrative", or "factual"
synthesis_prompt_narrative = ""      # Prompt for narrative synthesis ({recall_text} placeholder). Required when synthesis_style = "narrative"
synthesis_prompt_factual = ""        # Prompt for factual synthesis ({recall_text} placeholder). Required when synthesis_style = "factual"
```

Drop order under budget pressure: facts (15) → episodes (25) → vector (35) → commitments (40).

**synthesis_style** controls how raw recall blocks are transformed before context injection:

| Style | Behavior | Use case |
|-------|----------|----------|
| `structured` | No transformation — raw blocks as-is (default) | Clinical agents, debugging |
| `narrative` | Temporal arc paragraph via subagent LLM call | Companions, creative agents |
| `factual` | Concise fact summary via subagent LLM call | Professional, business agents |

When set to `narrative` or `factual`, an LLM call rewrites the recall blocks before injection. Always uses the same model that handles the current message — no model mismatch. Falls back to raw blocks on any failure. Applies to both session-start recall and `memory_search` tool results.

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

The indexer (`bin/lucyd-index`) runs hourly at `:10` via cron. Incremental — skips files whose content hash hasn't changed.

## [tools]

Tool registration and execution settings.

```toml
[tools]
enabled = [
    "read", "write", "edit", "exec",
    "web_search", "web_fetch",
    "memory_search", "memory_get",
    "memory_write", "memory_forget", "commitment_update",
    "message", "react", "schedule_message", "list_scheduled",
    "session_status", "sessions_spawn", "load_skill",
    "tts",
]
output_truncation = 30000    # Truncate tool output beyond this many characters
plugins_dir = "plugins.d"   # Directory for custom tool plugins
exec_timeout = 120           # Default exec tool timeout (seconds)
exec_max_timeout = 600       # Maximum allowed exec timeout (seconds)
subagent_deny = ["sessions_spawn", "tts", "react", "schedule_message"]  # Tools denied to sub-agents
subagent_max_turns = 0       # Max turns for sub-agents (0 = use max_turns_per_message)
subagent_timeout = 0         # Timeout for sub-agents in seconds (0 = use agent_timeout_seconds)
```

The `subagent_deny` list controls which tools are blocked for sub-agents spawned via `sessions_spawn`. When omitted, the default deny-list applies: `sessions_spawn` (prevents recursion), `tts`, `react`, `schedule_message`. Sub-agents CAN load skills by default. Set to `[]` to allow all tools.

Tools are only registered if they appear in `enabled` AND their dependencies are met (e.g., `tts` requires `LUCYD_ELEVENLABS_KEY`, `memory_search` requires a configured `memory.db`).

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

### [tools.tts]

```toml
[tools.tts]
provider = "elevenlabs"                # TTS provider (required if tts enabled; empty = TTS disabled)
api_key_env = "LUCYD_ELEVENLABS_KEY"   # Env var for TTS API key
default_voice_id = "your-voice-id"     # Voice identifier
default_model_id = "eleven_v3"         # TTS model (provider-specific; ElevenLabs default: eleven_v3)
speed = 1.0                            # Speech speed
stability = 0.5                        # Voice stability (0.0–1.0)
similarity_boost = 0.75                # Voice similarity boost (0.0–1.0)
timeout = 60                           # API request timeout in seconds (default: 60)
api_url = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"  # TTS endpoint URL template ({voice_id} placeholder)
```

The `api_url` setting accepts a `{voice_id}` placeholder. When empty, the provider-specific default is used (e.g., ElevenLabs URL for `provider = "elevenlabs"`).

**API key resolution:** If `api_key_env` is set, the key is loaded from that environment variable. Otherwise, the provider name is looked up in the global API key map (e.g., `provider = "elevenlabs"` resolves `LUCYD_ELEVENLABS_KEY`). This allows custom TTS providers without modifying the framework's env var mapping.

### [tools.scheduling]

```toml
[tools.scheduling]
max_scheduled = 50     # Max concurrent scheduled messages (default: 50)
max_delay = 86400      # Max delay in seconds (default: 86400 = 24 hours)
```

## [stt]

Speech-to-text configuration for voice message transcription. Supports pluggable backends.

```toml
[stt]
backend = "openai"                           # "openai" or "local" (required if voice messages enabled; empty = transcription disabled)
voice_label = "voice message"                # Label prefixed to transcriptions: "[voice message]: ..."
voice_fail_msg = "voice message — transcription failed"  # Label on failure
audio_label = "audio transcription"          # Label for non-voice audio files: "[audio transcription]: ..."
audio_fail_msg = "audio transcription — failed"  # Label when audio file transcription fails
```

**OpenAI backend** (default):

```toml
[stt.openai]
api_url = "https://api.openai.com/v1/audio/transcriptions"    # Whisper API endpoint
model = "whisper-1"                                            # Whisper model identifier
timeout = 60                                                   # Request timeout (seconds)
```

Requires `LUCYD_OPENAI_KEY`.

**Local backend** (whisper.cpp server):

```toml
[stt.local]
endpoint = "http://whisper-server:8082/inference"    # whisper.cpp HTTP inference endpoint
language = "auto"                                     # Language hint (or "auto" for detection)
ffmpeg_timeout = 30                                   # Timeout for ffmpeg audio conversion (seconds)
request_timeout = 60                                  # Timeout for whisper.cpp HTTP request (seconds)
```

The local backend converts audio to WAV (16kHz mono) via ffmpeg before sending to the whisper.cpp server. Requires `ffmpeg` installed on the system.

## [documents]

Document attachment processing. Extracts text from attachments (PDF, text files) so the agent sees content, not just `[attachment: file, type]` labels. PDF support requires `pypdf`.

```toml
[documents]
enabled = true                  # Enable document text extraction (default: true)
max_chars = 30000               # Truncation limit for extracted text (default: 30000)
max_file_bytes = 10485760       # Skip files larger than this (default: 10 MB)
text_extensions = [
    ".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml",
    ".html", ".htm", ".py", ".js", ".ts", ".sh", ".toml",
    ".ini", ".cfg", ".log", ".sql", ".css",
]
```

Files are matched by extension (for text) or MIME type (for PDF). Non-extractable formats fall through to label-only.

## [vision]

Image processing settings for inbound images.

```toml
[vision]
max_image_bytes = 5242880              # Skip inbound images larger than this (bytes, default 5 MB)
max_dimension = 1568                   # Max px on longest side (default: 1568)
default_caption = "image"              # Default caption for image attachments
too_large_msg = "image too large to display"  # Message when image exceeds size limit
jpeg_quality_steps = [85, 60, 40]      # JPEG quality reduction steps for fitting oversized images
caption_max_chars = 200                # Max chars from response embedded in image caption after processing
```

When an image exceeds `max_image_bytes`, the daemon tries dimension scaling first, then iterates through `jpeg_quality_steps` to reduce JPEG quality. If the image still exceeds the limit after all steps (e.g., PNG which can't be quality-reduced), the `too_large_msg` fallback is used.

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
audit_truncation_limit = 500                                           # Max chars per message in session audit truncation (default: 500)
queue_capacity = 1000                                                  # Max messages in the async queue (default: 1000)
queue_poll_interval = 1.0                                              # Queue poll cycle in seconds (default: 1.0)
quote_max_chars = 200                                                  # Max chars for quoted reply context (default: 200)
sqlite_timeout = 30                                                    # SQLite connection timeout in seconds for all DBs (default: 30)
primary_sender = "Nicolas"                                             # Route all notifications to this sender's session (default: "" = disabled)
passive_notify_refs = ["sensor-data"]                                  # Notification refs buffered passively without LLM calls (default: [])
telemetry_max_age = 30.0                                               # Max age in seconds for buffered telemetry injection (default: 30.0)
```

**`primary_sender`:** When set, all notifications (`/notify`, `--notify`) route to the named sender's session instead of creating throwaway sessions per source. Keeps notification context in the agent's main conversation. Empty string disables (backward compatible).

**`passive_notify_refs`:** List of notification `ref` values that are buffered at latest-value-per-ref without triggering an LLM call. Buffered entries are injected as `[telemetry: ...]` context on the next real message (max 30s age). Notifications with `data.priority = "active"` bypass the buffer. Use for high-frequency telemetry that would be prohibitively expensive to process individually (e.g., sensor readings every 5 seconds).

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
verify_enabled = true        # Post-hoc verification: structural + entity grounding (default: true)
verify_max_turn_labels = 3   # Max dialogue turn patterns before rejection (default: 3)
verify_grounding_threshold = 0.5  # Min fraction of distinctive tokens grounded in source (default: 0.5)
prompt = "..."               # Compaction prompt (supports {agent_name}, {max_tokens}). See lucyd.toml.example
diary_prompt = "..."         # Diary prompt for forced compact (supports {date}). See lucyd.toml.example
```

Compaction takes the oldest messages (1 - `keep_recent_pct`), summarizes them, and replaces them with the summary. The JSONL audit trail retains the full history.

**Verification (`verification.py`):** When `verify_enabled = true`, compaction summaries are checked post-hoc for fabrication. Tier 1 (structural) rejects summaries with more than `verify_max_turn_labels` dialogue turn patterns (e.g., "User:", "Assistant:"). Tier 2 (entity grounding) rejects summaries where less than `verify_grounding_threshold` fraction of distinctive tokens appear in the source transcript. Failed verification falls back to a deterministic summary. Zero LLM cost.

**Forced compact:** `lucyd-send --compact` / `POST /api/v1/compact` sends a diary prompt to the primary session. The agent writes a daily memory log via the `write` tool, then compaction fires regardless of token threshold.

## [logging]

Log file rotation settings.

```toml
[logging]
max_bytes = 10485760    # Max log file size before rotation (default: 10 MB)
backup_count = 3        # Number of rotated backups to keep (default: 3)
suppress = ["httpx", "httpcore", "anthropic", "openai"]  # Third-party loggers suppressed to WARNING
```

## [paths]

File paths for runtime state.

```toml
[paths]
state_dir = "~/.lucyd"
sessions_dir = "~/.lucyd/sessions"
cost_db = "~/.lucyd/cost.db"
log_file = "~/.lucyd/lucyd.log"
```

All paths support `~` expansion.

## Plugin System (`plugins.d/`)

Custom tools are loaded from Python files in the `plugins.d/` directory (relative to the `lucyd.toml` location).

Each plugin is a `.py` file exporting a `TOOLS` list (same format as built-in tool modules):

```python
# plugins.d/my_tool.py

def tool_my_action(param: str) -> str:
    """Do something custom."""
    return f"Done: {param}"

TOOLS = [
    {
        "name": "my_action",
        "description": "Custom action tool.",
        "input_schema": {
            "type": "object",
            "properties": {
                "param": {"type": "string", "description": "Input parameter"},
            },
            "required": ["param"],
        },
        "function": tool_my_action,
    },
]
```

An optional `configure()` function receives dependencies by parameter name (e.g. `config`, `channel`, `session_mgr`, `provider`).

Plugin tools must be listed in `[tools] enabled` to activate. Unlisted plugin tools are ignored. A failing plugin does not block other plugins or built-in tools from loading.

## Environment Variables

API keys are loaded from `.env` in the same directory as `lucyd.toml` (also loaded by the systemd unit via `EnvironmentFile`). The `.env` file uses `KEY=value` format, one per line.

| Variable | Purpose | Required |
|---|---|---|
| `LUCYD_ANTHROPIC_KEY` | Anthropic API key (Claude models) | Yes (if using anthropic-compat provider) |
| `LUCYD_TELEGRAM_TOKEN` | Telegram Bot API token | Yes (if using telegram channel) |
| `LUCYD_OPENAI_KEY` | OpenAI API key (embeddings) | For memory/embeddings |
| `LUCYD_BRAVE_KEY` | Brave Search API key | For web_search tool |
| `LUCYD_ELEVENLABS_KEY` | ElevenLabs API key | For tts tool |
| `LUCYD_HTTP_TOKEN` | HTTP API bearer token | For HTTP API (if `[http] enabled = true`) |

Environment variables take precedence over `.env` file values. The config loader reads `.env` first, then applies environment overrides.
