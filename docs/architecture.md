# Architecture

How the Lucyd codebase fits together. Read this when you need to fix something, add a feature, or understand why a message didn't arrive.

## Module Map

| File | Purpose |
|---|---|
| `lucyd.py` | Entry point. CLI args, daemon lifecycle, PID file, FIFO reader, signal handlers, message loop. |
| `agentic.py` | Provider-agnostic tool-use loop. Calls LLM, executes tool calls, loops until done. Records costs. |
| `config.py` | Loads `lucyd.toml`, applies `.env` and environment overrides, validates, provides typed access. Immutable after load. |
| `context.py` | Builds system prompt blocks from workspace files. Organizes into cache tiers (stable/semi_stable/dynamic). |
| `session.py` | Session manager. Dual storage: JSONL audit trail (append-only) + state file (atomic snapshot). Handles compaction. |
| `skills.py` | Scans workspace skills directory. Parses markdown with YAML frontmatter. Builds index for system prompt. |
| `memory.py` | Long-term memory. SQLite FTS5 for keyword search, pluggable embeddings for vector similarity. FTS-first strategy. Gracefully degrades to FTS5-only when no embedding provider is configured. Also handles structured recall (facts, episodes, commitments). |
| `memory_schema.py` | Schema management for all memory tables (10 tables: 4 unstructured + 4 structured + 2 infrastructure). Safe to call on every startup — all `IF NOT EXISTS`. |
| `consolidation.py` | Structured data extraction from sessions and workspace files via LLM. Extracts facts, episodes, commitments, and entity aliases. |
| `synthesis.py` | Memory recall synthesis. Transforms raw recall blocks into prose (narrative/factual) before context injection. Optional — defaults to passthrough ("structured"). |
| `channels/__init__.py` | Channel protocol definition (`connect`, `receive`, `send`, `send_typing`, `send_reaction`) and factory. |
| `channels/telegram.py` | Telegram transport. Long polling via Bot API (httpx), sends via Bot API HTTP methods. |
| `channels/cli.py` | stdin/stdout transport for testing. |
| `channels/http_api.py` | HTTP API server. REST endpoints for external integrations (chat, notify, status, sessions, cost). |
| `providers/__init__.py` | LLM provider protocol (`format_tools`, `format_system`, `format_messages`, `complete`) and factory. |
| `providers/anthropic_compat.py` | Anthropic provider. Handles cache control, extended thinking, thinking block preservation. |
| `providers/openai_compat.py` | OpenAI-compatible provider. Used for embeddings and any OpenAI-compatible API. |
| `tools/__init__.py` | ToolRegistry. Registration, dispatch, error isolation, output truncation. |
| `tools/filesystem.py` | `read`, `write`, `edit` tools. |
| `tools/shell.py` | `exec` tool. Subprocess with configurable timeout. |
| `tools/messaging.py` | `message` and `react` tools. Sends messages and emoji reactions via the active channel. |
| `tools/web.py` | `web_search` (Brave), `web_fetch` tools. |
| `tools/memory_tools.py` | `memory_search`, `memory_get` tools. Delegates to `memory.py`. |
| `tools/structured_memory.py` | `memory_write`, `memory_forget`, `commitment_update` tools. Structured fact storage with parameterized SQL. |
| `tools/agents.py` | `sessions_spawn` tool. Runs a sub-agent with scoped tools and a separate model. |
| `tools/skills_tool.py` | `load_skill` tool. Returns a skill's full body on demand. |
| `tools/status.py` | `session_status` tool. Returns uptime, today's cost, token counts. |
| `tools/scheduling.py` | `schedule_message` and `list_scheduled` tools. Asyncio timer-based delayed message delivery. |
| `tools/tts.py` | `tts` tool. Text-to-speech via ElevenLabs API. |
| `tools/indexer.py` | Memory indexer. Scans workspace, chunks files, embeds via configurable provider, writes to SQLite FTS5 + vector DB. Used by `bin/lucyd-index`. Provider-agnostic — embedding model, base URL, and provider are set via `configure()`. |
| `bin/lucyd-send` | CLI script. Writes JSON to the control FIFO. Queries cost DB and monitor state directly. |
| `bin/lucyd-index` | Memory indexer CLI. Scans workspace, chunks, embeds, writes to SQLite FTS5 + vector DB. Cron at `:10`. |
| `bin/lucyd-consolidate` | Memory consolidation CLI. Extracts structured facts/episodes/commitments from sessions. Cron at `:15`. |
| `providers.d/*.toml` | Provider config files. Each defines connection type, API key env var, and `[models.*]` sections. Loaded via `[providers] load` in `lucyd.toml`. |

