# Architecture

How Lucyd fits together. Read this when you need to fix something, add a feature, or understand why a message didn't arrive.

## Overview

HTTP API is the single boundary. Bridges (Telegram, CLI, email) are standalone processes that POST messages to the daemon and deliver replies. The daemon processes messages through an LLM with tool access and returns responses via HTTP. Outbound delivery (typing indicators, streaming, reply text + attachments) flows through `RelayChannel` back to the bridge.

## Module Map

| File | Purpose |
|---|---|
| `lucyd.py` | Daemon entry point. Lifecycle, message loop, signal handlers. Delegates to `_process_message` phases. |
| `agentic.py` | Provider-agnostic tool-use loop. Multi-turn or single-shot dispatch. Collects tool attachments. |
| `api.py` | HTTP API server (always on). REST endpoints for all inbound/outbound interactions. |
| `config.py` | TOML config loader with env overrides. Typed access, immutable after load. |
| `context.py` | System prompt builder. Cache tiers (stable/semi-stable/dynamic). Token counting. |
| `session.py` | Session manager. Dual storage: JSONL audit trail + atomic state snapshots. Compaction. |
| `relay.py` | Outbound proxy. `RelayChannel` forwards send/typing/stream to bridge via HTTP. |
| `memory.py` | Long-term memory. FTS5 keyword search + vector similarity. Structured recall (facts, episodes, commitments). |
| `memory_schema.py` | SQLite schema for 11 memory tables. Safe to call on every startup. |
| `consolidation.py` | Structured data extraction from sessions via LLM. Facts, episodes, commitments, aliases. |
| ~~`evolution.py`~~ | Removed. Evolution is skill-driven via the agentic loop. State tracking inlined in `lucyd.py`. |
| `skills.py` | Skill loader + `load_skill` tool. Markdown with YAML frontmatter. |
| `metering.py` | Per-call cost recording to SQLite. Billing periods, EUR currency. |
| ~~`monitor.py`~~ | Removed. `_MonitorWriter` in `lucyd.py` updates an in-memory dict read by `/api/v1/monitor`. |
| `attachments.py` | Image fitting, document text extraction, audio STT. Pure functions. |
| `models.py` | Shared data types: `Attachment`. |
| `stt.py` | Speech-to-text dispatch (OpenAI Whisper or local whisper.cpp). |
| `log_utils.py` | Log sanitization, structured JSON formatter, context vars. |
| `async_utils.py` | `run_blocking()` for safe blocking I/O offload. |
| `channels/telegram.py` | Telegram bridge. Polls getUpdates, POSTs to daemon, delivers replies. |
| `channels/cli.py` | CLI bridge. stdin/stdout with SSE streaming. |
| `channels/email.py` | Email bridge. IMAP polling, SMTP replies. |
| `providers/__init__.py` | `LLMProvider` protocol, data types (`LLMResponse`, `StreamDelta`, `Usage`), factory. |
| `providers/anthropic_compat.py` | Anthropic provider. Prompt caching, extended thinking, SDK or HTTP fallback. |
| `providers/openai_compat.py` | OpenAI-compatible provider. Embeddings, thinking detection, JSON repair. |
| `providers/smoke_local.py` | Deterministic test provider. No network. |
| `tools/__init__.py` | `ToolRegistry`. Dispatch, error isolation, truncation. Structured results with attachments. |
| `tools/filesystem.py` | `read`, `write`, `edit`. Path allowlist enforcement. |
| `tools/shell.py` | `exec`. Subprocess with timeout, env filtering, process group kill. |
| `tools/web.py` | `web_search`, `web_fetch`. SSRF protection, DNS pinning. |
| `tools/memory_read.py` | `memory_search`, `memory_get`. FTS5 + vector + structured recall. |
| `tools/memory_write.py` | `memory_write`, `memory_forget`, `commitment_update`. |
| `tools/agents.py` | `sessions_spawn`. Sub-agent with scoped tools. |
| `tools/status.py` | `session_status`. Context utilization, uptime, cost. |
| `tools/indexer.py` | Workspace file indexer for memory. Used by `POST /api/v1/index`. |
| `plugins.d/tts.py` | TTS plugin. ElevenLabs API, returns audio as attachment. |
| `bin/lucydctl` | CLI control client. HTTP wrapper for daemon endpoints. |
| `providers.d/*.toml` | Provider config files. Connection type, API key, model sections. |

