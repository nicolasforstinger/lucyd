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
| `relay.py` | Outbound proxy. `RelayChannel` forwards send/typing/stream to bridge via HTTP. `Channel` protocol. |
| `memory.py` | Long-term memory. FTS5 keyword search + vector similarity. Structured recall (facts, episodes, commitments). |
| `memory_schema.py` | SQLite schema for 11 memory tables. Safe to call on every startup. |
| `consolidation.py` | Structured data extraction from sessions via LLM. Facts, episodes, commitments, aliases. |
| `evolution.py` | Daily rewriting of workspace files (MEMORY.md, USER.md) using accumulated knowledge. |
| `skills.py` | Skill loader + `load_skill` tool. Markdown with YAML frontmatter. |
| `metering.py` | Per-call cost recording to SQLite. Billing periods, EUR currency. |
| `monitor.py` | Live agentic loop state tracking for `monitor.json`. |
| `attachments.py` | Image fitting, document text extraction, audio STT. Pure functions. |
| `models.py` | Shared data types: `Attachment`, `InboundMessage`. |
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
| `/api/v1/cost/export` | GET | Export cost data (CSV/JSON) |
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