## Message Flow

How an inbound message travels through the system:

```
1. Channel (Telegram long polling / stdin)
   |
2. channel.receive() yields InboundMessage
   |
3. Queue (asyncio.Queue)
   |  <- FIFO reader also pushes dict messages here
   |
4. Message loop (debounce, combine same-sender)
   |
5. _process_message()
   |
   +-- Route to model (config.route_model(source) -> model name)
   +-- Get/create session (SessionManager.get_or_create(sender))
   +-- Add user message to session
   +-- Build system prompt (ContextBuilder.build(tier, source))
   +-- Send typing indicator (if enabled, if not system source)
   |
6. Agentic loop (run_agentic_loop)
   |
   +-- Write monitor state: "thinking" (before loop, on_tool_results)
   +-- provider.complete(system, messages, tools) -> LLMResponse
   +-- Record cost to SQLite
   +-- on_response callback → write monitor state + turn history
   +-- If stop_reason == "tool_use":
   |     Write monitor state: "tools" (with tool names)
   |     Execute each tool call via ToolRegistry.execute()
   |     Append results to messages
   |     on_tool_results callback → write monitor state: "thinking"
   |     Loop back to provider.complete()
   +-- If stop_reason == "end_turn" or "max_tokens":
   |     Write monitor state: "idle"
   |     Return final response
   |
7. Persist session (JSONL + state)
   |
8. Silent token check (suppress if reply matches)
   |
9. Deliver reply via channel.send()
   |
10. Check compaction threshold (summarize if needed)
```

## Agentic Loop

The core loop in `agentic.py` is provider-agnostic:

1. Format messages for the provider (`provider.format_messages()`)
2. Call `provider.complete(system, messages, tools)` with timeout
3. Record cost if `cost_db` and `cost_rates` are set
4. Append the assistant's response to the message list
5. If `stop_reason != "tool_use"` or no tool calls: return the response
6. Execute each tool call via `ToolRegistry.execute(name, arguments)`
7. Append tool results to messages as `{"role": "tool_results", "results": [...]}`
8. Loop back to step 1

The loop runs up to `max_turns` iterations (default: 50). Each iteration is a single LLM API call plus its tool executions.

## Context Building

The `ContextBuilder` assembles system prompt blocks with cache tier metadata. Providers use these tiers for prompt caching optimization.

### Cache Tiers

| Tier | Content | Cache behavior |
|---|---|---|
| `stable` | Personality files (SOUL.md, AGENTS.md, etc.), tool descriptions | Rarely changes. Cached aggressively by the provider. |
| `semi_stable` | Memory file (MEMORY.md), always-on skill bodies, skill index | Changes occasionally. Cached with shorter TTL. |
| `dynamic` | Current date/time, extra runtime metadata | Changes every turn. Never cached. |

### Context Tier Overrides

Different message types can use different file sets. The `[agent.context.tiers]` config maps tier names to file lists:

- `full` (default): All stable + semi_stable files
- `operational`: Reduced set for system events (e.g., SOUL.md + AGENTS.md + HEARTBEAT.md)
- Undefined tiers fall back to empty file lists

The tier is selected per-message: user messages default to `full`, system events default to `operational`, and `lucyd-send --tier` can override explicitly.

### Source-Aware Dynamic Context

The context builder receives the message `source` (e.g., `"telegram"`, `"system"`, `"user"`)
and includes source-specific framing in the dynamic block. This tells the LLM what kind of
session it's in:

| Source | Dynamic context |
|--------|----------------|
| `"system"` | Session type annotation: automated infrastructure, replies not delivered |
| `"http"` | Session type annotation: HTTP API integration, replies returned via HTTP response |
| `"telegram"` | (none — default conversational context) |
| `"user"` | (none — default conversational context) |

This completes the `source` chain: routing → tier → suppression → context awareness.

### HTTP API