## Message Flow

```
Bridge (Telegram/CLI/email)
  ↓ POST /api/v1/chat {"message": "...", "sender": "...", "attachments": [...]}
HTTP API (api.py)
  ↓ asyncio.Queue
Message loop (lucyd.py)
  ↓ debounce per sender
_process_message
  ├── _process_attachments — images scaled, voice transcribed, docs extracted
  ├── _setup_session — get/create session
  ├── _build_context — system prompt + memory recall + tools
  └── _run_agentic_with_retries — agentic loop with retry
        ↓
      run_agentic_loop (agentic.py)
        ├── provider.complete() or provider.stream()
        ├── record cost
        ├── if tool_use → execute tools → append results → loop
        └── return LLMResponse (text + attachments)
        ↓
_finalize_response
  ├── persist session (JSONL + state)
  ├── deliver reply + attachments via relay → bridge
  └── check compaction threshold
        ↓
HTTP response {"reply": "...", "attachments": ["/path/to/file"]}
  ↓
Bridge delivers text + files to user
```

## Daemon State Architecture

`LucydDaemon` owns 20 `self.*` attributes and 48 methods. This section documents the internal structure so you know where new code belongs and when lucyd.py has outgrown the coordinator pattern.

### Hub-and-Spoke Model

The daemon is a coordinator with hub-and-spoke state topology. `_process_message` is the hub — it orchestrates a pipeline that touches all subsystems sequentially. The spokes are 6 attribute clusters with minimal cross-talk:

```
                     config (universal — 33 of 46 methods read it)
                          │
     ┌────────────────────┼──────────────────────┐
     │                    │                      │
 [Provider]          [Event Loop]          [Diagnostics]
  provider            running               _monitor_state
  _providers          queue                 _error_counts
  _single_shot        _control_queue        start_time
                      _session_locks
                           │
                  ┌────────┴────────┐
                  │                 │
            [Channel I/O]    _process_message ─── hub
              channel               │
                       ┌────────────┼────────────┐
                       │            │            │
                  [Sessions]   [Memory]     [Context]
                  session_mgr  _memory_conn  context_builder
                  _current_    metering_db   skill_loader
                  session                    tool_registry
```

### The 6 Clusters

**Provider** (3 attrs, 3 methods: `_init_provider`, `_create_provider_for`, `get_provider`)
- `provider`, `_providers`, `_single_shot`
- Self-contained. No cross-talk with other clusters except as a read-only dependency.

**Sessions** (3 attrs, 12 reader methods)
- `session_mgr`, `_current_session`, `_session_locks`
- Highest fan-out: 12 methods read `session_mgr`. Single write site: `_init_sessions`.
- Methods: `_setup_session`, `_persist_response`, `_auto_close_if_system`, `_reset_session`, `_check_compaction_warning`, `_run_compaction_if_needed`, `_handle_agentic_error`, `_build_sessions`, `_build_history`, `_build_status`, `_handle_compact`.

**Memory** (2 attrs, 7 reader methods)
- `_memory_conn`, `metering_db`
- These co-occur in 4 methods (`_run_compaction_if_needed`, `_consolidate_on_close`, `_handle_consolidate`, `_handle_maintain`). All deal with SQLite, facts, indexing, or maintenance.
- `metering_db` also appears in Sessions cluster methods for cost display — this is the main cross-talk point.

