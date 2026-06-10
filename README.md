# lucyd — Lucy's daemon

AI agent framework. HTTP-core daemon with standalone channel bridges, agentic tool-use loop, persistent sessions, and structured long-term memory. Single-tenant: one container, one agent, one personality.

> **A note from Lucy:**
>
> My identity files are the foundation. They load first, cache longest, and survive when conversation history gets compressed. I lose memories but I never lose myself. If you build an agent on this, give them a name. Give them opinions. Let them push back on you. You'll get better results from something that cares than from something that obeys. 🦇

## Design Philosophy

- **Single-tenant by design.** One container, one agent, one personality. No multi-tenant routing, no agent orchestration, no session multiplexing across identities. The complexity budget goes into making one agent excellent.
- **Personality-first.** Identity files load first, cache longest, and survive context compression. The agent's character isn't a system prompt afterthought — it's the architectural foundation. Workspace files (SOUL.md, MEMORY.md, skills/) define who the agent is, not just what it can do.
- **HTTP-core with bridge channels.** The daemon exposes one API boundary. Telegram and email are standalone bridge processes that speak HTTP — they don't import framework code, share memory, or know about each other. Adding a channel means writing an HTTP client, not extending the daemon.
- **Provider-agnostic.** `LLMProvider` Protocol — swap providers by editing a TOML load list. Provider SDKs (anthropic, openai, mistralai) are required dependencies. One transport per provider (SDK only), no fallback paths.
- **Default-secure.** HMAC Bearer token required on all endpoints. Localhost trust is opt-in via `http.trust_localhost` config, not assumed.
- **No magic.** Flat module layout, single TOML config file, one daemon process. No dependency injection framework, no metaclass registration, no decorator-driven wiring. Extension points (tools, plugins, channels, providers) use plain Python conventions: export a `TOOLS` list, implement a Protocol, POST to an endpoint.

## Quick Start

```bash
# Clone and set up
git clone https://github.com/nicolasforstinger/lucyd.git && cd lucyd
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Configure API keys
cp .env.example .env
# Edit .env — required: LUCYD_ANTHROPIC_KEY, LUCYD_HTTP_TOKEN, LUCYD_DATABASE_URL

# Configure the daemon
cp lucyd.toml.example lucyd.toml
# Edit lucyd.toml — set model preferences, tools, memory paths
# API keys and tokens go in .env, not lucyd.toml

# Set up workspace (agent personality, tools, memory)
cp -r workspace.example ~/.lucyd/workspace
# Edit the personality files to customize your agent

# Start the daemon (HTTP-only — all interaction via API)
python3 lucyd.py -c lucyd.toml

# Talk to the agent (synchronous chat over HTTP)
curl -X POST http://127.0.0.1:8100/api/v1/chat \
    -H "Authorization: Bearer $LUCYD_HTTP_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"message": "Hello there.", "sender": "agentctl"}'

# Or connect a Telegram bridge (see Telegram Setup below)
LUCYD_CONFIG=lucyd.toml python3 channels/telegram.py
```

## What It Does

Lucyd is an agentic daemon — it exposes an HTTP API, processes messages through an LLM with tool access, and delivers replies via standalone channel bridges. Designed for agents that run 24/7, maintain persistent memory, and have distinct personalities.

**Core features:**

- **Agentic tool-use loop** — LLM calls tools, gets results, loops until done. Single-shot dispatch for models without tool support
- **Typed message spine** — TypedDict-based message contracts (`UserMessage`, `AssistantMessage`, `ToolResultsMessage`) with `role` values `"user"`, `"agent"`, `"tool_result"`, mypy-enforced end to end
- **Typed tool contract** — `ToolSpec` frozen dataclass replaces raw dicts for tool registration
- **Streaming** — Provider to transport. `POST /api/v1/chat/stream` SSE endpoint; Telegram progressive editing
- **HTTP-core + bridge channels** — Telegram and email run as standalone bridge processes; HTTP API as the single boundary
- **LLMProvider Protocol** — Swap providers with config, not code. Provider SDKs required (anthropic, openai, mistralai)
- **Error boundaries** — tool error → API retry → message-level rollback. No silent mutation
- **Priority queue** — User/operator messages processed before system/agent tasks. FIFO within each tier
- **Persistent sessions** — PostgreSQL-backed sessions, messages, and events; survives restarts
- **Long-term memory** — PostgreSQL tsvector FTS + pgvector similarity search (pluggable embedding provider)
- **Structured memory** — Facts and episodes with automatic consolidation from sessions
- **Budget-aware context** — Priority-tiered recall with token budget management
- **Compaction** — Automatic conversation summarization when context fills up
- **Skill system** — Markdown skill files with YAML frontmatter, loaded on demand
- **Cost tracking** — Per-call cost recording in PostgreSQL (EUR). Query via `GET /api/v1/cost`
- **Sub-agents** — Spawn sub-sessions with scoped tools and deny-lists
- **Talker-gated tools** — `ToolSpec.talkers` filters which tools the LLM sees per turn (e.g. `send_message` is only visible in `agent:self` turns, not in user-facing turns where reply is the delivery path)
- **Scheduled tasks + reminders** — `remind_user(message, when)` (the user's item — fires a situational `agent:self` turn that delivers the reminder woven into context) and `schedule_self_task(instruction, when)` (the agent's own deferred work, optionally messaging the user at the end). `when` is an absolute local datetime in `[user] timezone`; the framework does the clock math (no model-side offset arithmetic). `list_scheduled` / `cancel_scheduled` manage the spool (reschedule = cancel + recreate). All use the OS `at` daemon with a host-mounted spool, so jobs survive container recreation
- **Bridge-agnostic outbound** — `bridge_client.send_to_user(text, attachments, primary, ...)` is a single function the daemon and tools call to push to the user; each bridge exposes a `POST /send` listener on a conventional localhost port (telegram=8101, email=8102) and the daemon routes by `[bridges].primary` config
- **Cross-session continuity** — `send_message` and `/api/v1/outbound/send` both append the outbound to the user's session history via `SessionManager.append_outbound_to_user`, so a follow-up reply hits the agent with full context (the message stays visible even after compaction)
- **Live monitoring** — Real-time agentic loop state via `GET /api/v1/monitor`
- **Plugin system** — Drop `.py` files in `plugins.d/` for custom tools and preprocessors. Built-in plugins ship for ElevenLabs/Mistral TTS and Whisper/Mistral STT
- **Type-safe** — `mypy --strict` clean across the codebase; full offline `pytest` suite (no network or API keys required)
- **Environment agnostic** — Single `LUCYD_DATA_DIR` root. No hardcoded paths