An optional HTTP server (`channels/http_api.py`) runs alongside the primary channel, providing REST endpoints for external integrations.

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/v1/chat` | POST | Synchronous: send message, await agentic loop response |
| `/api/v1/notify` | POST | Fire-and-forget: queue event, return 202 immediately |
| `/api/v1/status` | GET | Health check + daemon stats (uptime, cost, sessions) |
| `/api/v1/sessions` | GET | List active sessions |
| `/api/v1/cost` | GET | Cost breakdown by period (`?period=today\|week\|all`) |
| `/api/v1/monitor` | GET | Live agentic loop state (model, contact, turn) |
| `/api/v1/sessions/reset` | POST | Reset sessions by target (all, contact name, UUID) |
| `/api/v1/sessions/{id}/history` | GET | Session event history (`?full=true` for tool calls) |

**Design**: HTTP is not a Channel implementation. It feeds messages directly into the daemon's `asyncio.Queue` alongside Telegram and FIFO. For `/chat`, an `asyncio.Future` is attached to the queue item; `_process_message` resolves it with the reply. For `/notify`, no Future — the event is queued and the caller gets 202 immediately.

**Auth**: Bearer token on every request. Token loaded from `LUCYD_HTTP_TOKEN` environment variable.

**Channel delivery suppression**: HTTP sources (`source="http"`) suppress typing indicators, intermediate text delivery, and final reply delivery via the primary channel — the response goes to the Future instead. The agent can still use the `message` tool to send notifications via Telegram during processing.

## Session Storage

Dual-format persistence in `~/.lucyd/sessions/`:

| Format | File | Purpose |
|---|---|---|
| JSONL | `{session_id}.{YYYY-MM-DD}.jsonl` | Append-only audit trail. Every event (message, tool result, compaction) with timestamps. (daily-split — one file per day per session) |
| State | `{session_id}.state.json` | Atomic snapshot of current session state. Written after every message. Used for fast resume. |

### Session Routing

Sessions are keyed by sender (Telegram user ID or "cli"). The `sessions.json` index maps senders to session IDs. When a sender's session state is corrupted, it rebuilds from the JSONL trail.

### Compaction

When the last API response's `input_tokens` exceeds `behavior.compaction.threshold_tokens` (default: 150,000):

1. Take the oldest 2/3 of messages
2. Send them to the compaction model with the compaction prompt
3. Replace old messages with `[Previous conversation summary]\n{summary}`
4. Log a `compaction` event to JSONL (the full history is preserved in the audit trail)

## Memory

SQLite-based long-term memory at `~/.lucyd/memory/main.sqlite`.

### Search Strategy

FTS-first, vector fallback:

1. Run FTS5 full-text search on the query
2. If FTS returns >= 3 results: return them (no API call needed)
3. If FTS returns < 3: embed the query via the configured embedding provider, compute cosine similarity against stored embeddings
4. Merge and deduplicate FTS + vector results, sort by score, return top-k

This handles ~80% of queries without an embedding API call. When no embedding provider is configured (empty model/base_url), vector search is skipped entirely — FTS5 keyword search still works.

### Embedding Cache

Query embeddings are cached in an `embedding_cache` table (keyed by SHA-256 hash + model name) to avoid redundant API calls for repeated searches.

### Tables

**Unstructured memory (v1):**
- `files` -- Indexed file metadata (path, hash, timestamps)
- `chunks` -- Text chunks with source path, line ranges, and optional embeddings
- `chunks_fts` -- FTS5 virtual table for full-text search
- `embedding_cache` -- Cached query embeddings

**Structured memory (v2):**
- `facts` -- Entity-attribute-value triples with confidence scoring and soft deletion
- `episodes` -- Timestamped narrative session summaries with topics and emotional tone
- `commitments` -- Promises and obligations with status tracking (open/done/expired/cancelled)
- `entity_aliases` -- Canonical name resolution (nickname → primary entity name)
- `consolidation_state` -- Tracks per-session processing progress (dedup)
- `consolidation_file_hashes` -- Tracks file content hashes to skip unchanged files

Schema management is in `memory_schema.py` — all tables use `IF NOT EXISTS`. Structured data is extracted by `consolidation.py` via LLM (cron at `:15`), and also written directly by the `memory_write` and `commitment_update` agent tools. All SQL is parameterized.

## Provider Abstraction

The `LLMProvider` protocol defines four methods:

```python
class LLMProvider(Protocol):
    def format_tools(self, tools: list[dict]) -> list[dict]: ...
    def format_system(self, blocks: list[dict]) -> Any: ...
    def format_messages(self, messages: list[dict]) -> list[dict]: ...
    async def complete(self, system, messages, tools, **kwargs) -> LLMResponse: ...
