# Operations Guide

How to run, control, and monitor the Lucyd daemon.

## systemd Service

Lucyd runs as a system-level systemd unit (`lucyd.service`). It restarts on failure with a 5-second delay.

```bash
sudo systemctl status lucyd
sudo systemctl start lucyd
sudo systemctl stop lucyd
sudo systemctl restart lucyd
journalctl -u lucyd -f
```

The unit file lives at `/etc/systemd/system/lucyd.service` (template: `lucyd.service.example` in the repo).

## lucydctl

CLI control client (`bin/lucydctl`). Thin HTTP wrapper — all commands go through the daemon's HTTP API.

### Send Commands

| Flag | Endpoint | Behavior |
|---|---|---|
| `-m, --message TEXT` | `POST /chat` | Synchronous — waits for response (300s timeout) |
| `-s, --system TEXT` | `POST /system` | Fire-and-forget system event (202) |
| `-n, --notify TEXT` | `POST /notify` | Fire-and-forget notification (202) |
| `--evolve` | `POST /evolve` | Trigger memory evolution |
| `--compact` | `POST /compact` | Force diary write + compaction |
| `--index` | `POST /index` | Run workspace indexing (600s timeout) |
| `--consolidate` | `POST /consolidate` | Extract facts from workspace (600s timeout) |
| `--maintain` | `POST /maintain` | Run memory maintenance |
| `--reset [TARGET]` | `POST /sessions/reset` | Reset sessions (default: "all") |

### Query Commands

| Flag | Endpoint |
|---|---|
| `--status` | `GET /status` |
| `--cost [PERIOD]` | `GET /cost?period=PERIOD` (YYYY-MM, default: current month) |
| `--sessions` | `GET /sessions` |
| `--monitor` | `GET /monitor` |
| `--history [TARGET]` | `GET /sessions/{id}/history` |
| `--index-status` | `GET /index/status` |

### Options

| Flag | Use with | Purpose |
|---|---|---|
| `--from NAME` | `--message` | Sender name (default: "cli") |
| `--source SOURCE` | `--notify` | Source label, prefixed in LLM text as `[source: ...]` |
| `--ref REF` | `--notify` | Reference ID, prefixed as `[ref: ...]` |
| `--data JSON` | `--notify` | Arbitrary JSON metadata |
| `--full` | `--history`, `--index` | All events (history) or force full re-index |
| `-a, --attach FILE` | `--message`, `--system`, `--notify` | Attach file(s), repeatable |

### Examples

```bash
# Send a message (synchronous — waits for agent response)
~/lucyd/bin/lucydctl --message "Hello there."
~/lucyd/bin/lucydctl --message "Quick question." --from Claudio

# System event (fire-and-forget)
~/lucyd/bin/lucydctl --system "Health check"

# Notification with metadata
~/lucyd/bin/lucydctl --notify "Invoice ready" --source n8n --ref INV-42
~/lucyd/bin/lucydctl --notify "Task done" --data '{"status": "ok"}'

# Maintenance operations
~/lucyd/bin/lucydctl --compact
~/lucyd/bin/lucydctl --evolve
~/lucyd/bin/lucydctl --index
~/lucyd/bin/lucydctl --consolidate
~/lucyd/bin/lucydctl --maintain

# Queries
~/lucyd/bin/lucydctl --cost 2026-03
~/lucyd/bin/lucydctl --sessions
~/lucyd/bin/lucydctl --monitor
~/lucyd/bin/lucydctl --history Nicolas --full
~/lucyd/bin/lucydctl --reset user
```

## HTTP API

18 endpoints registered in `api.py` (lines 148-167). Auth via Bearer token from `LUCYD_HTTP_TOKEN` env var.

### Authentication

All endpoints require Bearer token except `/api/v1/status` and `/metrics`.

```
Authorization: Bearer $LUCYD_HTTP_TOKEN
```

No token configured = 503 on all protected endpoints. Invalid token = 401.

### Rate Limiting

| Group | Default | Endpoints |
|---|---|---|
| Read-only | 60 per 60s | `/status`, `/metrics`, `/sessions`, `/cost`, `/monitor`, `/index/status`, `/sessions/{id}/history` |
| Standard | 30 per 60s | All POST endpoints |

Rate limit key is client IP. Exceeded = 429.

### Body Size

