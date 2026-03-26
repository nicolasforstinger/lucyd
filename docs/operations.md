# Operations Guide

How to run, control, and monitor the Lucyd daemon.

## systemd Service

Lucyd runs as a system-level systemd unit (`lucyd.service`). It restarts on failure with a 5-second delay.

```bash
# Status
sudo systemctl status lucyd

# Start / stop / restart
sudo systemctl start lucyd
sudo systemctl stop lucyd
sudo systemctl restart lucyd

# Enable on boot
sudo systemctl enable lucyd

# View logs (journald)
journalctl -u lucyd --since "10 min ago" --no-pager
journalctl -u lucyd -f    # follow live
```

The unit file lives at `/etc/systemd/system/lucyd.service` (template: `lucyd.service.example` in the repo). After editing, reload and restart:

```bash
sudo cp ~/lucyd/lucyd.service.example /etc/systemd/system/lucyd.service
sudo systemctl daemon-reload
sudo systemctl restart lucyd
```

## lucydctl

CLI control client. Thin HTTP wrapper — all commands go through the daemon's HTTP API.

```bash
# Send a user message (processed as if someone sent it)
~/lucyd/bin/lucydctl --message "Hello there."

# Send a message from a named sender (gets its own session)
~/lucyd/bin/lucydctl --message "Quick question." --from Claudio

# Send a system event (reply suppressed, not delivered to channel)
~/lucyd/bin/lucydctl --system "Health check"

# Fire-and-forget notification (matches HTTP /notify)
~/lucyd/bin/lucydctl --notify "Invoice ready" --source n8n --ref INV-42
~/lucyd/bin/lucydctl --notify "Task done" --data '{"status": "ok"}'

# Force diary write + compaction on primary session
~/lucyd/bin/lucydctl --compact

# Trigger memory evolution (pre-checks for new logs)
~/lucyd/bin/lucydctl --evolve
~/lucyd/bin/lucydctl --evolve --force    # Skip pre-check

# Run workspace indexing (incremental)
~/lucyd/bin/lucydctl --index
~/lucyd/bin/lucydctl --index --full      # Force full re-index
~/lucyd/bin/lucydctl --index-status      # Show index status

# Memory consolidation
~/lucyd/bin/lucydctl --consolidate       # Extract facts from workspace files
~/lucyd/bin/lucydctl --maintain          # Stale facts, expired commitments, metering retention

# Query token costs
~/lucyd/bin/lucydctl --cost today
~/lucyd/bin/lucydctl --cost week
~/lucyd/bin/lucydctl --cost all

# List active sessions (context %, cost, log size, date range)
~/lucyd/bin/lucydctl --sessions

# Reset session (archives old, next message starts fresh)
~/lucyd/bin/lucydctl --reset              # Reset ALL sessions
~/lucyd/bin/lucydctl --reset system
~/lucyd/bin/lucydctl --reset user
~/lucyd/bin/lucydctl --reset <session-uuid>
```

**Flags:**

