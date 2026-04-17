# Architecture

How Lucyd fits together. Read this when you need to fix something, add a feature, or understand why a message didn't arrive.

## Overview

HTTP API is the single boundary. Bridges (Telegram, CLI, email) are standalone HTTP clients that POST messages to the daemon and receive replies in the HTTP response. The daemon processes messages through an LLM with tool access. SSE streaming is available via `/chat/stream`. There is no outbound push.

## Module Map

| File | Purpose |
|---|---|
| `lucyd.py` | Daemon coordinator. Bootstrap, message loop, signal handlers, init methods. Delegates processing to `MessagePipeline` and periodic operations to `operations.py`. |
| `pipeline.py` | `MessagePipeline`. Complete message processing: preprocessors, attachments, session setup, recall, context, agentic loop, response finalization. |
| `operations.py` | Periodic operations: evolve, index, consolidate, maintain, compact. Standalone functions called by daemon handlers. |
| `agentic.py` | Provider-agnostic tool-use loop. Multi-turn or single-shot dispatch. Collects tool attachments. |
| `api.py` | HTTP API server. 17 endpoints. Envelope extraction, auth, rate limiting. |
| `config.py` | TOML config loader with env overrides. `_SCHEMA`-based typed properties + `raw()` for plugin config. |
| `context.py` | System prompt builder. Three cache tiers (stable/semi-stable/dynamic). Token budget enforcement. |
| `db.py` | PostgreSQL connection pool (asyncpg) + forward-only schema versioning from `schema/*.sql`. |
| `session.py` | Session manager. PostgreSQL-backed sessions, messages, events. Compaction via LLM. |
| `memory.py` | Long-term memory. PostgreSQL tsvector FTS + pgvector similarity. Structured recall (facts, episodes, commitments). |
| `consolidation.py` | Structured data extraction from sessions via LLM. Facts, episodes, commitments, aliases. |
| `skills.py` | Skill loader + `load_skill` tool. Markdown with YAML frontmatter. |
| `metering.py` | Per-call cost recording to PostgreSQL. Billing periods, costs in EUR. |
| `metrics.py` | Prometheus metrics. 29 metric families, graceful no-op if `prometheus_client` not installed. |
| `attachments.py` | `Attachment` type, image fitting (`fit_image`), document text extraction (`extract_document_text`). Pure functions. |
| `log_utils.py` | Log sanitization, structured JSON formatter, context vars. |
| `async_utils.py` | `run_blocking()` for safe blocking I/O offload. |
| `channels/telegram.py` | Telegram bridge. Polls getUpdates, POSTs `/inbound/telegram`, delivers replies. Config: `[telegram]` section in `lucyd.toml`. |
| `channels/email.py` | Email bridge. IMAP polling, POSTs `/inbound/email`, SMTP replies. Config: `[email]` section in `lucyd.toml`. |
| `providers/__init__.py` | `LLMProvider` protocol, data types (`LLMResponse`, `StreamDelta`, `Usage`, `ModelCapabilities`), factory. |
| `providers/anthropic.py` | Anthropic provider. Prompt caching, extended thinking. SDK required. |
| `providers/openai.py` | OpenAI-compatible provider. Embeddings, thinking detection. SDK required. |
| `providers/mistral.py` | Mistral provider. Tool use, vision, streaming. SDK required. |
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
| `plugins.d/whisper.py` | STT preprocessor plugin. OpenAI Whisper SDK (cloud) + whisper.cpp (local), cost tracking via metering DI. Config: `whisper.toml`. |
| `plugins.d/elevenlabs.py` | TTS tool plugin. ElevenLabs SDK, cost tracking via metering DI. Config: `elevenlabs.toml`. |
| `providers.d/*.toml` | Provider config files. Connection type, API key, model sections. |

## Message Flow