Request bodies capped at `[http] max_body_bytes` (default: 10 MiB). Oversized = 413.

---

### POST /api/v1/chat

Synchronous — sends a message and waits for the agent to respond.

**Request:**

| Field | Required | Default | Description |
|---|---|---|---|
| `message` | yes | — | Message text |
| `sender` | no | `"default"` | Sender name (prefixed with `http-` internally) |
| `channel_id` | no | `"http"` | Channel identifier for session keying and metrics |
| `task_type` | no | `"conversational"` | `"conversational"`, `"task"` (auto-close), `"system"` (auto-close) |
| `reply_to` | no | — | `"silent"` (log only) or sender name (redirect reply) |
| `context` | no | — | Freeform label prepended as `[context]` |
| `attachments` | no | — | Base64-encoded file attachments |

**Response (200):**

```json
{"reply": "agent response", "session_id": "uuid", "tokens": {"input": 1500, "output": 200}}
```

When reply matches a `silent_token`: `{"reply": "...", "silent": true, ...}`.
When `reply_to` is a sender name: includes `"redirected_to": "<sender>"`.
On agentic loop error: `{"error": "exception message", "session_id": "uuid"}`.

**Errors:** 400 (invalid JSON, missing message), 408 (timeout).

---

### POST /api/v1/chat/stream

Same as `/chat` but streams the response via Server-Sent Events.

**Request:** Same fields as `/chat`.

**Response (200 — SSE):**

```
data: {"text": "partial"}
data: {"thinking": "..."}
data: {"status": "tool_use"}
data: {"done": true, "stop_reason": "end_turn", "usage": {"input_tokens": 1500, "output_tokens": 200}}
```

On error: `event: error\ndata: {"error": "..."}`.

---

### POST /api/v1/message

Fire-and-forget user message.

**Request:** Same fields as `/chat`. **Response (202):** `{"accepted": true, "queued_at": "ISO8601"}`.

---

### POST /api/v1/notify

Fire-and-forget notification. Routes to operator's session via `notify_target`. Default `task_type`: `"system"`.

**Request:**

| Field | Required | Default | Description |
|---|---|---|---|
| `message` | yes | — | Notification text |
| `source` | no | — | Source label, prefixed as `[source: ...]` |
| `ref` | no | — | Reference ID, prefixed as `[ref: ...]` |
| `data` | no | — | Arbitrary JSON metadata (passed through, not in LLM text) |
| `sender` | no | `"default"` | Sender name |

**Response (202):** `{"accepted": true, "queued_at": "ISO8601"}`.

---

### GET /api/v1/status

Health check. Auth-exempt.

**Response (200):**

```json
{
  "status": "ok",
  "pid": 12345,
  "uptime_seconds": 3600,
  "model": "claude-sonnet-4-6",
  "active_sessions": 3,
  "today_cost": 4.2150,
  "queue_depth": 0,
  "error_counts": {}
}
```

When `agent_name` is configured, includes `"agent": "<name>"` and `X-Lucyd-Agent` response header.

Side effect: updates Prometheus gauges (`uptime_seconds`, `active_sessions`, `queue_depth`).

---

### GET /metrics

Prometheus metrics in text exposition format. Auth-exempt. Returns `text/plain`. Returns a comment line if `prometheus_client` is not installed.