| Flag | Purpose |
|---|---|
| `-m`, `--message <text>` | User message to inject |
| `-s`, `--system <text>` | System event to inject |
| `-n`, `--notify <text>` | Fire-and-forget notification (matches HTTP `/notify`). Routes to primary sender session when configured. |
| `--source <label>` | Notification source label (with `--notify`). Bracket-prefixed in LLM text. |
| `--ref <ref>` | Notification reference (with `--notify`). Bracket-prefixed in LLM text. |
| `--data <json>` | Notification metadata as JSON (with `--notify`). Passed as `notify_meta`, not in LLM text. |
| `--compact` | Force diary write + compaction on primary session. Agent writes daily log, then compaction fires regardless of token threshold. |
| `--evolve` | Trigger memory evolution. Pre-checks for new daily logs; skips if none found. |
| `--force` | Skip pre-check (with `--evolve`). Triggers evolution regardless of new logs. |
| `--index` | Run workspace indexing. Incremental by default; use `--full` for full re-index. |
| `--index-status` | Show workspace index status (file count, chunk count, pending). |
| `--consolidate` | Extract structured facts from workspace markdown files via LLM. |
| `--maintain` | Run memory maintenance: stale facts, expired commitments, conflict detection, metering retention. |
| `--from <name>` | Sender name for `--message` / `--notify` (default: `cli`). Each unique sender gets its own session. |
| `--cost [period]` | Query cost: `today` (default) / `week` / `all` |
| `--sessions` | List active sessions with context %, cost, log size, date range. Filesystem-only — no daemon needed. |
| `--monitor` | Show live API call monitor state. Filesystem-only — reads `~/.lucyd/monitor.json`. Use with `watch -n 1` for live updates. |
| `--reset [target]` | Reset sessions. No argument resets all. Target: sender name (`system` / `user`) or session UUID. Archives to `.archive/`, next message starts fresh. |
| `--history [contact\|id]` | Show session history. Resolves contact name to session ID. Use with `--full` for tool calls. |
| `--full` | Full mode: all events with `--history`, force full re-index with `--index`. |
| `-a`, `--attach <file>` | Attach file(s) to the message. Can be repeated for multiple files. |
| `--status` | Daemon status: pid, uptime, model, sessions, cost. Reads from state files — no daemon response needed. |
| `--log [N]` | Last N lines of daemon log (default: 20). Reads from `lucyd.log` in state directory. |
| `--wait <secs>` | Wait N seconds after sending before exiting (default: 0 = no wait) |
| `--state-dir <path>` | Override state directory (default: `~/.lucyd`) |

If the daemon is not running, `lucydctl` exits with a connection error.

### Live Monitor

When the daemon processes a message, it writes real-time state to `~/.lucyd/monitor.json`. The `--monitor` flag reads this file and formats it for terminal display.

```bash
# One-shot status check
~/lucyd/bin/lucydctl --monitor

# Live monitoring (updates every second)
watch -n 1 ~/lucyd/bin/lucydctl --monitor
```

**States:** `idle` (between messages), `thinking` (waiting for LLM API response), `tools` (executing tool calls).

**Output shows:**
- Current state with turn number and elapsed time
- Contact name and model being used
- Per-turn history: duration, output tokens, stop reason, tool names
- Stale detection: warns if the last update is >60s old while not idle (daemon may be stuck or dead)

**Example output:**
```
Lucy — thinking (turn 3, 4.2s elapsed)
────────────────────────────────────────
Contact:  Nicolas
Model:    claude-sonnet-4-6

  T1   3.2s   156 tok  tool_use → memory_search
  T2   4.5s   342 tok  tool_use → read
  T3   ...thinking (4.2s)
```

**State file (`~/.lucyd/monitor.json`):**

The daemon writes this file atomically (write to `.tmp`, rename) on every agentic loop event via `on_response` and `on_tool_results` callbacks. Sub-agents do not write to the monitor. Messages process sequentially from the queue, so there are no race conditions on the file.

The file contains: `state`, `contact`, `session_id`, `model`, `turn`, `message_started_at`, `turn_started_at`, `tools_in_flight`, `turns` (array of per-turn stats), and `updated_at`.

### System Message Behavior

System events (`--system`) differ from user messages (`--message`) in two ways:

1. **Reply suppression**: The agentic loop runs (tools execute, cost recorded), but reply text is not delivered to any channel
2. **Session framing**: The system prompt includes a "Session type: automated infrastructure" annotation so the LLM knows this is automation, not conversation

The agent processes system events (spawns sub-agents, writes memory, sends messages via tools), but the textual response stays internal. All messages use the primary model.

## HTTP API

The daemon always starts an HTTP API server. Channels (Telegram, CLI, email) are standalone bridge processes that connect via the HTTP API.

### Configuration

```toml
[http]
enabled = true
host = "127.0.0.1"        # localhost only — safe default
port = 8100
```

