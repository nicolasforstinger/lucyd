# Architecture

How Lucyd fits together. Read this when you need to fix something, add a feature, or understand why a message didn't arrive.

## Overview

HTTP API is the single boundary. Bridges (Telegram, email) are standalone processes that POST inbound messages to the daemon and receive replies in the HTTP response. The daemon processes messages through an LLM with tool access. SSE streaming is available via `/chat/stream`.

Bridges are **bidirectional**: alongside their inbound poll loop, each bridge runs a `POST /send` listener on a conventional localhost port (telegram=8101, email=8102) so the daemon can push proactive messages — used by the `send_message` tool (proactive outbound from `agent:self` turns) and by at-jobs from `remind_user` (scheduled literal reminders that fire without an LLM call). See [bridge contract](#bridge-contract--proactive-outbound) below.

## Module Map

| File | Purpose |
|---|---|
| `lucyd.py` | Daemon coordinator. Bootstrap, priority message queue, signal handlers, init methods. Delegates processing to `MessagePipeline` and periodic operations to `operations.py`. |
| `pipeline.py` | `MessagePipeline`. Complete message processing: preprocessors, attachments, session setup, recall, context, agentic loop, response finalization. Owns monitor state, error counts, per-session locks. |
| `operations.py` | Periodic operations: index, consolidate, maintain, compact. Standalone functions called by daemon HTTP handlers. |
| `agentic.py` | Provider-agnostic tool-use loop. `run_agentic_loop` (multi-turn) and `run_single_shot` dispatch. Collects tool attachments. |
| `api.py` | HTTP API server (aiohttp). Envelope extraction, sender validation, auth, rate limiting. |
| `config.py` | TOML config loader with env overrides. `_SCHEMA`-based typed properties + `raw()` for plugin config. Defines `Talker`, `OPERATOR_SENDERS`, `SYSTEM_SENDERS`, `AGENT_SENDERS`. |
| `context.py` | System prompt builder. Three cache tiers (stable / semi-stable / dynamic). Token budget enforcement. |
| `db.py` | PostgreSQL connection pool (asyncpg) + forward-only schema versioning from `schema/*.sql`. |
| `session.py` | Session manager. PostgreSQL-backed sessions, messages, events. Compaction via LLM. `append_outbound_to_user` for cross-session outbound. |
| `memory.py` | Long-term memory. PostgreSQL tsvector FTS + pgvector similarity. Structured recall (facts, episodes, commitments). |
| `consolidation.py` | Structured data extraction from sessions and workspace files via LLM. Facts, episodes, commitments, aliases. |
| `skills.py` | Skill loader + `load_skill` tool. Markdown with YAML frontmatter. |
| `metering.py` | Per-call cost recording to PostgreSQL (`metering.costs`). Retention via `enforce_retention`. EUR. |
| `metrics.py` | Prometheus metrics. 34 metric families, graceful no-op if `prometheus_client` not installed. |
| `attachments.py` | `Attachment` dataclass, image fitting (`fit_image`), document text extraction (`extract_document_text`). Pure functions. |
| `guardrails.py` | Tripwire registry. Async input/output predicates raising `GuardrailTripped`; pipeline halts the run on trip. |
| `log_utils.py` | Log sanitization, structured JSON formatter, context vars. |
| `async_utils.py` | `run_blocking()` for safe blocking I/O offload. |
| `plugins.py` | Typed `PluginError` hierarchy, `PreprocessorSpec`, `run_plugin_op` (retry + Prometheus emission), `mark_configured` / `mark_unconfigured` / `list_plugin_health`. |
| `bridge_client.py` | Channel-agnostic outbound primitive. `BRIDGE_LIMITS` (port + max bytes per bridge), `send_to_user` function, `BridgeDeliveryError`. |
| `conversion.py` | `CurrencyConverter` — fetches FX rate (Frankfurter API or static) and converts provider-native costs to EUR. |
| `channels/telegram.py` | Telegram bridge. Polls getUpdates, POSTs `/api/v1/inbound/telegram`, runs outbound `/send` listener on 8101. Config: `[telegram]` section in `lucyd.toml`. |
| `channels/email.py` | Email bridge. IMAP poll → POST `/api/v1/inbound/email`; SMTP send via outbound `/send` listener on 8102. Config: `[email]` section in `lucyd.toml`. |
| `channels/bridge_outbound_server.py` | Shared `aiohttp` `POST /send` app builder. Each bridge calls `build_outbound_app(...)` to wire its outbound listener. |
| `providers/__init__.py` | `LLMProvider` protocol, data types (`LLMResponse`, `StreamDelta`, `Usage`, `ModelCapabilities`, `CostContext`, `ToolCall`), factory. |
| `providers/anthropic.py` | Anthropic provider via official SDK. Prompt caching, extended thinking. |
| `providers/openai.py` | OpenAI-compatible provider via SDK. Embeddings, `<think>` extraction, JSON repair. |
| `providers/mistral.py` | Mistral provider via SDK. Tool use, vision, streaming. |
| `providers/smoke_local.py` | Deterministic test provider. No network. |
| `tools/__init__.py` | `ToolRegistry`. Dispatch, error isolation, JSON-aware truncation. `ToolSpec.talkers` filters for context-gated tools. |
| `tools/filesystem.py` | `read`, `write`, `edit`, `send_file`. Path allowlist enforcement. |
| `tools/shell.py` | `exec`. Subprocess with timeout, env filtering, process group kill. |
| `tools/web.py` | `web_search` (Brave), `web_fetch`. SSRF protection, DNS pinning. |
| `tools/memory_read.py` | `memory_search`, `memory_get`. tsvector + vector + structured recall. |
| `tools/memory_write.py` | `memory_write`, `memory_forget`, `commitment_update`. |
| `tools/reminder.py` | `remind_user` (literal scheduled message via `at` → `/api/v1/outbound/send`), `schedule_self_task` (future agent:self turn via `at` → `/api/v1/agent/action`), `list_scheduled` / `cancel_scheduled` (manage the at-spool). |
| `tools/send_message.py` | Proactive outbound to user — gated to `talkers={"agent"}`. Validates per-bridge attachment caps, calls `bridge_client.send_to_user`, then `SessionManager.append_outbound_to_user`. |
| `tools/agents.py` | `sessions_spawn`. Sub-agent with scoped tools and deny-list. |
| `tools/status.py` | `session_status`. Context utilization, uptime, cost. |
| `tools/indexer.py` | Workspace file indexer for memory. Used by `POST /api/v1/index`. |
| `tools/gdpr.py` | `gdpr_search`, `gdpr_redact`. |
| `tools/pdf.py` | `pdf_read`. Text extraction with page control via `pypdf`. |
| `messages.py` | TypedDict message types: `UserMessage` (`role="user"`), `AssistantMessage` (`role="agent"`), `ToolResultsMessage` (`role="tool_result"`), `Message` union. |
| `plugins.d/whisper.py` | STT preprocessor plugin. OpenAI Whisper SDK (cloud) + whisper.cpp (local). Config: `plugins.d/whisper.toml`. |
| `plugins.d/elevenlabs.py` | TTS tool plugin. ElevenLabs SDK, cost tracking. Config: `plugins.d/elevenlabs.toml`. |
| `plugins.d/mistral_stt.py` | STT preprocessor plugin via Mistral. Config: `plugins.d/mistral_stt.toml`. |
| `plugins.d/mistral_tts.py` | TTS tool plugin via Mistral. Config: `plugins.d/mistral_tts.toml`. |
| `providers.d/*.toml` | Provider config files. Connection type, API key, model sections. |
| `schema/*.sql` | PostgreSQL migrations applied at startup (forward-only, version tracked in `public.schema_version`). |

## Message Flow

```
Bridge (Telegram/email) | Operator (agentctl) | System (cron/automation) | Agent (at-job, a2a)
  |
  | POST one of:
  |   /api/v1/inbound/{telegram,email}     talker=user, sender=config.user.name
  |   /api/v1/chat | /chat/stream          talker=operator, sender=agentctl
  |   /api/v1/system/event                 talker=system, sender ∈ {maintenance,automation,error}
  |   /api/v1/agent/action                 talker=agent, sender ∈ {self,other}
  | body: {message|text, sender, attachments?}      ← talker is pinned by endpoint
  v
HTTPApi (api.py)
  | validate sender against allowed set for the talker
  | enqueue {talker, sender, text, attachments?} (priority queue: user > system/agent)
  v
PriorityMessageQueue (lucyd.py)
  v
_message_loop → MessagePipeline.process_message(ctx)
  |
  |  ctx.session_key = f"{talker}:{sender}"
  |
  |-- _run_preprocessors          plugins claim/transform attachments
  |-- _process_attachments        images scaled, documents extracted
  |-- _setup_session              get_or_create(session_key), inject timestamp
  |-- _build_recall               structured memory recall
  |-- _build_context              system prompt: stable + semi-stable + dynamic tiers
  |-- ctx.tools = tool_registry.get_schemas_for_talker(ctx.talker)   ← talker filter
  |-- _ensure_context_budget      emergency compact if context > 80%
  |-- _run_agentic                run_agentic_loop or run_single_shot (agentic.py)
  |     +-- _call_provider_with_retry  provider.complete()/stream() with API-level retry
  |          → tool_use → ToolRegistry.execute() → loop until end_turn or max_turns
  |
  +-- _finalize_response
        |-- _persist_response     append to PostgreSQL sessions.messages
        |-- _deliver_reply        talker-driven:
        |     user / operator     resolve HTTP future with reply (bridge sends it back)
        |     system / agent      silent — reply is logged but not delivered
        |-- _check_compaction_warning / _run_compaction_if_needed
        +-- _auto_close_if_ephemeral   close if talker ∈ {system, agent}

# Out-of-band proactive outbound (no incoming message)
send_message tool (in agent:self turn)  ──┐
POST /api/v1/outbound/send (at-job from   ├──> bridge_client.send_to_user
remind_user, automation, etc.)            │      → POST 127.0.0.1:<bridge_port>/send
                                          │      → bridge sends via channel
                                          └──> SessionManager.append_outbound_to_user
                                               → AssistantMessage in user:{user_name}
                                                 (so follow-up reply has context)
```

Metrics fire at: preprocessor execution, context utilization calculation, agentic loop (API calls, tokens, cost, latency), tool execution, message completion (count, duration, cost, turns), session close, compaction, errors.

### Internal Message Format

Messages are typed via `messages.py` — three TypedDicts discriminated by `role`:

- **`UserMessage`**: `{"role": "user", "content": str}` — content is always `str` at rest. Transient `_image_blocks` key added during image processing, stripped before persistence.
- **`AssistantMessage`**: `{"role": "agent", "text": ..., "tool_calls": [...], "usage": {...}, "thinking": ..., "thinking_block": ..., "attachments": ..., "source_metadata": ...}` — all fields except `role` are `NotRequired`. `attachments`/`source_metadata` populated only by `SessionManager.append_outbound_to_user`.
- **`ToolResultsMessage`**: `{"role": "tool_result", "results": [{"tool_call_id": str, "tool_name": str, "content": str}]}`

The union `Message = UserMessage | AssistantMessage | ToolResultsMessage` is used throughout the pipeline (`session.messages`, agentic loop params, consolidation). mypy narrows via `msg["role"] == "user"` checks. Provider `format_messages(messages: list[Message]) -> list[dict[str, Any]]` is the boundary between internal and provider-specific formats. TypedDicts are plain dicts at runtime — no serialization change, no wire format change.

## Daemon State Architecture

`LucydDaemon` is a composition root — it wires dependencies at startup and delegates processing. `MessagePipeline` owns message processing state. `operations.py` provides standalone periodic functions.

### Decomposed Structure

```
LucydDaemon (lucyd.py)
  ├── Bootstrap: _init_provider, _init_sessions, _init_skills,
  │              _init_context, _init_metering, _init_conversion, _init_tools
  ├── Event loop: _message_loop, PriorityMessageQueue, _control_queue, running
  ├── Signals: SIGUSR1 (re-scan skills), SIGTERM/SIGINT (stop)
  └── Delegates to:
        ├── MessagePipeline (pipeline.py)
        │     process_message → _setup_session → _build_context
        │       → _ensure_context_budget → _run_agentic
        │       → _finalize_response → _auto_close_if_ephemeral
        │     Owns: session_mgr, tool_registry, context_builder,
        │           metering_db, _preprocessors, error_counts,
        │           monitor_state, current_session, session locks
        └── operations.py
              handle_index, handle_index_status,
              handle_consolidate, handle_maintain, handle_compact,
              consolidate_on_close
```

`LucydDaemon._process_message()` is a thin delegator to `pipeline.process_message()`. The daemon owns init, the priority queue, signal handling, and HTTP-handler callbacks; the pipeline owns per-message processing.

### Write-Once-Read-Many Topology

Most heavily-shared attributes are written exactly once at startup:

| Attribute | Owner | Written by |
|---|---|---|
| `pool` (asyncpg) | daemon | `run()` (after `_acquire_pid_file`) |
| `provider`, `_providers`, `_single_shot` | daemon | `_init_provider` |
| `session_mgr` | daemon | `_init_sessions` |
| `skill_loader` | daemon | `_init_skills` |
| `context_builder` | daemon | `_init_context` |
| `metering_db` | daemon | `_init_metering` |
| `converter` | daemon | `_init_conversion` (optional) |
| `tool_registry`, `_preprocessors` | daemon | `_init_tools` |
| `pipeline` | daemon | `run()` after `_init_tools` |

### Rules for New Code

1. **Message processing goes in pipeline.py.** Per-message logic belongs on `MessagePipeline`. Daemon methods should only handle lifecycle, init, and HTTP handler callbacks.
2. **Periodic operations go in operations.py.** Standalone functions, not daemon methods.
3. **Don't add write sites.** Create new shared resources in `_init_*` methods. Let operational code read them.
4. **Core never imports from plugins.d/.** Plugins resolve their own config via `config.raw()` and validate their own dependencies in `configure()`.

## HTTP API

Endpoints registered in `api.py::HTTPApi.start()`.

| Endpoint | Method | Auth | Talker | Purpose |
|---|---|---|---|---|
| `/api/v1/chat`, `/api/v1/chat/stream` | POST | yes | `operator` | Operator request/response (sync + SSE) |
| `/api/v1/inbound/telegram`, `/api/v1/inbound/email` | POST | yes | `user` | Bridge inbound — auto-injects `[user] name` |
| `/api/v1/inbound/whatsapp` | POST | yes | — | Reserved (returns 501; no bridge implemented) |
| `/api/v1/system/event` | POST | yes | `system` | External events (cron, automation, error). Senders: `maintenance`, `automation`, `error` |
| `/api/v1/agent/action` | POST | yes | `agent` | Agent self-actions (scheduled-task fires, a2a). Senders: `self`, `other` |
| `/api/v1/outbound/send` | POST | yes | — (no session) | Daemon-side proactive outbound to user via primary bridge. Used by at-jobs from `remind_user`. Also appends to `user:{user_name}` for follow-up continuity. |
| `/api/v1/status` | GET | no | — | Health check + daemon stats. Updates Prometheus gauges. |
| `/metrics` | GET | no | — | Prometheus metrics exposition |
| `/api/v1/sessions` | GET | yes | — | List active sessions |
| `/api/v1/cost` | GET | yes | — | Cost records by billing period (YYYY-MM) |
| `/api/v1/monitor` | GET | yes | — | Live agentic loop state |
| `/api/v1/sessions/reset` | POST | yes | — | Reset (close) sessions by target |
| `/api/v1/sessions/{id}/history` | GET | yes | — | Session event history |
| `/api/v1/compact` | POST | yes | — | Force diary write + compaction on user session |
| `/api/v1/index` | POST | yes | — | Run workspace indexing |
| `/api/v1/index/status` | GET | yes | — | Workspace index status |
| `/api/v1/consolidate` | POST | yes | — | Extract facts from workspace files |
| `/api/v1/maintain` | POST | yes | — | Run memory maintenance + metering retention |
| `/api/v1/plugins` | GET | yes | — | Plugin registration state (configured / unconfigured) |
| `/api/v1/plugins/{name}/health` | GET | yes | — | Single plugin health |

Auth: Bearer token from `LUCYD_HTTP_TOKEN` env var. Auth-exempt paths: `/api/v1/status`, `/metrics`.

### Talker / sender envelope

Every inbound HTTP message declares a `talker` (pinned by the endpoint, never overridable from the body) and a `sender` (enumerated per talker). Session key = `f"{talker}:{sender}"`. Behaviors (auto-close, memory feed, reply path) derive from the talker class alone.

| Talker | Allowed senders | Source |
|---|---|---|
| `operator` | `agentctl` only | `OPERATOR_SENDERS` (config.py) |
| `user` | `config.user.name` (single-tenant single-user) | inbound channels |
| `system` | `maintenance`, `automation`, `error` | `SYSTEM_SENDERS` |
| `agent` | `self`, `other` | `AGENT_SENDERS` |

System sender descriptions:
- `maintenance` — internal framework operations (consolidate, compact, maintain)
- `automation` — external automation events (n8n, webhooks)
- `error` — bridge delivery failures (Telegram/email can't reach the user)

Auto-close behavior is talker-driven: `system` and `agent` sessions are ephemeral (close after each message), `user` and `operator` sessions persist. Reply delivery is also talker-driven: `system` and `agent` reply paths are silent by construction; proactive delivery happens via `send_message` (tool, in `agent:self` turns) or `/api/v1/outbound/send` (endpoint, e.g. from `at`-job).

### Request Flow

For `/api/v1/chat` and `/api/v1/inbound/{telegram,email}`: an `asyncio.Future` is attached to the queue item. `MessagePipeline.process_message` resolves it via `_deliver_reply`. The HTTP handler awaits the Future with `agent_timeout_seconds` timeout, returns 408 on expiry.

For `/api/v1/chat/stream`: a `asyncio.Queue` is attached in addition. The pipeline pushes SSE events as the provider streams. The HTTP handler reads the queue and writes SSE frames.

For `/api/v1/system/event` and `/api/v1/agent/action`: fire-and-forget. No Future. Returns 202 immediately and processes via the priority queue (system/agent priority is below user/operator).

## Plugin System

Plugins are Python files in `plugins.d/`. They export `TOOLS` (tool definitions) and/or `PREPROCESSORS` (attachment transformers). Tools are gated by `[tools] enabled`. Preprocessors register unconditionally when the plugin loads.

At startup, `_init_tools()` scans `plugins.d/*.py`, loads each via `importlib.util`, calls `configure()` with inspect-based dependency injection, then registers tools and preprocessors.

Core never imports from `plugins.d/`. Plugins access their config via `config.raw()` and validate their own dependencies in `configure()`.

`ToolSpec.talkers: frozenset[str] | None` filters which tools the LLM is told about per turn — `None` (default) is universal; a set restricts visibility. Today `send_message` uses `talkers={"agent"}` so it only appears in `agent:self` turns where reply delivery is silent and proactive outbound needs an explicit primitive.

See [Plugin & Channel Guide](plugins.md) for the full developer reference.

## Bridge contract — proactive outbound

The daemon has a single channel-agnostic outbound primitive,
`bridge_client.send_to_user(text, attachments, primary, token, http_client)`.
Both the `send_message` tool and the `POST /api/v1/outbound/send`
endpoint call it; no other code path talks to a bridge for outbound.

Each bridge implements the same `POST /send` listener via the shared
helper `channels.bridge_outbound_server.build_outbound_app(...)`:

```
POST http://127.0.0.1:<port>/send         (Bearer auth: LUCYD_HTTP_TOKEN)
{ "text": str, "attachments": [{filename, content_type, data_b64}]? }
→ 200 { "delivered": true } | 4xx/5xx { "error": "..." }
```

Conventional ports + per-bridge attachment caps live in
`bridge_client.BRIDGE_LIMITS`: telegram=8101 (50 MB), email=8102 (20 MB).

`[bridges] primary` in `lucyd.toml` selects the active outbound target.
No fanout — single-tenant, single-channel by design. Adding a bridge =
adding a row to `BRIDGE_LIMITS` + wiring `build_outbound_app` in the
bridge's `main()` alongside its inbound poll loop.

### Cross-session continuity

When `send_message` or `/api/v1/outbound/send` deliver successfully,
they ALSO call `SessionManager.append_outbound_to_user(target_key=
f"user:{user_name}")` to persist the outbound as an `AssistantMessage`
in the user's session. Without this, proactive messages disappear from
the agent's context as soon as compaction runs — and a follow-up reply
from the user hits a confused agent.

The append is locked via `pipeline.get_session_lock(user_session_key)`
to serialize against in-flight user turns. Empty user sessions get a
synthetic anchor `UserMessage` prepended so provider role-alternation
holds for the next provider call.

### Stability boundary — at-job target endpoints

Two HTTP endpoint contracts are public APIs frozen as long as any
in-flight `at` job may exist in the spool:

- `POST /api/v1/agent/action` body: `{"message": "[Scheduled task] <instruction>", "sender": "self"}`
- `POST /api/v1/outbound/send` body: `{"text": "...", "attachments": [...]?}`

The shell scripts written by `remind_user` and `schedule_self_task` at
scheduling time embed those URLs and bodies; the at-spool stores the
job content verbatim (named volume on `/var/spool/cron/atjobs` so it
survives container recreation, watchtower updates, and deploys).

If either body shape needs to change: verify the spool is empty
(`docker exec <agent> at -l`) OR support both old and new shapes until
the spool is provably drained.

## Session Storage

All session state lives in PostgreSQL (`schema/001_initial.sql`):

| Table | Purpose |
|---|---|
| `sessions.sessions` | Per-session row keyed by `id` (UUID), with `contact` (`talker:sender`), `model`, token totals, compaction count, warning state |
| `sessions.messages` | Per-message rows: `session_id`, `role`, `content` (JSONB), `ordinal`, `created_at`. Append-only via `save_state`; `replace_all_messages` is used by compaction. |
| `sessions.events` | Audit log of session lifecycle events (creation, messages, tool results, compaction). Used by `GET /sessions/{id}/history`. |

Sessions are keyed by `talker:sender` (e.g., `user:Nicolas`, `operator:agentctl`, `system:maintenance`). The key is computed in `process_message` as `f"{talker}:{sender}"` and passed to `session_mgr.get_or_create()`.

Ephemeral sessions (talker `system` or `agent`) auto-close after processing via `_auto_close_if_ephemeral` — but only if the session was created by this event (pre-existing sessions are never auto-closed).

Compaction triggers when `input_tokens` exceeds `compaction.threshold_tokens` OR when the pre-loop context budget exceeds 80% (emergency compaction). Oldest messages are summarized via the `compaction` model role, keeping the newest `keep_recent_pct` fraction verbatim (`SessionManager.compact_session`). Consolidation runs first when `[memory.consolidation] enabled = true` — fact extraction from messages must succeed before compaction overwrites them.

On close, `on_close` callbacks fire (consolidation); the row in `sessions.sessions` is marked `closed_at` and removed from the in-memory pool.

## Context Building

System prompt assembled in `ContextBuilder.build()` (context.py) across three cache tiers:

| Tier | Content | Caching |
|---|---|---|
| stable | Personality files (SOUL.md, etc.), tool descriptions | Cached aggressively |
| semi-stable | MEMORY.md, always-on skill bodies, skill index | Shorter TTL |
| dynamic | Date/time, talker framing, sender, framework conventions, silent tokens, limits, memory recall, image ephemerality hint | Never cached |

Talker framing (`ContextBuilder._build_dynamic`) tells the agent who is speaking and what session lifecycle to expect:

| Talker | Framing |
|---|---|
| `user` | "Messages come from the person you serve … conversation history is preserved and feeds your memory." |
| `operator` | "Messages come from an administrator (agentctl). Conversation persists for the operator session but does NOT feed user memory." |
| `system` | "Automated infrastructure events … process and reply internally — nothing is delivered to any channel, this session closes after your reply." |
| `agent` | "This message is from you (a scheduled self-action or agent-to-agent event). Execute and return — no outbound delivery, session closes after reply." |

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

Four implementations, all SDK-only (no HTTP fallback): Anthropic (prompt caching, extended thinking), OpenAI-compatible (embeddings, local models, `<think>` extraction), Mistral (tool use, vision, streaming), smoke-test (deterministic, offline). Model definitions live in `providers.d/*.toml`.

## Metrics

34 Prometheus metric families (metrics.py). Graceful no-op when `prometheus_client` is not installed. Exposed at `GET /metrics`.

| Scope | Metrics | Labels |
|---|---|---|
| Per-message | `lucyd_messages_total`, `lucyd_message_duration_seconds`, `lucyd_message_cost_eur`, `lucyd_agentic_turns`, `lucyd_context_utilization_ratio`, `lucyd_message_outcome_total` | talker, session_id, sender; outcome |
| Per-provider | `lucyd_api_calls_total`, `lucyd_api_latency_seconds`, `lucyd_tokens_total`, `lucyd_api_cost_eur_total`, `lucyd_ttft_seconds`, `lucyd_api_retries_total` | model, provider, status/direction |
| Per-tool | `lucyd_tool_calls_total`, `lucyd_tool_duration_seconds` | tool_name, status |
| Per-preprocessor | `lucyd_preprocessor_total`, `lucyd_preprocessor_duration_seconds` | name, status |
| Per-plugin | `lucyd_plugin_calls_total`, `lucyd_plugin_duration_seconds`, `lucyd_plugin_retries_total`, `lucyd_plugin_configured` | plugin, operation, status, code, backend |
| Memory | `lucyd_memory_ops_total`, `lucyd_memory_search_duration_seconds` | operation; search_type |
| Session | `lucyd_active_sessions`, `lucyd_compaction_total`, `lucyd_compaction_tokens_reclaimed`, `lucyd_session_close_total`, `lucyd_session_open_total`, `lucyd_consolidation_duration_seconds` | reason (close only) |
| Queue | `lucyd_queue_depth`, `lucyd_queue_wait_seconds` | priority |
| System | `lucyd_uptime_seconds`, `lucyd_workspace_bytes`, `lucyd_errors_total`, `lucyd_fx_fetch_errors_total` | error_type (errors only) |

## Cost Tracking

Every LLM call records to PostgreSQL via `MeteringDB` (metering.py): tokens, cost_eur, fx_rate, model, provider, session, latency. Query via `GET /api/v1/cost?period=YYYY-MM`. Retention enforced by `POST /api/v1/maintain` per `[metering] retention_months` (default: 84 / 7 years).