```

Internal message format uses a neutral schema. Each provider translates to/from its API format in the `format_*` methods. The agentic loop never touches provider-specific structures.

### Internal Message Format

- User messages use `"content"` for the message body (string or list of content blocks)
- Assistant messages use `"text"` for the response body
- Providers must handle both: `msg.get("content", msg.get("text", ""))`

This convention exists because user messages mirror the API format while assistant messages are stored in a simplified internal format.

### Neutral Image Blocks

Image attachments use a provider-agnostic format in the internal message schema:

```python
{"type": "image", "media_type": "image/jpeg", "data": "<base64>"}
```

Each provider's `_convert_content_blocks()` static method converts to the native API format:

- **Anthropic**: `{"type": "image", "source": {"type": "base64", "media_type": ..., "data": ...}}`
- **OpenAI**: `{"type": "image_url", "image_url": {"url": "data:{mime};base64,{data}"}}`

Image blocks are injected transiently into `session.messages` for the API call duration only, then restored to text-only before persistence. This prevents base64 data from bloating JSONL files and breaking compaction.

### Provider Configuration

Model definitions live in `providers.d/*.toml` files, loaded via `[providers] load` in `lucyd.toml`. Each provider file declares a connection `type`, `api_key_env`, and one or more `[models.*]` sections. On startup, `config.py:_load_providers()` merges these into the main config. See [configuration — providers](configuration.md#providers) for the file format.

### Adding a New Provider

1. Create `providers/your_provider.py` implementing `LLMProvider`
2. Add a branch in `providers/__init__.py:create_provider()` for the new `provider` type
3. Create `providers.d/your_provider.toml` with `type`, `api_key_env`, and `[models.*]` sections
4. Add the provider name to `[providers] load` in `lucyd.toml`

## Tool Registration

Tools are Python modules in `tools/` that export a `TOOLS` list of dicts:

```python
TOOLS = [
    {
        "name": "tool_name",
        "description": "What this tool does.",
        "input_schema": { ... },   # JSON Schema
        "function": async_or_sync_callable,
    },
]
```

Registration in `lucyd.py:_init_tools()`:

1. Check if the tool name is in `[tools].enabled`
2. Check if dependencies are met (API keys, DB paths, etc.)
3. Call module-level `configure()` if needed (inject config, providers, channel references)
4. Register via `ToolRegistry.register_many(TOOLS)`

The `ToolRegistry` dispatches tool calls from the agentic loop:

- Looks up the function by name
- Calls it with `**arguments` (handles both sync and async functions)
- Catches all exceptions, returns error strings instead of crashing
- Truncates output to `output_truncation` chars (default: 30,000)

Sub-agents spawned via `sessions_spawn` have configurable `max_turns` (default: 50) and `timeout`. A deny-list prevents sub-agents from accessing `sessions_spawn`, `tts`, `react`, and `schedule_message` by default. Sub-agents CAN load skills.

## Cost Tracking

Every API call in the agentic loop records cost to `~/.lucyd/cost.db` (SQLite):

```sql
CREATE TABLE costs (
    timestamp INTEGER,
    session_id TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    cost_usd REAL
);
```

Cost is calculated from the `cost_per_mtok` array in the model config: `[input_rate, output_rate, cache_read_rate]` per million tokens. Query costs via `lucyd-send --cost` (see [operations guide](operations.md#lucyd-send)).

## Live Monitor

Real-time visibility into the agentic loop's state. The daemon writes `~/.lucyd/monitor.json` on every loop event; `lucyd-send --monitor` reads and formats it.

### How It Works

The `on_response` and `on_tool_results` callback parameters in `run_agentic_loop` (already implemented in `agentic.py`) are wired in `lucyd.py:_process_message()`. The callbacks use closure variables for turn counting and timing — no changes to `agentic.py` were needed.

**State transitions:**
```
_process_message entry → "thinking" (turn 1)
  on_response (tool_use) → "tools" (with tool names)
  on_tool_results → "thinking" (turn N+1)
  on_response (tool_use) → "tools"
  on_tool_results → "thinking"
  on_response (end_turn) → "idle"
finally block → "idle"
```

**Concurrency:** Messages process sequentially from the daemon's queue. Sub-agents (spawned via `tools/agents.py`) call `run_agentic_loop` directly without callbacks and do not write to the monitor.

**Atomic writes:** The file is written to `monitor.json.tmp` then renamed to `monitor.json`, preventing partial reads from `watch`.

Direct SQL example:

```sql
SELECT SUM(cost_usd) FROM costs WHERE timestamp >= strftime('%s', 'now', 'start of day');
```
