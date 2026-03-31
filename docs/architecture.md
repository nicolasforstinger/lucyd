# Architecture

How Lucyd fits together. Read this when you need to fix something, add a feature, or understand why a message didn't arrive.

## Overview

HTTP API is the single boundary. Bridges (Telegram, CLI, email) are standalone HTTP clients that POST messages to the daemon and receive replies in the HTTP response. The daemon processes messages through an LLM with tool access. SSE streaming is available via `/chat/stream`. There is no outbound push.

## Module Map

| File | Purpose |
|---|---|
| `lucyd.py` | Daemon entry point. Lifecycle, message loop, signal handlers. Hub-and-spoke across 5 attribute clusters. |
| `agentic.py` | Provider-agnostic tool-use loop. Multi-turn or single-shot dispatch. Collects tool attachments. |
| `api.py` | HTTP API server. 17 endpoints. Envelope extraction, auth, rate limiting. |
| `config.py` | TOML config loader with env overrides. `_SCHEMA`-based typed properties + `raw()` for plugin config. |
| `context.py` | System prompt builder. Three cache tiers (stable/semi-stable/dynamic). Token budget enforcement. |
| `session.py` | Session manager. Dual storage: JSONL audit trail + atomic state snapshots. Compaction via LLM. |
| `memory.py` | Long-term memory. FTS5 keyword search + vector similarity. Structured recall (facts, episodes, commitments). |
| `memory_schema.py` | SQLite schema for memory tables. Safe to call on every startup. |
| `consolidation.py` | Structured data extraction from sessions via LLM. Facts, episodes, commitments, aliases. |
| `skills.py` | Skill loader + `load_skill` tool. Markdown with YAML frontmatter. |
| `metering.py` | Per-call cost recording to SQLite. Billing periods, EUR currency. |
| `metrics.py` | Prometheus metrics. 21 metric families, graceful no-op if `prometheus_client` not installed. |
| `attachments.py` | `Attachment` type, image fitting (`fit_image`), document text extraction (`extract_document_text`), scanned PDF rendering (`render_pdf_pages`). Pure functions. |
| `log_utils.py` | Log sanitization, structured JSON formatter, context vars. |
| `async_utils.py` | `run_blocking()` for safe blocking I/O offload. |
| `channels/telegram.py` | Telegram bridge. Polls getUpdates, POSTs to daemon, delivers replies. Standalone config: `telegram.toml`. |
| `bin/lucydctl` | CLI control client. HTTP wrapper for daemon endpoints. `lucydctl chat` for interactive SSE streaming. |
| `channels/email.py` | Email bridge. IMAP polling, SMTP replies. Standalone config: `email.toml`. |
| `providers/__init__.py` | `LLMProvider` protocol, data types (`LLMResponse`, `StreamDelta`, `Usage`, `ModelCapabilities`), factory. |
| `providers/anthropic.py` | Anthropic provider. Prompt caching, extended thinking, SDK or HTTP fallback. |
| `providers/openai.py` | OpenAI-compatible provider. Embeddings, thinking detection, JSON repair. |
| `providers/smoke_local.py` | Deterministic test provider. No network. |
| `tools/__init__.py` | `ToolRegistry`. Dispatch, error isolation, JSON-aware truncation. |
| `tools/filesystem.py` | `read`, `write`, `edit`. Path allowlist enforcement. |
| `tools/shell.py` | `exec`. Subprocess with timeout, env filtering, process group kill. |
| `tools/web.py` | `web_search`, `web_fetch`. SSRF protection, DNS pinning. |
| `tools/memory_read.py` | `memory_search`, `memory_get`. FTS5 + vector + structured recall. |
| `tools/memory_write.py` | `memory_write`, `memory_forget`, `commitment_update`. |
| `messages.py` | TypedDict message types (`UserMessage`, `AssistantMessage`, `ToolResultsMessage`, `Message` union). |
| `tools/agents.py` | `sessions_spawn`. Sub-agent with scoped tools and deny-list. |
| `tools/status.py` | `session_status`. Context utilization, uptime, cost. |
| `tools/indexer.py` | Workspace file indexer for memory. Used by `POST /api/v1/index`. |
| `plugins.d/stt.py` | STT preprocessor plugin. Transcription backends (OpenAI Whisper, local whisper.cpp), ffmpeg validation, audio attachment preprocessing. |
| `plugins.d/tts.py` | TTS tool plugin. ElevenLabs API, returns audio as attachment. |
| `providers.d/*.toml` | Provider config files. Connection type, API key, model sections. |