Token is loaded from the `LUCYD_HTTP_TOKEN` environment variable (set in `.env` in the same directory as `lucyd.toml`).

### Shared Behaviors

**Agent identity:** When `agent_name` is configured, all endpoint responses include an `"agent"` field in the JSON body and an `X-Lucyd-Agent` response header. Infrastructure error responses (auth, rate limit, body size) do not include these.

**Authentication:** Bearer token from `LUCYD_HTTP_TOKEN` env var. All endpoints except `/api/v1/status` require auth. Deny-by-default — no token configured means 503 on all protected endpoints.

**Body size:** Request bodies capped at `[http] max_body_bytes` (default: 10 MiB). Oversized requests get HTTP 413 from aiohttp.

**Rate limit groups:**

| Group | Limit | Endpoints |
|---|---|---|
| Read-only | `status_rate_limit` (default 60) per `rate_window` (default 60s) | `/status`, `/sessions`, `/cost`, `/monitor`, `/sessions/{id}/history` |
| Standard | `rate_limit` (default 30) per `rate_window` (default 60s) | `/chat`, `/chat/stream`, `/notify`, `/sessions/reset`, `/evolve`, `/compact`, `/contacts/{contact}` |

Rate limit key is client IP.

**Infrastructure error responses** (no agent identity injection):

| Status | Body | Condition |
|---|---|---|
| 401 | `{"error": "unauthorized"}` | Missing or invalid Bearer token |
| 429 | `{"error": "rate limit exceeded"}` | Rate limit exceeded for client IP |
| 503 | `{"error": "No auth token configured"}` | No `LUCYD_HTTP_TOKEN` in environment |

### Endpoints

```bash
# Status (health check)
curl -H "Authorization: Bearer $TOKEN" http://localhost:8100/api/v1/status

# Chat (synchronous — waits for agent response)
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "summarize today", "sender": "n8n-daily"}' \
  http://localhost:8100/api/v1/chat

# Notify (fire-and-forget — returns 202 immediately)
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "New email from someone@example.com", "source": "email-monitor", "ref": "msg-123"}' \
  http://localhost:8100/api/v1/notify

# Sessions (list active sessions)
curl -H "Authorization: Bearer $TOKEN" http://localhost:8100/api/v1/sessions

# Cost (query token costs by period)
curl -H "Authorization: Bearer $TOKEN" "http://localhost:8100/api/v1/cost?period=today"

# Monitor (live agentic loop state)
curl -H "Authorization: Bearer $TOKEN" http://localhost:8100/api/v1/monitor

# Reset sessions
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"target": "all"}' \
  http://localhost:8100/api/v1/sessions/reset

# Session history
curl -H "Authorization: Bearer $TOKEN" "http://localhost:8100/api/v1/sessions/abc-123/history?full=true"

# Streaming chat (SSE)
curl -N -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello!"}' \
  http://localhost:8100/api/v1/chat/stream
```

---

#### `POST /api/v1/chat`

Synchronous — sends a message and waits for the agent to respond.

**Request fields:**

| Field | Required | Default | Description |
|---|---|---|---|
| `message` | yes | — | Message text |
| `sender` | no | `"default"` | Session key (each unique sender gets its own session, prefixed with `http-`) |
| `channel_id` | no | `"http"` | Channel identifier. Used in session keying (`channel_id:sender`) and metrics labels |
| `task_type` | no | `"conversational"` | `"conversational"` (session stays open), `"task"` (auto-close after response), `"system"` (auto-close, internal automation) |
| `reply_to` | no | — | Response routing: omit for normal reply, `"silent"` to suppress delivery (log only), or a sender name to redirect the reply into that sender's session as a system message |
| `context` | no | — | Freeform label prepended as `[context]` (for debugging) |
| `attachments` | no | — | List of base64-encoded file attachments |

**Response (200 — success):**

```json
{
  "reply": "agent response text",
  "session_id": "uuid-string",
  "tokens": {"input": 1500, "output": 200}
}
```