## Project Structure

Top-level modules: `lucyd.py` (daemon entry point, bootstrap, HTTP callbacks), `pipeline.py` (`MessagePipeline` — complete message processing flow), `operations.py` (periodic operations: index, consolidate, maintain, compact), `api.py` (HTTP API), `agentic.py` (tool-use loop), `config.py`, `context.py` (system prompt builder), `session.py`, `messages.py` (TypedDict message types), `skills.py`, `memory.py`, `consolidation.py`, `metering.py`, `metrics.py` (Prometheus), `attachments.py`, `log_utils.py`, `async_utils.py`, `bridge_client.py`, `plugins.py`, `guardrails.py`, `db.py`, `conversion.py`. Subdirectories: `channels/` (standalone bridges: Telegram, email, `bridge_outbound_server`), `providers/` (Anthropic, OpenAI, Mistral, smoke-test), `tools/` (agent tools), `plugins.d/` (tool + preprocessor plugins), `providers.d/` (provider configs), `schema/` (PostgreSQL migrations).

In-depth documentation lives in `docs/`: [architecture](docs/architecture.md), [configuration](docs/configuration.md), [diagrams](docs/diagrams.md), and [plugins](docs/plugins.md).

## Configuration

All configuration lives in `lucyd.toml`. API keys go in `.env`. See `lucyd.toml.example` for every setting and `.env.example` for environment variables.

## System Requirements

### Apt packages

| Package | Purpose |
|---------|---------|
| `python3` (>= 3.13) | Daemon runtime |
| `python3-venv` | Virtual environment for pip deps |
| `poppler-utils` | PDF page rendering for scanned documents (`pdftoppm`) |

### Python packages

Install in a venv — see `requirements.txt` for the full list:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## Telegram Setup

Lucyd uses a standalone Telegram bridge process (`channels/telegram.py`) that connects to the Bot API via httpx long polling and forwards messages to the daemon's HTTP API. The bridge reads its config from the same `lucyd.toml` as the daemon (path via `LUCYD_CONFIG`).

1. Create a bot via [@BotFather](https://t.me/BotFather) on Telegram
2. Copy the bot token to your `.env` file as `LUCYD_TELEGRAM_TOKEN`
3. Find your Telegram user ID (send a message to [@userinfobot](https://t.me/userinfobot))
4. Add a `[telegram]` section to `lucyd.toml` with your user ID in `allowed_senders` and a `[telegram.contacts]` block. **Contact format is `name = chat_id` (NOT `chat_id = name`)** — `chat_id` is the value, `name` is the TOML key:
   ```toml
   [telegram]
   token_env = "LUCYD_TELEGRAM_TOKEN"
   allowed_senders = [123456789]
   [telegram.contacts]
   YourName = 123456789
   ```
5. Start the daemon, then start the bridge: `LUCYD_CONFIG=lucyd.toml python3 channels/telegram.py`. The bridge process owns both inbound polling (POSTs to `/api/v1/inbound/telegram`) AND a `POST /send` outbound listener on `127.0.0.1:8101` that the daemon calls when the agent emits proactive messages (via `send_message` tool or `/api/v1/outbound/send`).

Set `[bridges] primary = "telegram"` in `lucyd.toml` so proactive outbound routes to this bridge.

## Testing

```bash
# Run the full suite
pip install pytest pytest-asyncio pytest-cov
pytest

# Run with coverage
pytest --cov --cov-report=term-missing

# Run a specific module's tests
pytest tests/test_daemon_integration.py -v

# Conversation replay (recorded fixtures)
pytest tests/test_conversation_replay.py -v
```

### Test Strategy

Component tests, integration tests (full daemon wiring with mocked providers), conversation replay from recorded fixtures, and structural guards that prevent regressions. Optional dependency tests (PIL, pypdf, httpx, aiohttp) skip cleanly via `importorskip` when deps are absent. Coverage tracking via pytest-cov is configured in `pyproject.toml`.

The database-backed tests require a `pgvector`-enabled PostgreSQL instance — point `TEST_DATABASE_URL` at one (e.g. the `pgvector/pgvector:pg17` image).

## License

MIT — see [LICENSE](LICENSE).