## Message Flow

```
Bridge (Telegram/CLI/email)
  | POST /api/v1/chat
  | body: {message, sender, channel_id, task_type, reply_to, attachments}
  v
HTTP API (api.py)
  | _extract_envelope: channel_id (default "http"), task_type (validated), reply_to
  | _parse_and_queue: validate body, build queue item, attach asyncio.Future for /chat
  v
asyncio.Queue (maxsize=1000)
  v
_message_loop (lucyd.py)
  | /chat items: process_http_immediate (no debounce, Future attached)
  | /message /notify items: debounce per sender, then drain_pending
  v
_process_message(text, sender, source, channel_id, task_type, reply_to, ...)
  |
  |-- _run_preprocessors          plugins claim/transform attachments sequentially
  |-- _process_attachments        images scaled (fit_image), documents extracted
  |-- _setup_session              get_or_create(channel_id:sender), inject timestamp
  |-- _build_recall               structured memory recall (facts, episodes, commitments)
  |-- _build_context              system prompt: stable + semi-stable + dynamic tiers
  |-- _run_agentic_with_retries   agentic loop with message-level retry
  |     |
  |     +-- run_agentic_loop (agentic.py)
  |           provider.complete() or provider.stream()
  |           if tool_use -> ToolRegistry.execute() -> append results -> loop
  |           return LLMResponse
  |
  +-- _finalize_response
        |-- _persist_response     JSONL append + state snapshot
        |-- _deliver_reply        route by reply_to:
        |     "" (default)        resolve HTTP future with reply
        |     "silent"            resolve future with silent:true (log only)
        |     "<sender>"          resolve future + enqueue reply as system message to target
        |-- _check_compaction_warning
        |-- _run_compaction_if_needed
        +-- _auto_close_if_ephemeral   close if task_type in ("task", "system")
```

Metrics fire at: preprocessor execution, context utilization calculation, agentic loop (API calls, tokens, cost, latency), tool execution, message completion (count, duration, cost, turns), session close, compaction, errors.

### Internal Message Format

Messages are typed via `messages.py` — three TypedDicts discriminated by `role`:

- **`UserMessage`**: `{"role": "user", "content": str}` — content is always `str` at rest. Transient `_image_blocks` key added during image processing, stripped before persistence.
- **`AssistantMessage`**: `{"role": "assistant", "text": ..., "tool_calls": [...], "usage": {...}}` — all fields except `role` are `NotRequired`. `usage` stripped during compaction.
- **`ToolResultsMessage`**: `{"role": "tool_results", "results": [{"tool_call_id": str, "content": str}]}`

The union `Message = UserMessage | AssistantMessage | ToolResultsMessage` is used throughout the pipeline (`session.messages`, agentic loop params, consolidation). mypy narrows via `msg["role"] == "user"` checks. Provider `format_messages(messages: list[Message]) -> list[dict[str, Any]]` is the boundary between internal and provider-specific formats. TypedDicts are plain dicts at runtime — no serialization change, no wire format change.

## Daemon State Architecture

`LucydDaemon` is a composition root — the attributes are injected dependencies, not mutable shared state.

### Hub-and-Spoke Model

`_process_message` is the hub. It orchestrates a pipeline that touches all subsystems sequentially. The spokes are 5 attribute clusters with minimal cross-talk:

```
                     config (universal — read by most methods)
                          |
     +--------------------+----------------------+
     |                    |                      |
 [Provider]          [Event Loop]          [Diagnostics]
  provider            running               _error_counts
  _providers          queue                 start_time
  _single_shot        _control_queue
                      _session_locks
                           |
                    _process_message --- hub
                           |
              +------------+------------+
              |            |            |
         [Sessions]   [Memory]     [Context]
         session_mgr  _memory_conn  context_builder
         _current_    metering_db   skill_loader
         session                    tool_registry
                                    _preprocessors
```

### Write-Once-Read-Many Topology

Every heavily-shared attribute has exactly one write site. After startup, no mutable-state entanglement:

| Attribute | Written by | Operational readers |
|---|---|---|
| `session_mgr` | `_init_sessions` | 12 methods |
| `metering_db` | `_init_metering` | 8 methods |
| `_memory_conn` | `_get_memory_conn` (lazy) | 7 methods |
| `provider` | `_init_provider` | 6 methods |
| `_preprocessors` | `_init_tools` | 1 method |
| `tool_registry` | `_init_tools` | 4 methods |
| `context_builder` | `_init_context` | 1 method |
| `skill_loader` | `_init_skills` | 2 methods |