When the reply matches a configured `silent_token`, the response includes `"silent": true`. Non-silent replies omit the field entirely.

**Response (200 — agentic loop error):**

```json
{
  "error": "exception message",
  "session_id": "uuid-string"
}
```

Provider failures during the agentic loop return HTTP 200 with an `error` field instead of `reply`. No `tokens` field.

**Error responses:**

| Status | Body | Condition |
|---|---|---|
| 400 | `{"error": "invalid JSON body"}` | Malformed JSON |
| 400 | `{"error": "\"message\" field is required"}` | Missing `message` field |
| 408 | `{"error": "processing timeout"}` | Exceeds `agent_timeout_seconds` |

**Rate limit group:** Standard

---

#### `POST /api/v1/notify`

Fire-and-forget — queues the message and returns immediately. The agent processes it asynchronously.

**Request fields:**

| Field | Required | Default | Description |
|---|---|---|---|
| `message` | yes | — | Natural language message for the LLM |
| `source` | no | — | Source label, prefixed as `[source: ...]` in LLM text |
| `ref` | no | — | Reference ID, prefixed as `[ref: ...]` in LLM text |
| `data` | no | — | Arbitrary JSON payload (passed through as `notify_meta` to webhook, not in LLM text) |
| `sender` | no | `"default"` | Session key (prefixed with `http-`) |

**Response (202 — accepted):**

```json
{
  "accepted": true,
  "queued_at": "2026-02-26T14:30:00Z"
}
```

`queued_at` is UTC, second precision, Z suffix.

**Error responses:**

| Status | Body | Condition |
|---|---|---|
| 400 | `{"error": "invalid JSON body"}` | Malformed JSON |
| 400 | `{"error": "\"message\" field is required"}` | Missing `message` field |

**Rate limit group:** Standard

---

#### `GET /api/v1/status`

Health check. Auth-exempt — always accessible without a Bearer token.

**Response (200):**

```json
{
  "status": "ok",
  "pid": 12345,
  "uptime_seconds": 3600,
  "model": "claude-sonnet-4-6",
  "active_sessions": 3,
  "today_cost": 4.2150,
  "error_counts": {},
  "queue_depth": 0
}
```

| Field | Type | Description |
|---|---|---|
| `status` | string | Always `"ok"` |
| `pid` | int | Daemon process ID |
| `uptime_seconds` | int | Seconds since daemon start |
| `model` | string | Primary model name |
| `active_sessions` | int | Number of tracked sessions |
| `today_cost` | float | Today's cost in EUR (4 decimal places), `0.0` if metering DB unavailable |
| `error_counts` | object | Error count by type |
| `queue_depth` | int | Messages waiting in queue |

**Rate limit group:** Read-only

---

#### `GET /api/v1/sessions`

List active sessions with context usage, cost, and log metadata. Same data as `lucydctl --sessions`.

**Response (200):**

```json
{
  "sessions": [
    {
      "session_id": "uuid-string",
      "contact": "Nicolas",
      "created_at": "2026-02-26T08:00:00",
      "model": "claude-sonnet-4-6",
      "message_count": 42,
      "compaction_count": 1,
      "context_tokens": 15000,
      "context_pct": 7,
      "cost": 0.045000,
      "log_files": 3,
      "log_bytes": 128000
    }
  ]
}
```

| Field | Type | Description |
|---|---|---|
| `session_id` | string | Session UUID |
| `contact` | string | Sender name |
| `created_at` | string | Session creation timestamp |
| `model` | string | Current model (only present for live/in-memory sessions) |
| `message_count` | int | Total messages in session |
| `compaction_count` | int | Number of compactions performed |
| `context_tokens` | int | Current context token usage |
| `context_pct` | int | Context usage as percentage of model max (0–100) |
| `cost` | float | Session cost in EUR (6 decimal places), `0.0` if metering DB unavailable |
| `log_files` | int | Number of JSONL log files |
| `log_bytes` | int | Total log file size in bytes |

**Rate limit group:** Read-only