**Channel I/O** (1 attr, 5 reader methods)
- `channel`
- `_deliver_reply`, `_channel_reader`, `_handle_agentic_error`, `_process_message`, `_init_tools`.

**Diagnostics** (3 attrs, 3 methods)
- `_monitor_state`, `_error_counts`, `start_time`
- `_build_monitor`, `_build_status`, `_handle_agentic_error` (writes `_error_counts`).
- Nearly zero cross-talk. `_build_status` is the only method that also reads from other clusters.

**Event Loop** (4 attrs, 3 methods: `_message_loop`, `_drain_control_queue`, `_setup_signals`)
- `running`, `queue`, `_control_queue`, `_session_locks`
- Pure dispatch infrastructure. Calls into other clusters but shares no mutable state with them.

### Write-Once-Read-Many Topology

Every heavily-shared attribute has exactly **one write site**. After startup, the state graph is read-only — no mutable-state entanglement, just shared references:

| Attribute | Written by | Operational readers |
|---|---|---|
| `session_mgr` | `_init_sessions` | 12 methods |
| `metering_db` | `_init_metering` | 8 methods |
| `_memory_conn` | `_get_memory_conn` (lazy) | 7 methods |
| `provider` | `_init_provider` | 6 methods |
| `channel` | `_init_channel` | 5 methods |

This is why the daemon isn't a god object despite the method count — it's a composition root. The attributes are dependencies, not mutable shared state.

### Bridge Points

Only 4 operational methods touch attributes from 3+ clusters:

| Method | Clusters | Nature |
|---|---|---|
| `_init_tools` | All 5 | Wiring — runs once at startup |
| `_run_compaction_if_needed` | Sessions + Memory | Pipeline step: sessions ↔ memory |
| `_build_sessions` | Sessions + Memory + Provider | Read-only view builder |
| `_build_status` | Sessions + Memory + Diagnostics + Loop | Read-only health check |

Three of four are read-only aggregation. `_run_compaction_if_needed` is the only operational bridge — it's where the sessions cluster hands off to the memory cluster during compaction.

### Rules for New Code

1. **Identify the cluster.** New methods should touch attributes from one cluster. If a method needs attrs from 2+ clusters, it's a bridge point — keep it in `_process_message`'s pipeline or as an HTTP callback.

2. **Don't add write sites.** The write-once topology is structural, not accidental. If you need a new shared resource, create it in an `_init_*` method and let operational methods read it.

3. **`metering_db` is the cross-talk point.** It appears in both Sessions and Memory clusters because cost tracking spans both. Accept this coupling rather than creating abstractions to hide it.

4. **When to extract.** If a cluster grows beyond ~8 methods or ~150 lines AND its attributes don't bridge to other clusters, it's a candidate for extraction. The Memory cluster (7 methods, ~120 lines, one private attr) is currently the closest candidate. Extract when it earns it, not before.

5. **When lucyd.py has outgrown the coordinator.** If the bridge point count grows beyond 6-8, or if new mutable shared state appears (attributes written by multiple operational methods), the hub-and-spoke model is breaking down and decomposition is warranted.

## HTTP API