```
Caller (bridge / cron / webhook / tool)
  | POST /api/v1/<endpoint>
  | body: {message, sender?, reply_to?, attachments?}
  v
HTTP API (api.py)
  | endpoint pins talker (user|operator|system|agent)
  | validate sender against talker's enumerated set
  | /chat, /chat/stream, /inbound/*: attach asyncio.Future (sync)
  v
asyncio.Queue (priority: user/operator -> USER, system/agent -> SYSTEM)
  v
_message_loop (lucyd.py)
  | sync items (Future attached): process_http_immediate (no debounce)
  | async items: debounce per session_key, then drain_pending
  v
_process_message(text, sender, talker, channel?, reply_to?, ...)
  |
  |-- _run_preprocessors          plugins claim/transform attachments sequentially
  |-- _process_attachments        images scaled (fit_image), documents extracted
  |-- _setup_session              get_or_create(f"{talker}:{sender}"), inject timestamp
  |-- _build_recall               structured memory recall (facts, episodes, commitments)
  |-- _build_context              system prompt: stable + semi-stable + dynamic tiers
  |                                (dynamic framing is talker-keyed)
  |-- _ensure_context_budget       pre-loop compaction if context > 80%
  |-- _run_agentic                  agentic loop (API-level retry only)
  |     |
  |     +-- run_agentic_loop (agentic.py)
  |           provider.complete() or provider.stream()
  |           if tool_use -> ToolRegistry.execute() -> append results -> loop
  |           return LLMResponse
  |
  +-- _finalize_response
        |-- _persist_response     JSONL append + state snapshot
        |-- _deliver_reply        system/agent: silent (no reply path)
        |                          reply_to="silent": silent
        |                          otherwise: resolve HTTP future with reply
        |-- _check_compaction_warning
        |-- _run_compaction_if_needed
        +-- _auto_close_if_ephemeral   close if talker in ("system", "agent")
```

Metrics fire at: preprocessor execution, context utilization calculation, agentic loop (API calls, tokens, cost, latency), tool execution, message completion (count, duration, cost, turns), session close, compaction, errors.

### Internal Message Format

Messages are typed via `messages.py` — three TypedDicts discriminated by `role`:

- **`UserMessage`**: `{"role": "user", "content": str}` — content is always `str` at rest. Transient `_image_blocks` key added during image processing, stripped before persistence.
- **`AssistantMessage`**: `{"role": "assistant", "text": ..., "tool_calls": [...], "usage": {...}}` — all fields except `role` are `NotRequired`. `usage` stripped during compaction.
- **`ToolResultsMessage`**: `{"role": "tool_results", "results": [{"tool_call_id": str, "content": str}]}`

The union `Message = UserMessage | AssistantMessage | ToolResultsMessage` is used throughout the pipeline (`session.messages`, agentic loop params, consolidation). mypy narrows via `msg["role"] == "user"` checks. Provider `format_messages(messages: list[Message]) -> list[dict[str, Any]]` is the boundary between internal and provider-specific formats. TypedDicts are plain dicts at runtime — no serialization change, no wire format change.

## Daemon State Architecture

`LucydDaemon` is a composition root — it wires dependencies at startup and delegates processing. `MessagePipeline` owns message processing state. `operations.py` provides standalone periodic functions.

### Decomposed Structure

```
LucydDaemon (lucyd.py)
  ├── Bootstrap: _init_provider, _init_sessions, _init_tools, _init_context, ...
  ├── Event loop: _message_loop, queue, running
  ├── Signals: SIGUSR1 (reload), SIGTERM/SIGINT (stop)
  └── Delegates to:
        ├── MessagePipeline (pipeline.py)
        │     process_message → _build_recall → _build_context
        │       → _ensure_context_budget → _run_agentic → _finalize_response
        │     Owns: session_mgr, tool_registry, context_builder,
        │           _memory_conn, metering_db, _preprocessors, error_counts
        └── operations.py
              evolve, index, consolidate, maintain, compact
```

`LucydDaemon._process_message()` is a thin delegator to `pipeline.process_message()`. The daemon owns init and lifecycle; the pipeline owns per-message processing.

### Write-Once-Read-Many Topology

Every heavily-shared attribute has exactly one write site. After startup, no mutable-state entanglement. Most operational attributes live on `MessagePipeline`:

| Attribute | Owner | Written by |
|---|---|---|
| `session_mgr` | pipeline | `_init_sessions` |
| `metering_db` | pipeline | `_init_metering` |
| `_memory_conn` | pipeline | `_get_memory_conn` (lazy) |
| `provider` | daemon | `_init_provider` |
| `_preprocessors` | pipeline | `_init_tools` |
| `tool_registry` | pipeline | `_init_tools` |
| `context_builder` | pipeline | `_init_context` |
| `skill_loader` | pipeline | `_init_skills` |

### Rules for New Code

1. **Message processing goes in pipeline.py.** Per-message logic belongs on `MessagePipeline`. Daemon methods should only handle lifecycle, init, and HTTP handler callbacks.
2. **Periodic operations go in operations.py.** Standalone functions, not daemon methods.
3. **Don't add write sites.** Create new shared resources in `_init_*` methods. Let operational code read them.
4. **Core never imports from plugins.d/.** Plugins resolve their own config via `config.raw()` and validate their own dependencies in `configure()`.

## HTTP API

All registered in `api.py`. Auth: Bearer token from `LUCYD_HTTP_TOKEN`.
Auth-exempt: `/api/v1/status`, `/metrics`.

| Endpoint | Method | Talker | Purpose |
|---|---|---|---|
| `/api/v1/chat` | POST | operator | Sync message, await response |
| `/api/v1/chat/stream` | POST | operator | Sync message, stream response via SSE |
| `/api/v1/inbound/telegram` | POST | user | Telegram bridge inbound |
| `/api/v1/inbound/email` | POST | user | Email bridge inbound |
| `/api/v1/inbound/whatsapp` | POST | user | Reserved (501 until implemented) |
| `/api/v1/system/event` | POST | system | External events (cron, webhooks, errors) |
| `/api/v1/agent/action` | POST | agent | Agent self-actions (reminders, a2a) |
| `/api/v1/status` | GET | — | Health check + daemon stats |
| `/metrics` | GET | — | Prometheus metrics exposition |
| `/api/v1/sessions` | GET | — | List active sessions |
| `/api/v1/cost` | GET | — | Cost breakdown by period |
| `/api/v1/monitor` | GET | — | Live agentic loop state |
| `/api/v1/sessions/reset` | POST | — | Reset/archive sessions |
| `/api/v1/sessions/{id}/history` | GET | — | Session event history |
| `/api/v1/evolve` | POST | — | Trigger memory evolution |
| `/api/v1/compact` | POST | — | Force diary write + compaction |
| `/api/v1/index` | POST | — | Run workspace indexing |
| `/api/v1/index/status` | GET | — | Workspace index status |
| `/api/v1/consolidate` | POST | — | Extract facts from workspace files |
| `/api/v1/maintain` | POST | — | Run memory maintenance |

### Message Envelope

Every inbound message endpoint declares the `talker` internally — never
overridable from the request body. The client only supplies content plus
the within-talker `sender`:

| Field | Type | Required | Purpose |
|---|---|---|---|
| `message` | string | yes | The content |
| `sender` | string | depends | Within-talker identity. Operator: `agentctl`. System: `maintenance`/`automation`/`error`. Agent: `self`/`other`. User: auto-injected as `config.user.name` by `/inbound/*`. |
| `reply_to` | string | no | `"silent"` suppresses delivery (reply still processed). |
| `attachments` | list | no | Base64-encoded files |

Invalid `sender` for the talker class → 400.

### Request Flow

For `/chat`, `/chat/stream`, and `/inbound/*`: an `asyncio.Future` is
attached to the queue item. `_process_message` resolves it via
`_deliver_reply`. The HTTP handler awaits the Future with
`agent_timeout_seconds` timeout, returns 408 on expiry.

For `/chat/stream`: an additional `asyncio.Queue` is attached. Stream
callbacks push SSE events as the provider streams. The HTTP handler
reads the queue and writes SSE frames.

For `/system/event`, `/agent/action`: fire-and-forget. No Future.
Returns 202 immediately. Sessions auto-close after processing.

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

Sessions are keyed by `talker:sender` (e.g., `user:nicolas`,
`operator:agentctl`, `system:maintenance`, `agent:self`). The key is computed
in `_process_message` as `f"{talker}:{sender}"` and passed to
`session_mgr.get_or_create()`.

