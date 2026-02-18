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

### [agent.context.tiers]

Override file lists for non-default context tiers. When `lucyd-send --tier operational` is used, these files replace the defaults. See [architecture — cache tiers](architecture.md#cache-tiers) for the caching strategy.

```toml
[agent.context.tiers]
operational.stable = ["SOUL.md", "AGENTS.md", "IDENTITY.md"]
operational.semi_stable = ["HEARTBEAT.md"]
```

Unspecified tiers fall through to empty file lists.

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
```

Auth token is loaded from the `LUCYD_HTTP_TOKEN` environment variable. See [operations — HTTP API](operations.md#http-api) for endpoint details.

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

[models.subagent]
model = "claude-haiku-4-5-20251001"
max_tokens = 4096
cost_per_mtok = [1.0, 5.0, 0.1]

[models.compaction]
model = "claude-sonnet-4-6"
max_tokens = 4096
cost_per_mtok = [3.0, 15.0, 0.3]
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

Model names (`primary`, `subagent`, `compaction`, `embeddings`) are referenced by routing rules, behavior settings, and the `sessions_spawn` tool. No alias table -- model IDs resolve directly to API calls.

**Common options (all providers):**

| Option | Purpose |
|---|---|
| `model` | Model identifier passed to the provider API (e.g., `"claude-sonnet-4-6"`) |
| `max_tokens` | Maximum output tokens per API call |
| `max_context_tokens` | Maximum input context window size (used by `session_status` tool for context % display) |
| `cost_per_mtok` | Cost per million tokens as `[input, output, cache_read]` — used for cost tracking |
| `supports_vision` | Enable vision/image input for this model (default: `true`) |

**Provider-specific options:**

| Option | Provider | Purpose |
|---|---|---|
| `cache_control` | anthropic-compat | Enable prompt caching via cache tier metadata |
| `thinking_enabled` | anthropic-compat | Enable extended thinking (chain-of-thought) |
| `thinking_budget` | anthropic-compat | Max tokens for thinking block |
| `thinking_mode` | anthropic-compat | Thinking mode: `"adaptive"` (model decides depth), `"budgeted"` (uses `thinking_budget`), `"disabled"` |
| `thinking_effort` | anthropic-compat | Thinking effort level (empty = default) |
| `base_url` | openai-compat | API base URL |

## [routing]

Maps message source to model name. The source is determined by the channel or message type.

```toml
[routing]
telegram = "primary"   # Telegram messages use the primary model
cli = "primary"        # CLI messages use the primary model
system = "subagent"    # System events (lucyd-send --system) use the subagent model
http = "primary"       # HTTP API messages use the primary model
```

Unmapped sources default to `"primary"`.

## [memory]

Long-term memory configuration.

```toml
[memory]
db = "~/.lucyd/memory/main.sqlite"            # SQLite DB with FTS5 + embeddings
search_top_k = 10                             # Default result limit for memory searches
```

The memory DB is optional. If the path is empty or the file does not exist, memory tools are not registered.

## [tools]

Tool registration and execution settings.

```toml
[tools]
enabled = [
    "read", "write", "edit", "exec",
    "web_search", "web_fetch",
    "memory_search", "memory_get",
    "message", "react", "schedule_message", "list_scheduled",
    "session_status", "sessions_spawn", "load_skill",
    "tts",
]
output_truncation = 30000    # Truncate tool output beyond this many characters
exec_timeout = 120           # Default exec tool timeout (seconds)
exec_max_timeout = 600       # Maximum allowed exec timeout (seconds)
```

Tools are only registered if they appear in `enabled` AND their dependencies are met (e.g., `tts` requires `LUCYD_ELEVENLABS_KEY`, `memory_search` requires a configured `memory.db`).

### [tools.filesystem]

```toml
[tools.filesystem]
allowed_paths = ["~/.lucyd/workspace", "/tmp/"]    # Path prefixes the agent can read/write (defaults to workspace + /tmp/)
```

### [tools.web_search]

```toml
[tools.web_search]
provider = "brave"    # Web search provider (currently only "brave")
```

### [tools.whisper]

```toml
[tools.whisper]
api_url = "https://api.openai.com/v1/audio/transcriptions"    # Whisper API endpoint
model = "whisper-1"                                            # Whisper model identifier
timeout = 60                                                   # Request timeout (seconds)
```

Requires `LUCYD_OPENAI_KEY`. Used to transcribe audio attachments received via Telegram or other channels.

### [tools.tts]

```toml
[tools.tts]
provider = "elevenlabs"                # TTS provider (currently only "elevenlabs")
default_voice_id = "your-voice-id"     # ElevenLabs voice identifier
default_model_id = "eleven_v3"         # ElevenLabs model
speed = 1.0                            # Speech speed
stability = 0.5                        # Voice stability (0.0–1.0)
similarity_boost = 0.75                # Voice similarity boost (0.0–1.0)
```

## [behavior]

Runtime behavior tuning.

```toml
[behavior]
silent_tokens = ["HEARTBEAT_OK", "NO_REPLY"]                           # Replies starting/ending with these are not delivered
typing_indicators = true                                               # Send typing indicator before processing
error_message = "I'm having trouble connecting right now. Try again in a moment."  # Sent when agentic loop fails
agent_timeout_seconds = 600                                            # Timeout per API call in the agentic loop
max_turns_per_message = 50                                             # Max tool-use iterations per inbound message
max_cost_per_message = 5.0                                             # USD circuit breaker per message (0.0 = disabled)
max_image_bytes = 5242880                                              # Skip inbound images larger than this (bytes, default 5 MB)
```

### [behavior.compaction]

Session compaction (summarization of old messages to free context window).

```toml
[behavior.compaction]
threshold_tokens = 150000    # Trigger compaction when last input_tokens exceeds this
model = "compaction"         # Model name from [models.*] to use for summarization
prompt = "Summarize this conversation preserving all factual details, decisions, action items, and emotional context."
```

Compaction takes the oldest 2/3 of messages, summarizes them, and replaces them with the summary. The JSONL audit trail retains the full history.

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