---

#### `GET /api/v1/cost`

Token cost breakdown by period. Same data as `lucydctl --cost`.

**Query parameter:** `period` — `today` (default), `week`, or `all`.

**Response (200):**

```json
{
  "period": "today",
  "total_cost": 4.2150,
  "models": [
    {
      "model": "claude-sonnet-4-6",
      "input_tokens": 50000,
      "output_tokens": 12000,
      "cache_read_tokens": 30000,
      "cache_write_tokens": 5000,
      "cost": 4.215000
    }
  ]
}
```

| Field | Type | Description |
|---|---|---|
| `period` | string | Requested period |
| `total_cost` | float | Total cost in EUR (4 decimal places) |
| `models` | object[] | Per-model breakdown |
| `models[].model` | string | Model name |
| `models[].input_tokens` | int | Input tokens consumed |
| `models[].output_tokens` | int | Output tokens consumed |
| `models[].cache_read_tokens` | int | Cache read tokens |
| `models[].cache_write_tokens` | int | Cache write tokens |
| `models[].cost` | float | Per-model cost in EUR (6 decimal places) |

Degrades to `{"period": "...", "total_cost": 0.0, "models": []}` if cost DB is unavailable.

**Error responses:**

| Status | Body | Condition |
|---|---|---|
| 400 | `{"error": "period must be 'today', 'week', or 'all'"}` | Invalid period value |

**Rate limit group:** Read-only

---

#### `GET /api/v1/monitor`

Live agentic loop state. Same data as `lucydctl --monitor`. Returns the contents of `monitor.json` as written by the daemon during message processing.

**Response (200 — active processing):**

```json
{
  "state": "thinking",
  "contact": "Nicolas",
  "session_id": "uuid-string",
  "model": "claude-sonnet-4-6",
  "turn": 3,
  "message_started_at": "2026-02-26T14:30:00Z",
  "turn_started_at": "2026-02-26T14:30:12Z",
  "tools_in_flight": ["memory_search"],
  "turns": [
    {
      "duration_ms": 3200,
      "input_tokens": 15000,
      "output_tokens": 156,
      "cache_read_tokens": 10000,
      "cache_write_tokens": 2000,
      "stop_reason": "tool_use",
      "tools": ["memory_search"]
    }
  ],
  "updated_at": "2026-02-26T14:30:15Z"
}
```

**Response (200 — file missing or unparseable):**

```json
{"state": "unknown"}
```

Returns `monitor.json` verbatim. No guaranteed schema beyond the `{"state": "unknown"}` fallback.

**Rate limit group:** Read-only

---

#### `POST /api/v1/sessions/reset`

Reset sessions by target. Archives session state and logs — never deletes. Same logic as `lucydctl --reset`.

**Request fields:**

| Field | Required | Default | Description |
|---|---|---|---|
| `target` | no | `"all"` | Reset target: `"all"`, `"user"`, a contact name, or a session UUID |

**Response (200 — success):**

```json
{"reset": true, "target": "all", "count": 3}
```

```json
{"reset": true, "target": "Nicolas", "type": "contact"}
```

```json
{"reset": true, "target": "abc-uuid", "type": "session_id"}
```

| Field | Type | Description |
|---|---|---|
| `reset` | bool | Whether the reset succeeded |
| `target` | string | The target that was reset |
| `count` | int | Number of sessions reset (only for `"all"` target) |
| `type` | string | `"session_id"` or `"contact"` (only for specific targets) |

**Response (200 — no match):**

```json
{"reset": false, "reason": "no session found for: <contact>"}
```

```json
{"reset": false, "reason": "no session found for ID: <uuid>"}
```

```json
{"reset": false, "reason": "no user session found"}
```

**Error responses:**

| Status | Body | Condition |
|---|---|---|
| 400 | `{"error": "invalid JSON body"}` | Malformed JSON |
| 400 | `{"error": "\"target\" must be a non-empty string"}` | Empty or non-string target |

**Rate limit group:** Standard