### Bridge Points

4 operational methods touch attributes from 3+ clusters:

| Method | Clusters | Nature |
|---|---|---|
| `_init_tools` | All 5 | Wiring — runs once at startup |
| `_run_compaction_if_needed` | Sessions + Memory | Pipeline step: sessions <-> memory |
| `_build_sessions` | Sessions + Memory + Provider | Read-only view builder |
| `_build_status` | Sessions + Memory + Diagnostics + Loop | Read-only health check |

### Rules for New Code

1. **Identify the cluster.** New methods should touch attributes from one cluster. If it needs 2+, it's a bridge point.
2. **Don't add write sites.** Create new shared resources in `_init_*` methods. Let operational methods read them.
3. **Core never imports from plugins.d/.** Plugins resolve their own config via `config.raw()` and validate their own dependencies in `configure()`.
4. **When to extract.** If a cluster grows beyond ~8 methods or ~150 lines AND its attributes don't bridge to other clusters, extract it.

## HTTP API

17 endpoints. All registered in `api.py` lines 156-174.

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/api/v1/chat` | POST | yes | Send message, await response |
| `/api/v1/chat/stream` | POST | yes | Send message, stream response via SSE |
| `/api/v1/message` | POST | yes | Fire-and-forget user message (202) |
| `/api/v1/notify` | POST | yes | Fire-and-forget notification (202) |
| `/api/v1/status` | GET | no | Health check + daemon stats |
| `/metrics` | GET | no | Prometheus metrics exposition |
| `/api/v1/sessions` | GET | yes | List active sessions |
| `/api/v1/cost` | GET | yes | Cost breakdown by period |
| `/api/v1/monitor` | GET | yes | Live agentic loop state |
| `/api/v1/sessions/reset` | POST | yes | Reset/archive sessions |
| `/api/v1/sessions/{id}/history` | GET | yes | Session event history |
| `/api/v1/evolve` | POST | yes | Trigger memory evolution |
| `/api/v1/compact` | POST | yes | Force diary write + compaction |
| `/api/v1/index` | POST | yes | Run workspace indexing |
| `/api/v1/index/status` | GET | yes | Workspace index status |
| `/api/v1/consolidate` | POST | yes | Extract facts from workspace files |
| `/api/v1/maintain` | POST | yes | Run memory maintenance |

Auth: Bearer token from `LUCYD_HTTP_TOKEN` env var. Auth-exempt paths: `/api/v1/status`, `/metrics`.

### Message Envelope

All inbound message endpoints (`/chat`, `/chat/stream`, `/message`, `/notify`) accept envelope fields extracted by `_extract_envelope()` (api.py):

| Field | Type | Default | Purpose |
|---|---|---|---|
| `channel_id` | string | `"http"` | Channel identifier. Used in session keying and metrics labels. |
| `task_type` | string | `"conversational"` | Session lifecycle: `"conversational"` (stays open), `"task"` (auto-close), `"system"` (auto-close, internal). Validated against `_VALID_TASK_TYPES`. |
| `reply_to` | string | absent | Response routing: omit for normal reply, `"silent"` for log-only, or a sender name to redirect. |

### Request Flow

For `/chat`: an `asyncio.Future` is attached to the queue item. `_process_message` resolves it via `_deliver_reply`. The HTTP handler awaits the Future with `agent_timeout_seconds` timeout, returns 408 on expiry.

For `/chat/stream`: an `asyncio.Queue` is attached instead. `_on_stream_delta` pushes SSE events as the provider streams. The HTTP handler reads the queue and writes SSE frames.

For `/message`, `/notify`: fire-and-forget. No Future. Returns 202 immediately.

## Plugin System

Plugins are Python files in `plugins.d/`. They export `TOOLS` (tool definitions) and/or `PREPROCESSORS` (attachment transformers). Tools are gated by `[tools] enabled`. Preprocessors register unconditionally when the plugin loads.

At startup, `_init_tools()` scans `plugins.d/*.py`, loads each via `importlib.util`, calls `configure()` with inspect-based dependency injection, then registers tools and preprocessors.

Core never imports from `plugins.d/`. Plugins access their config via `config.raw()` and validate their own dependencies in `configure()`.

See [Plugin & Channel Guide](plugins.md) for the full developer reference.

## Session Storage

Dual-format persistence in `$DATA_DIR/sessions/`:

| Format | File | Purpose |
|---|---|---|
| JSONL | `{id}.{YYYY-MM-DD}.jsonl` | Append-only audit trail (daily-split) |
| State | `{id}.state.json` | Atomic snapshot for fast resume |

Sessions are keyed by `channel_id:sender` (e.g., `telegram:Nicolas`, `http:n8n-daily`). The key is computed in `_process_message` as `f"{channel_id}:{sender}"` and passed to `session_mgr.get_or_create()`.

Ephemeral sessions (`task_type` "task" or "system") auto-close after processing via `_auto_close_if_ephemeral`. Pre-existing sessions are never auto-closed.

Compaction triggers when `input_tokens` exceeds `compaction.threshold_tokens`: oldest messages are summarized via LLM, keeping the newest `keep_recent_pct` fraction verbatim (`session:compact_session()`).

On close, `on_close` callbacks fire (consolidation extracts facts/episodes), then session files are archived to `.archive/`.

## Context Building

System prompt assembled in `ContextBuilder.build()` (context.py) across three cache tiers:

| Tier | Content | Caching |
|---|---|---|
| stable | Personality files (SOUL.md, etc.), tool descriptions | Cached aggressively |
| semi-stable | MEMORY.md, always-on skill bodies, skill index | Shorter TTL |
| dynamic | Date/time, sender, task-type framing, memory recall, limits | Never cached |

Task-type framing (`ContextBuilder._build_dynamic()`) tells the agent what kind of session it's in:

| task_type + deliver | Framing |
|---|---|
| `system` + no deliver | "automated infrastructure — replies internal only" |
| `system` + deliver | "notification routed to operator" |
| `task` | "ephemeral task — session closes after reply" |
| `conversational` | "conversation — history preserved" |

Token budget enforcement trims dynamic first, then semi-stable. Stable tier is never trimmed.

## Memory

SQLite FTS5 + vector similarity at `$DATA_DIR/memory/main.sqlite`.

**Structured memory:** Facts (entity-attribute-value), episodes (session summaries), commitments (trackable promises). Extracted on session close via `consolidation.py` and written directly by agent tools (`memory_write`, `commitment_update`).

**Recall:** At session start, `_build_recall` injects relevant facts, episodes, and open commitments into the dynamic context tier. Priority: commitments > vector > episodes > facts. Budget-aware.

## Provider Abstraction

`LLMProvider` protocol (providers/__init__.py): `capabilities`, `format_tools`, `format_system`, `format_messages`, `complete`, `stream`.

Key data types:
- `LLMResponse`: text, tool_calls, stop_reason, usage, turns, attachments, cost_limited
- `Usage`: input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
- `ModelCapabilities`: supports_tools, supports_vision, supports_streaming, supports_thinking, max_context_tokens

Three implementations: Anthropic (prompt caching, extended thinking), OpenAI-compatible (embeddings, local models), smoke-test (deterministic, offline). Model definitions live in `providers.d/*.toml`.

## Metrics

21 Prometheus metric families (metrics.py). Graceful no-op when `prometheus_client` is not installed. Exposed at `GET /metrics`.

| Scope | Metrics | Labels |
|---|---|---|
| Per-message | `messages_total`, `message_duration_seconds`, `message_cost_eur`, `agentic_turns`, `context_utilization_ratio` | channel_id, task_type, session_id, sender |
| Per-provider | `api_calls_total`, `api_latency_seconds`, `tokens_total`, `api_cost_eur_total` | model, provider, status/direction |
| Per-tool | `tool_calls_total`, `tool_duration_seconds` | tool_name, status |
| Per-preprocessor | `preprocessor_total`, `preprocessor_duration_seconds` | name, status |
| Memory ops | `memory_ops_total` | operation |
| Session | `active_sessions`, `compaction_total`, `compaction_tokens_reclaimed`, `session_close_total` | reason (close only) |
| System | `queue_depth`, `uptime_seconds`, `errors_total` | error_type (errors only) |

## Cost Tracking

Every LLM call records to `metering.db` via `MeteringDB` (metering.py): tokens, cost (EUR), model, provider, session, latency. Query via `lucydctl --cost` or `GET /api/v1/cost`.