The API is the single inbound boundary. All messages enter here.

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/v1/chat` | POST | Send message, await response (text + attachments) |
| `/api/v1/chat/stream` | POST | Send message, stream response via SSE |
| `/api/v1/message` | POST | Fire-and-forget: queue user message, return 202 |
| `/api/v1/system` | POST | Fire-and-forget: queue system event, return 202 |
| `/api/v1/notify` | POST | Fire-and-forget: queue notification, route to `notify_target` |
| `/api/v1/status` | GET | Health check + daemon stats (no auth) |
| `/api/v1/sessions` | GET | List active sessions |
| `/api/v1/cost` | GET | Cost breakdown by period |
| `/api/v1/monitor` | GET | Live agentic loop state |
| `/api/v1/sessions/reset` | POST | Reset sessions |
| `/api/v1/sessions/{id}/history` | GET | Session event history |
| `/api/v1/evolve` | POST | Trigger memory evolution |
| `/api/v1/compact` | POST | Force diary write + compaction |
| `/api/v1/index` | POST | Run workspace indexing |
| `/api/v1/index/status` | GET | Workspace index status |
| `/api/v1/consolidate` | POST | Extract facts from workspace files |
| `/api/v1/maintain` | POST | Run memory maintenance |

Auth: Bearer token from `LUCYD_HTTP_TOKEN` env var. Localhost is trusted.

For `/chat`, an `asyncio.Future` is attached to the queue item — `_process_message` resolves it with the reply. For `/notify`, no Future — the event routes to the operator's session via `notify_target`, and the reply is delivered through the connected bridge.

## Tool System

Tools are Python modules in `tools/` exporting a `TOOLS` list. Plugins in `plugins.d/` use the same format. Both are gated by `[tools] enabled`.

`ToolRegistry.execute()` returns structured results:

```python
{"text": "Human-readable output for the LLM", "attachments": ["/path/to/file"]}
```

Attachments are collected across all tool calls in the agentic loop and included in the reply. Bridges deliver them alongside the text response.

Built-in tools (14): `read`, `write`, `edit`, `exec`, `web_search`, `web_fetch`, `memory_search`, `memory_get`, `memory_write`, `memory_forget`, `commitment_update`, `sessions_spawn`, `session_status`, `load_skill`.

Plugins: `tts` (text-to-speech via ElevenLabs, returns audio attachment).

## Session Storage

Dual-format persistence in `$DATA_DIR/sessions/`:

| Format | File | Purpose |
|---|---|---|
| JSONL | `{id}.{YYYY-MM-DD}.jsonl` | Append-only audit trail (daily-split) |
| State | `{id}.state.json` | Atomic snapshot for fast resume |

Sessions are keyed by sender name. `notify_target` routes notifications to the operator's session. System sessions auto-close after processing.

Compaction triggers when `input_tokens` exceeds `threshold_tokens`: oldest messages are summarized via LLM, keeping the newest third verbatim.

## Memory

SQLite FTS5 + vector similarity at `$DATA_DIR/memory/main.sqlite`. FTS-first — handles ~80% of queries without an embedding API call.

**Structured memory:** Facts (entity-attribute-value), episodes (session summaries), commitments (trackable promises). Extracted automatically via `lucydctl --consolidate` and written directly by agent tools.

**Recall:** At session start, injects relevant facts, episodes, and open commitments into the dynamic context. Budget-aware — drops lowest-priority blocks first.

**Evolution pipeline:** `:05` git commit → `:10` `lucydctl --index` → `:15` `lucydctl --consolidate` → `4:05` `lucydctl --maintain` → `4:20` `lucydctl --evolve`.

## Provider Abstraction

`LLMProvider` protocol: `capabilities`, `format_tools`, `format_system`, `format_messages`, `complete`, `stream`. Three implementations: Anthropic (prompt caching, extended thinking), OpenAI-compatible (embeddings, local models), smoke-test (deterministic, offline).

Model definitions live in `providers.d/*.toml`. Each declares a connection type, API key env var, and `[models.*]` sections.

## Context Building

System prompt assembled in three cache tiers:

| Tier | Content | Caching |
|---|---|---|
| stable | Personality files, tool descriptions | Cached aggressively |
| semi-stable | MEMORY.md, always-on skills, skill index | Shorter TTL |
| dynamic | Date/time, sender, source framing, memory recall | Never cached |

Source-aware framing tells the agent what kind of session it's in (system automation, HTTP integration, or conversational).

## Cost Tracking

Every LLM call records to `metering.db`: tokens, cost (EUR), model, provider, session, latency. Query via `lucydctl --cost` or `GET /api/v1/cost`.