Ephemeral sessions (talker `system` or `agent`) auto-close after
processing via `_auto_close_if_ephemeral`. Pre-existing sessions are
never auto-closed. `user` and `operator` sessions stay open until
manually reset.

Compaction triggers when `input_tokens` exceeds `compaction.threshold_tokens`: oldest messages are summarized via LLM, keeping the newest `keep_recent_pct` fraction verbatim (`session:compact_session()`).

On close, `on_close` callbacks fire (consolidation extracts facts/episodes), then session files are archived to `.archive/`.

## Context Building

System prompt assembled in `ContextBuilder.build()` (context.py) across three cache tiers:

| Tier | Content | Caching |
|---|---|---|
| stable | Personality files (SOUL.md, etc.), tool descriptions | Cached aggressively |
| semi-stable | MEMORY.md, always-on skill bodies, skill index | Shorter TTL |
| dynamic | Date/time, sender, task-type framing, memory recall, limits | Never cached |

Talker framing (`ContextBuilder._build_dynamic()`) tells the agent who
is speaking and how to treat the session:

| talker | Framing |
|---|---|
| `user` | "Messages come from the person you serve, possibly via any of their whitelisted channels. History feeds your memory." |
| `operator` | "Messages come from an administrator. History persists but does NOT feed user memory." |
| `system` | "Automated infrastructure events. Process and reply internally — session closes after your reply." |
| `agent` | "This message is from you (scheduled self-action or agent-to-agent). No outbound delivery." |

Token budget enforcement trims dynamic first, then semi-stable. Stable tier is never trimmed.

## Memory

PostgreSQL tsvector FTS + pgvector similarity (knowledge + search schemas).

**Structured memory:** Facts (entity-attribute-value), episodes (session summaries), commitments (trackable promises). Extracted on session close via `consolidation.py` and written directly by agent tools (`memory_write`, `commitment_update`).

**Recall:** At session start, `_build_recall` injects relevant facts, episodes, and open commitments into the dynamic context tier. Priority: commitments > vector > episodes > facts. Budget-aware.

## Provider Abstraction

`LLMProvider` protocol (providers/__init__.py): `capabilities`, `format_tools`, `format_system`, `format_messages`, `complete`, `stream`.

Key data types:
- `LLMResponse`: text, tool_calls, stop_reason, usage, turns, attachments, cost_limited
- `Usage`: input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
- `ModelCapabilities`: supports_tools, supports_vision, supports_streaming, supports_thinking, max_context_tokens

Four implementations: Anthropic (prompt caching, extended thinking), OpenAI-compatible (embeddings, local models), Mistral (tool use, streaming), smoke-test (deterministic, offline). Model definitions live in `providers.d/*.toml`.

## Metrics

29 Prometheus metric families (metrics.py). Graceful no-op when `prometheus_client` is not installed. Exposed at `GET /metrics`.

| Scope | Metrics | Labels |
|---|---|---|
| Per-message | `messages_total`, `message_duration_seconds`, `message_cost_eur`, `agentic_turns`, `context_utilization_ratio`, `message_outcome_total` | talker, session_id, sender; outcome |
| Per-provider | `api_calls_total`, `api_latency_seconds`, `tokens_total`, `api_cost_eur_total`, `ttft_seconds`, `api_retries_total` | model, provider, status/direction |
| Per-tool | `tool_calls_total`, `tool_duration_seconds` | tool_name, status |
| Per-preprocessor | `preprocessor_total`, `preprocessor_duration_seconds` | name, status |
| Memory | `memory_ops_total`, `memory_search_duration_seconds` | operation; search_type |
| Session | `active_sessions`, `compaction_total`, `compaction_tokens_reclaimed`, `session_close_total`, `session_open_total`, `consolidation_duration_seconds` | reason (close only) |
| Context | `context_trims_total`, `context_trim_tokens` | — |
| System | `queue_depth`, `uptime_seconds`, `errors_total` | error_type (errors only) |

## Cost Tracking

Every LLM call records to PostgreSQL via `MeteringDB` (metering.py): tokens, cost_eur, fx_rate, model, provider, session, latency. Query via `GET /api/v1/cost`.