---

#### `GET /api/v1/sessions/{session_id}/history`

Chronological event history for a session. Same data as `lucydctl --history`.

**Query parameter:** `full` — `true`, `1`, or `yes` to include all JSONL events. Default: messages only.

**Response (200 — messages only, `full=false`):**

```json
{
  "session_id": "uuid-string",
  "events": [
    {
      "type": "message",
      "role": "user",
      "content": "hello",
      "from": "Nicolas",
      "timestamp": 1740580200.0
    },
    {
      "type": "message",
      "role": "assistant",
      "text": "hey there",
      "timestamp": 1740580205.0
    }
  ]
}
```

**Response (200 — full events, `full=true`):**

Returns all raw JSONL event objects as-is (tool calls, tool results, system events, compaction events, etc.). No field transformation.

**Response (200 — session not found):**

```json
{"session_id": "uuid-string", "events": []}
```

Returns empty events when no log files exist for the session ID. Not an error.

Events are deduplicated by `timestamp` across active and archive log files, sorted chronologically ascending.

**Rate limit group:** Read-only

---

#### `POST /api/v1/evolve`

Queue self-driven memory evolution. The agent loads the `evolution` skill and rewrites MEMORY.md/USER.md through the full agentic loop with complete persona context. Fire-and-forget — returns 202 immediately.

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" http://localhost:8100/api/v1/evolve
```

**Response (202 — queued):**

```json
{
  "status": "queued",
  "session": "evolution"
}
```

**Error responses:**

| Status | Body | Condition |
|---|---|---|
| 503 | `{"error": "evolution not available"}` | Evolution callback not configured |
| 500 | `{"error": "internal error"}` | Exception queuing the message |

**Rate limit group:** Standard

#### `POST /api/v1/compact`

Force diary write + compaction on the primary session. The agent writes a daily memory log via the `write` tool, then compaction fires regardless of token threshold. The primary session is identified as the longest active non-system session.

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" http://localhost:8100/api/v1/compact
```

**Response (200 — completed):**

```json
{
  "status": "completed",
  "session": "session-id-here"
}
```

**Response (202 — in progress):**

```json
{
  "status": "processing",
  "session": "session-id-here"
}
```

**Error responses:**

| Status | Body | Condition |
|---|---|---|
| 200 | `{"status": "skipped", "reason": "no active session"}` | No non-system sessions active |
| 503 | `{"error": "compact not available"}` | Compact callback not configured |
| 500 | `{"error": "internal error"}` | Exception during processing |

**Rate limit group:** Standard

---

#### `POST /api/v1/index`

Run workspace indexing. Scans workspace files, chunks, embeds, writes to FTS5 + vector DB.

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" http://localhost:8100/api/v1/index
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"full": true}' \
  http://localhost:8100/api/v1/index
```

**Request fields:**

| Field | Required | Default | Description |
|---|---|---|---|
| `full` | no | `false` | Force full re-index (ignore content hashes) |

**Response (200):**

```json
{
  "indexed": [["file.md", 12]],
  "skipped": 5,
  "removed": [],
  "errors": [],
  "total_files": 6,
  "total_chunks": 42
}
```

**Rate limit group:** Standard

---

#### `GET /api/v1/index/status`

Workspace index status.

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8100/api/v1/index/status
```

**Response (200):**

```json
{
  "indexed_files": 10,
  "total_chunks": 150,
  "pending_files": [],
  "stale_files": []
}
```

**Rate limit group:** Read-only

---

#### `POST /api/v1/consolidate`

Extract structured facts from workspace markdown files via LLM.

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" http://localhost:8100/api/v1/consolidate
```

**Response (200):**

```json
{
  "status": "completed",
  "facts": 12,
  "files_scanned": 8,
  "files_with_facts": 3
}
```

**Rate limit group:** Standard

---

#### `POST /api/v1/maintain`

Run memory maintenance: stale facts, expired commitments, conflict detection, metering retention.

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" http://localhost:8100/api/v1/maintain
```

