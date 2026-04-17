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

## API-only operation

The daemon has no bundled CLI client. Every operation — bridges, cron
jobs, interactive chat, admin commands — is an HTTP client of the API.
Bearer auth via `LUCYD_HTTP_TOKEN`.

Example shell aliases (put these in your shell profile for ad-hoc ops):

```bash
alias lucy-status='curl -sf -H "Authorization: Bearer $LUCYD_HTTP_TOKEN" http://localhost:8100/api/v1/status | jq'
alias lucy-sessions='curl -sf -H "Authorization: Bearer $LUCYD_HTTP_TOKEN" http://localhost:8100/api/v1/sessions | jq'
alias lucy-reset='curl -sf -X POST -H "Authorization: Bearer $LUCYD_HTTP_TOKEN" -H "Content-Type: application/json" -d "{\"target\":\"all\"}" http://localhost:8100/api/v1/sessions/reset'
```

Interactive chat is planned as the `agentctl web` UI consuming
`/api/v1/chat/stream`. Until it exists, use curl + jq or a minimal SSE
client script.

## HTTP API

Auth via Bearer token from `LUCYD_HTTP_TOKEN` env var.

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

### Envelope

Every inbound message declares two fields; the endpoint pins the
`talker` (never overridable from the body):

| Endpoint | Talker | `sender` allowed |
|---|---|---|
| `POST /api/v1/chat` | operator | `cli`, `agentctl`, `web` |
| `POST /api/v1/chat/stream` | operator | `cli`, `agentctl`, `web` |
| `POST /api/v1/inbound/telegram` | user | auto-injected = `config.user.name` |
| `POST /api/v1/inbound/email` | user | auto-injected = `config.user.name` |
| `POST /api/v1/inbound/whatsapp` | user | reserved (501 Not Implemented) |
| `POST /api/v1/system/event` | system | `maintenance`, `automation`, `error` |
| `POST /api/v1/agent/action` | agent | `self`, `other` |

Invalid sender for the talker class → 400. Session key is always
`f"{talker}:{sender}"`.

---

### POST /api/v1/chat

Synchronous — sends an operator message and waits for the agent to respond.

**Request:**

| Field | Required | Default | Description |
|---|---|---|---|
| `message` | yes | — | Message text |
| `sender` | no | `"cli"` | One of `cli`, `agentctl`, `web` |
| `reply_to` | no | — | `"silent"` suppresses delivery (reply is still processed and logged) |
| `context` | no | — | Freeform label prepended as `[context]` |
| `attachments` | no | — | Base64-encoded file attachments |

**Response (200):**

```json
{"reply": "agent response", "session_id": "uuid", "tokens": {"input": 1500, "output": 200}}
```

When reply matches a `silent_token`: `{"reply": "...", "silent": true, ...}`.
On agentic loop error: `{"error": "exception message", "session_id": "uuid"}`.

**Errors:** 400 (invalid JSON, missing message, bad sender), 408 (timeout).

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

### POST /api/v1/inbound/{telegram,email,whatsapp}

Bridge endpoint. Forces `talker: user` and `sender: config.user.name`
server-side — the bridge only supplies content.

**Request:**

| Field | Required | Description |
|---|---|---|
| `message` | yes | Message text |
| `attachments` | no | Base64-encoded file attachments |

**Response:** Synchronous, same shape as `/chat`. Bridges send the
returned reply back out on their channel.

`whatsapp` returns 501 until the bridge is implemented.

---

### POST /api/v1/system/event

Fire-and-forget system-class events: cron maintenance, external webhooks,
bridge delivery-failure reports.

**Request:**

| Field | Required | Description |
|---|---|---|
| `message` | yes | Event text |
| `sender` | yes | `maintenance`, `automation`, or `error` |
| `attachments` | no | Base64-encoded attachments |

**Response (202):** `{"accepted": true, "queued_at": "ISO8601"}`.

Session auto-closes after processing; no reply is delivered.

---

### POST /api/v1/agent/action

Fire-and-forget agent self-actions: reminder firings, future agent-to-agent.

**Request:**

| Field | Required | Description |
|---|---|---|
| `message` | yes | Action text |
| `sender` | yes | `self` or `other` |
| `attachments` | no | Base64-encoded attachments |

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

See [architecture.md](architecture.md#metrics) for the full metric inventory.

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

Live agentic loop state. Returns in-memory monitor state or `{"state": "unknown"}`.

---

### POST /api/v1/sessions/reset

Reset sessions by target.

**Request:** `{"target": "all"}`, `{"target": "user"}` (shortcut for
`user:<config.user.name>`), `{"target": "talker:sender"}`, or
`{"target": "session-uuid"}`.

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

Force diary write + compaction on the single `user:<config.user.name>`
session. **Response:** 200 (completed), 200 (skipped — no user session), 408 (timeout).

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

## Unix Signals

| Signal | Effect | Handler |
|---|---|---|
| `SIGUSR1` | Reload workspace files (skill_loader.scan()) | `_setup_signals` |
| `SIGTERM` | Graceful shutdown (running = False) | `_setup_signals` |
| `SIGINT` | Same as SIGTERM | `_setup_signals` |

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

### Framework cron (entrypoint.sh)

Installed automatically by the Docker entrypoint. Each job is a direct
curl against the daemon's specialized maintenance endpoints:

| Schedule | Job | Endpoint |
|---|---|---|
| `10 * * * *` | Memory indexer | `POST /api/v1/index` |
| `15 * * * *` | Memory consolidation | `POST /api/v1/consolidate` |
| `50 3 * * *` | Diary write + compaction | `POST /api/v1/compact` |
| `5 4 * * *` | Memory maintenance | `POST /api/v1/maintain` |
| `20 4 * * *` | Memory evolution | `POST /api/v1/evolve` |

### Suggested operator cron (not in entrypoint.sh)

Optional jobs for production deployments — add to host or container crontab as needed:

| Schedule | Job | Command |
|---|---|---|
| `5 * * * *` | Git auto-commit workspace | `cd workspace && git add -A && git commit -m "auto" && git push` |
| `5 3 * * *` | Trash cleanup | `find .trash/ -mtime +30 -delete` |

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
2. Check `allow_from` in `[telegram]` section of `lucyd.toml`
3. Check daemon log: `tail -50 ~/.lucyd/lucyd.log`

### Empty replies

1. Check for silent tokens — if reply matches a `silent_token`, it is suppressed
2. Check agentic loop: `grep "Tool call" ~/.lucyd/lucyd.log | tail -20`
3. Check for API errors: `grep "ERROR" ~/.lucyd/lucyd.log | tail -10`

### Timeout errors

Default timeout is 600s (`behavior.agent_timeout_seconds`). If consistently hitting it: check provider status, network, or whether the agent is stuck in a tool loop.

### High token costs

```bash
curl -sf -H "Authorization: Bearer $LUCYD_HTTP_TOKEN" \
  "http://localhost:8100/api/v1/cost?period=2026-04" | jq
curl -sf -H "Authorization: Bearer $LUCYD_HTTP_TOKEN" \
  http://localhost:8100/api/v1/sessions | jq
```

`max_turns_per_message` (default: 50) and `max_cost_per_message` cap per-message resource usage.