See [architecture.md](architecture.md#metrics) for the full 20-family metric inventory.

---

### GET /api/v1/sessions

List active sessions. **Response (200):** `{"sessions": [...]}`.

---

### GET /api/v1/cost

Cost records for a billing period.

**Query param:** `period` — `YYYY-MM` format (default: current month).

**Response (200):** Cost records from metering DB. **Error:** 400 if metering unavailable.

---

### GET /api/v1/monitor

Live agentic loop state. Returns `monitor.json` contents or `{"state": "unknown"}`.

---

### POST /api/v1/sessions/reset

Reset sessions by target.

**Request:** `{"target": "all"}` or `{"target": "contact_name"}` or `{"target": "session-uuid"}`.

**Response (200):** `{"reset": true, ...}` or `{"reset": false, "reason": "..."}`. **Errors:** 400 (invalid target), 408 (timeout).

---

### GET /api/v1/sessions/{session_id}/history

Chronological event history. Add `?full=true` for all JSONL events (tool calls, system events).

**Response (200):** `{"session_id": "...", "events": [...]}`. Empty events array if session not found.

---

### POST /api/v1/evolve

Trigger memory evolution. Pre-checks for new daily logs.

**Request:** `{"force": true}` to skip pre-check. **Response:** 202 (queued), 200 (skipped), 503 (not configured), 500 (error).

---

### POST /api/v1/compact

Force diary write + compaction on primary session. **Response:** 200 (completed), 202 (processing), 408 (timeout).

---

### POST /api/v1/index

Run workspace indexing. **Request:** `{"full": true}` for full re-index. **Response:** 200 (results), 503 (not configured), 500 (error).

---

### GET /api/v1/index/status

Workspace index status. **Response:** 200 (status), 503 (not configured).

---

### POST /api/v1/consolidate

Extract structured facts from workspace files via LLM. **Response:** 200 (results), 503 (not configured), 500 (error).

---

### POST /api/v1/maintain

Run memory maintenance. **Response:** 200 (results), 503 (not configured), 500 (error).

---

## CLI Mode

Interactive chat against a running daemon:

```bash
lucydctl chat
```

Reads from stdin, POSTs to `/api/v1/chat/stream`, streams the response via SSE. Set `LUCYD_URL` to override the daemon address (default: `http://127.0.0.1:8100`). Use `--from NAME` to override the sender.

## Unix Signals

| Signal | Effect | Source |
|---|---|---|
| `SIGUSR1` | Reload workspace files (skill_loader.scan()) | lucyd.py:1816 |
| `SIGTERM` | Graceful shutdown (running = False) | lucyd.py:1820 |
| `SIGINT` | Same as SIGTERM | lucyd.py:1825 |

```bash
kill -USR1 $(cat ~/.lucyd/lucyd.pid)   # reload
kill -TERM $(cat ~/.lucyd/lucyd.pid)   # stop
```

## Log Locations

| Destination | Level | Path |
|---|---|---|
| File | DEBUG | `$DATA_DIR/logs/lucyd.log` |
| stderr (journald) | INFO | `journalctl -u lucyd` |

Third-party loggers (`httpx`, `httpcore`, `anthropic`, `openai`) suppressed to WARNING.

## Health Checks

```bash
# PID file
kill -0 $(cat ~/.lucyd/lucyd.pid) && echo "running" || echo "dead"

# HTTP status
curl http://localhost:8100/api/v1/status

# Prometheus metrics
curl http://localhost:8100/metrics
```

## Cron Jobs

| Schedule | Job | Command |
|---|---|---|
| `5 * * * *` | Git auto-commit | `cd workspace && git add -A && git commit -m "auto" && git push` |
| `10 * * * *` | Memory indexer | `lucydctl --index` |
| `15 * * * *` | Memory consolidation | `lucydctl --consolidate` |
| `5 3 * * *` | Trash cleanup | `find .trash/ -mtime +30 -delete` |
| `5 4 * * *` | Memory maintenance | `lucydctl --maintain` |
| `20 4 * * *` | Memory evolution | `lucydctl --evolve` |
| `5 4 * * 0` | DB integrity check | `sqlite3 memory/main.sqlite "PRAGMA integrity_check"` |

## Troubleshooting

### Daemon won't start: "Another instance is running"

PID file exists and process is live. Stop the running instance or remove stale PID file if process is dead:

```bash
cat ~/.lucyd/lucyd.pid
ps -p $(cat ~/.lucyd/lucyd.pid)
rm ~/.lucyd/lucyd.pid              # only if dead
```

### Messages not delivered

1. Check the bridge process is running (`ps aux | grep telegram.py`)
2. Check `allow_from` in `telegram.toml`
3. Check daemon log: `tail -50 ~/.lucyd/lucyd.log`

### Empty replies

1. Check for silent tokens — if reply matches a `silent_token`, it is suppressed
2. Check agentic loop: `grep "Tool call" ~/.lucyd/lucyd.log | tail -20`
3. Check for API errors: `grep "ERROR" ~/.lucyd/lucyd.log | tail -10`

### Timeout errors

Default timeout is 600s (`behavior.agent_timeout_seconds`). If consistently hitting it: check provider status, network, or whether the agent is stuck in a tool loop.

### High token costs

```bash
lucydctl --cost 2026-03
lucydctl --sessions
```

`max_turns_per_message` (default: 50) and `max_cost_per_message` cap per-message resource usage.