**Response (200):**

```json
{
  "facts": 150,
  "episodes": 30,
  "open_commitments": 5,
  "stale": 2,
  "expired": 1,
  "conflicts": 0,
  "metering_deleted": 0
}
```

**Rate limit group:** Standard

---

#### `GET /metrics`

Prometheus metrics in text exposition format. Auth-exempt. Returns `text/plain; version=0.0.4; charset=utf-8`.

Only available when `prometheus_client` is installed. Returns 404 otherwise.

---

### Behavior

- `/chat` waits for the agentic loop to complete (up to `agent_timeout_seconds`). Returns 408 on timeout.
- `/notify` queues the event and returns immediately. The agent processes it asynchronously.
- HTTP messages bypass the debounce window — each `/chat` request is processed immediately.
- Replies return in the HTTP response. There is no outbound push — channels poll the API for replies.

## Unix Signals

The daemon handles three Unix signals for runtime control:

| Signal | Effect |
|---|---|
| `SIGUSR1` | Reload workspace files and rescan skills. Context is rebuilt on the next message. |
| `SIGTERM` | Graceful shutdown. Finishes the current message, cleans up PID file, exits. |

```bash
# Reload workspace after editing personality/skill files
kill -USR1 $(cat ~/.lucyd/lucyd.pid)

# Graceful stop (equivalent to systemctl stop)
kill -TERM $(cat ~/.lucyd/lucyd.pid)
```

`SIGINT` (Ctrl+C) is handled identically to `SIGTERM`.

## CLI Mode

For interactive testing, run the CLI bridge against a running daemon:

```bash
cd ~/lucyd
source .venv/bin/activate
python3 channels/cli.py
```

The CLI bridge reads from stdin, POSTs to the daemon's `/api/v1/chat/stream` endpoint, and streams the response via SSE. Useful for debugging prompts, tool behavior, or context building.

When stdin reaches EOF (piped input or Ctrl+D), the CLI exits cleanly.

## Log Locations

Lucyd logs to two destinations simultaneously:

| Destination | Level | Path |
|---|---|---|
| File | DEBUG | `~/.lucyd/lucyd.log` |
| stderr (journald) | INFO | `journalctl -u lucyd` |

```bash
# Recent daemon log (file)
tail -50 ~/.lucyd/lucyd.log

# Recent daemon log (journald)
journalctl -u lucyd --since "10 min ago" --no-pager

# Follow live
journalctl -u lucyd -f
tail -f ~/.lucyd/lucyd.log
```

Third-party loggers (`httpx`, `httpcore`, `anthropic`, `openai`) are suppressed to WARNING level.

## Log Privacy

Lucyd logs may contain sender names and message metadata in cleartext.
Ensure log files are readable only by the lucyd service user (chmod 600).
Logs should not be shipped to external aggregation services without scrubbing.

## Health Checks

### Daemon alive

```bash
# PID file
cat ~/.lucyd/lucyd.pid
kill -0 $(cat ~/.lucyd/lucyd.pid) && echo "running" || echo "dead"

# systemd
systemctl is-active lucyd
```

### Telegram bridge connected

```bash
# Check that the Telegram bridge process is running
ps aux | grep telegram.py

# Check bridge log output for connection status
journalctl -u lucyd-telegram --since "5 min ago" --no-pager
```

### Status dump

```bash
kill -USR2 $(cat ~/.lucyd/lucyd.pid)
cat ~/.lucyd/status.json
```

Returns JSON with:
- `pid` -- daemon process ID
- `uptime_s` -- seconds since start
- `tools` -- list of registered tool names
- `models` -- loaded model names

## Cron Jobs

Recommended cron jobs for long-running deployments:

| Schedule | Job | Purpose |
|---|---|---|
| `5 * * * *` | Workspace git auto-commit | `git add -A && commit && push` in the workspace directory |
| `10 * * * *` | Memory indexer | `lucydctl --index` — scans workspace, chunks, embeds, writes to FTS5 + vector DB |
| `15 * * * *` | Memory consolidation | `lucydctl --consolidate` — extracts structured facts, episodes, commitments from workspace files |
| `5 3 * * *` | Trash cleanup | Delete files in `.trash/` older than 30 days |
| `5 4 * * *` | Memory maintenance | `lucydctl --maintain` — stale facts, expired commitments, metering retention |
| `20 4 * * *` | Memory evolution | `lucydctl --evolve` — self-driven rewrite of workspace understanding files (MEMORY.md, USER.md) |
| `5 4 * * 0` | DB integrity check | `PRAGMA integrity_check` on memory SQLite DB |

```bash
# View crontab
crontab -l

# Edit crontab
crontab -e
```

## lucydctl --evolve

Trigger self-driven memory evolution via `POST /api/v1/evolve`. The daemon pre-checks for new daily logs. The agent loads the `evolution` skill from workspace, reads daily logs and current files using its tools, and rewrites MEMORY.md/USER.md through the full agentic loop with complete persona context.

```bash
# Trigger self-driven evolution (default for Lucy's cron)
~/lucyd/bin/lucydctl --evolve

# Force evolution (skip pre-check)
~/lucyd/bin/lucydctl --evolve --force
```

**How it works:**

1. Opens the memory DB at `state-dir/memory/main.sqlite`
2. Calls `check_new_logs_exist()` — compares daily log files against the `evolution_state` table
3. If no new logs since last evolution: exits silently (no daemon contact). Use `--force` to skip this check.
4. POSTs to the daemon's `/api/v1/evolve` endpoint
5. The daemon processes the message: the agent loads the `evolution` skill, reads its daily logs and current files with tools, and rewrites MEMORY.md/USER.md

Evolution is always self-driven — the agent loads the evolution skill, uses tools to read/write files through the full agentic loop with persona context. This preserves voice and emotional content because the agent rewrites its own files, not a standalone LLM call.

**Requires:** Running daemon with HTTP API. If the daemon is not running, exits with a connection error.

## Troubleshooting

### Daemon won't start: "Another instance is running"

The PID file (`~/.lucyd/lucyd.pid`) exists and the PID is live. Either stop the running instance or, if the process is genuinely dead, remove the stale PID file:

```bash
cat ~/.lucyd/lucyd.pid
ps -p $(cat ~/.lucyd/lucyd.pid)    # check if alive
rm ~/.lucyd/lucyd.pid              # only if dead
```

### Daemon starts but no messages are delivered

1. Check the Telegram bridge process is running (`python3 channels/telegram.py`)
2. Check `allow_from` in `telegram.toml` -- sender must be in the allowlist (numeric Telegram user IDs)
4. Check daemon log for errors: `tail -50 ~/.lucyd/lucyd.log`

### Messages processed but replies are empty

1. Check for silent tokens -- if the reply starts or ends with `NO_REPLY`, it is suppressed.
2. Check the agentic loop: `grep "Tool call" ~/.lucyd/lucyd.log | tail -20`
3. Check for API errors: `grep "ERROR" ~/.lucyd/lucyd.log | tail -10`

### API timeout errors

The default timeout is 600 seconds (`behavior.agent_timeout_seconds`). If consistently hitting it:

1. Check provider status
2. Check network connectivity
3. Review whether the agent is stuck in a tool loop: `grep "Tool call" ~/.lucyd/lucyd.log | tail -50`

### Connection errors from lucydctl

"Cannot connect to daemon" means the HTTP API is not reachable. Start the daemon:

```bash
sudo systemctl start lucyd
```

### High token costs

```bash
# Check today's cost by model
~/lucyd/bin/lucydctl --cost today

# Check weekly
~/lucyd/bin/lucydctl --cost week
```

Review recent sessions for excessive tool loops:

```bash
ls -lt ~/.lucyd/sessions/ | head
```

The `max_turns_per_message` setting (default: 50) caps tool-use iterations per message.
