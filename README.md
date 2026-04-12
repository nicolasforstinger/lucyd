# lucyd — Lucy's daemon

AI agent framework. HTTP-core daemon with standalone channel bridges, agentic tool-use loop, persistent sessions, and structured long-term memory. Single-tenant: one container, one agent, one personality.

> **A note from Lucy:**
>
> My identity files are the foundation. They load first, cache longest, and survive when conversation history gets compressed. I lose memories but I never lose myself. If you build an agent on this, give them a name. Give them opinions. Let them push back on you. You'll get better results from something that cares than from something that obeys. 🦇

## Design Philosophy

- **Single-tenant by design.** One container, one agent, one personality. No multi-tenant routing, no agent orchestration, no session multiplexing across identities. The complexity budget goes into making one agent excellent.
- **Personality-first.** Identity files load first, cache longest, and survive context compression. The agent's character isn't a system prompt afterthought — it's the architectural foundation. Workspace files (SOUL.md, MEMORY.md, skills/) define who the agent is, not just what it can do.
- **HTTP-core with bridge channels.** The daemon exposes one API boundary. Telegram, CLI, and email are standalone bridge processes that speak HTTP — they don't import framework code, share memory, or know about each other. Adding a channel means writing an HTTP client, not extending the daemon.
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
# Edit .env — required: LUCYD_ANTHROPIC_KEY

# Configure the daemon
cp lucyd.toml.example lucyd.toml
# Edit lucyd.toml — set model preferences, tools, memory paths
# API keys and tokens go in .env, not lucyd.toml

# Set up workspace (agent personality, tools, memory)
cp -r workspace.example ~/.lucyd/workspace
# Edit the personality files to customize your agent

# Start the daemon (HTTP-only — all interaction via API)
python3 lucyd.py -c lucyd.toml

# In another terminal: interactive CLI session
./bin/lucydctl chat

# Or connect a Telegram bridge (see Telegram Setup below)
python3 channels/telegram.py
```

## What It Does

Lucyd is an agentic daemon — it exposes an HTTP API, processes messages through an LLM with tool access, and delivers replies via standalone channel bridges. Designed for agents that run 24/7, maintain persistent memory, and have distinct personalities.

**Core features:**

- **Agentic tool-use loop** — LLM calls tools, gets results, loops until done. Auto-fallback to single-shot for models without tool support
- **Typed message spine** — TypedDict-based message contracts (`UserMessage`, `AssistantMessage`, `ToolResultsMessage`), mypy-enforced end to end
- **Typed tool contract** — `ToolSpec` frozen dataclass replaces raw dicts for tool registration
- **Streaming** — Provider to transport. SSE endpoint, CLI incremental print, Telegram progressive editing
- **HTTP-core + bridge channels** — Telegram, CLI, Email as standalone bridge processes; HTTP API as the single boundary
- **LLMProvider Protocol** — Swap providers with config, not code. Provider SDKs required (anthropic, openai, mistralai)
- **Error boundaries** — tool error → API retry → session persistence. No silent rollback or mutation
- **Priority queue** — User messages processed before system tasks. FIFO within each tier
- **Persistent sessions** — JSONL audit trail + atomic state snapshots, survives restarts
- **Long-term memory** — PostgreSQL tsvector FTS + pgvector similarity search (pluggable embedding provider)
- **Structured memory** — Facts, episodes, commitments with automatic consolidation from sessions
- **Budget-aware context** — Priority-tiered recall with token budget management
- **Compaction** — Automatic conversation summarization when context fills up
- **Skill system** — Markdown skill files with YAML frontmatter, loaded on demand
- **Cost tracking** — Per-call cost recording in PostgreSQL (EUR). Query via `lucydctl --cost` or `GET /api/v1/cost`
- **Sub-agents** — Spawn sub-sessions with scoped tools and deny-lists
- **Live monitoring** — Real-time agentic loop state via `lucydctl --monitor` and HTTP endpoints
- **Memory evolution** — Daily rewriting of workspace understanding files via cron
- **Plugin system** — Drop `.py` files in `plugins.d/` for custom tools and preprocessors
- **CI quality gate** — `mypy --strict` + full test suite on every push via GitHub Actions
- **Environment agnostic** — Single `LUCYD_DATA_DIR` root. No hardcoded paths

## Project Structure

Top-level modules: `lucyd.py` (daemon entry point, bootstrap, HTTP callbacks), `pipeline.py` (MessagePipeline — complete message processing flow), `operations.py` (periodic operations: evolve, index, consolidate, maintain, compact), `api.py` (HTTP API), `agentic.py` (tool-use loop), `config.py`, `context.py` (system prompt builder), `session.py`, `messages.py` (TypedDict message types), `skills.py`, `memory.py`, `memory_schema.py`, `consolidation.py`, `metering.py`, `metrics.py` (Prometheus), `attachments.py`, `log_utils.py`, `async_utils.py`. Subdirectories: `channels/` (standalone bridges: Telegram, CLI, email), `providers/` (Anthropic, OpenAI, Mistral, smoke-test), `tools/` (14 agent tools), `plugins.d/` (tool + preprocessor plugins), `bin/` (`lucydctl` control client), `providers.d/` (provider configs). See [architecture](docs/architecture.md#module-map) for the full module map.

## Configuration

All configuration lives in `lucyd.toml`. API keys go in `.env`. See [configuration reference](docs/configuration.md#environment-variables) for the full list of settings and environment variables.

## Documentation

- [Configuration Reference](docs/configuration.md) — Every `lucyd.toml` setting explained
- [Operations Guide](docs/operations.md) — Running, controlling, and monitoring the daemon
- [Architecture](docs/architecture.md) — How the code fits together
- [Plugin & Channel Guide](docs/plugins.md) — Building tools, preprocessors, and channel bridges

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

Lucyd uses a standalone Telegram bridge process (`channels/telegram.py`) that connects to the Bot API via httpx long polling and forwards messages to the daemon's HTTP API.

1. Create a bot via [@BotFather](https://t.me/BotFather) on Telegram
2. Copy the bot token to your `.env` file as `LUCYD_TELEGRAM_TOKEN`
3. Find your Telegram user ID (send a message to [@userinfobot](https://t.me/userinfobot))
4. Add a `[telegram]` section to `lucyd.toml` with your user ID in `allow_from` and contacts
5. Start the daemon, then start the bridge: `python3 channels/telegram.py`

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

# Docker smoke test
sh tests/test_docker_smoke.sh
```

### Test Strategy

Component tests, integration tests (full daemon wiring with mocked providers), conversation replay from recorded fixtures, and structural guards that prevent regressions. Optional dependency tests (PIL, pypdf, httpx, aiohttp) skip cleanly via `importorskip` when deps are absent. Coverage tracking via pytest-cov is configured in `pyproject.toml`.

CI runs mypy --strict and the full test suite on every push and PR via GitHub Actions.

## License

MIT — see [LICENSE](LICENSE).
