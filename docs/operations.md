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

## lucyd-send

CLI tool for injecting messages into the running daemon via its control FIFO (`~/.lucyd/control.pipe`).

```bash
# Send a user message (processed as if someone sent it)
~/lucyd/bin/lucyd-send --message "Hello there."

# Send a message from a named sender (gets its own session)
~/lucyd/bin/lucyd-send --message "Quick question." --from Claudio

# Send a system event (uses operational context tier by default)
~/lucyd/bin/lucyd-send --system "Execute HEARTBEAT.md. Follow task frequencies strictly."

# System event with explicit tier
~/lucyd/bin/lucyd-send --system "Health check" --tier operational

# Query token costs
~/lucyd/bin/lucyd-send --cost today
~/lucyd/bin/lucyd-send --cost week
~/lucyd/bin/lucyd-send --cost all

# List active sessions (context %, cost, log size, date range)
~/lucyd/bin/lucyd-send --sessions

# Reset session (archives old, next message starts fresh)
~/lucyd/bin/lucyd-send --reset              # Reset ALL sessions
~/lucyd/bin/lucyd-send --reset system
~/lucyd/bin/lucyd-send --reset user
~/lucyd/bin/lucyd-send --reset <session-uuid>
```

**Flags:**

| Flag | Purpose |
|---|---|
| `-m`, `--message <text>` | User message to inject |
| `-s`, `--system <text>` | System event to inject |
| `--from <name>` | Sender name for `--message` (default: `cli`). Each unique sender gets its own session. |
| `--tier <tier>` | Context tier: `full` (default for user) / `operational` (default for system) / `minimal` |
| `--cost [period]` | Query cost: `today` (default) / `week` / `all` |
| `--sessions` | List active sessions with context %, cost, log size, date range. Filesystem-only — no daemon needed. |
| `--monitor` | Show live API call monitor state. Filesystem-only — reads `~/.lucyd/monitor.json`. Use with `watch -n 1` for live updates. |
| `--reset [target]` | Reset sessions. No argument resets all. Target: sender name (`system` / `user`) or session UUID. Archives to `.archive/`, next message starts fresh. |
| `-a`, `--attach <file>` | Attach file(s) to the message. Can be repeated for multiple files. |
| `--state-dir <path>` | Override state directory (default: `~/.lucyd`) |

If the daemon is not running, `lucyd-send` exits with an error ("no reader on FIFO").

### Live Monitor

When the daemon processes a message, it writes real-time state to `~/.lucyd/monitor.json`. The `--monitor` flag reads this file and formats it for terminal display.

```bash
# One-shot status check
~/lucyd/bin/lucyd-send --monitor

# Live monitoring (updates every second)
watch -n 1 ~/lucyd/bin/lucyd-send --monitor
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

System events (`--system`) differ from user messages (`--message`) in four ways:

1. **Model**: Routed to the `subagent` model (Haiku), not `primary` (Sonnet)
2. **Context tier**: Defaults to `operational` (reduced file set: SOUL.md, AGENTS.md, HEARTBEAT.md)
3. **Reply suppression**: The agentic loop runs (tools execute, cost recorded), but reply text is not delivered to any channel
4. **Session framing**: The system prompt includes a "Session type: automated infrastructure" annotation so the LLM knows this is automation, not conversation

Example: the daily heartbeat cron job:

```
0 8 * * * ~/lucyd/bin/lucyd-send --system "Execute HEARTBEAT.md." --tier operational
```

The agent processes the heartbeat (spawns sub-agents, writes memory, sends messages via tools), but the textual response stays internal.

## HTTP API

When `[http] enabled = true` in `lucyd.toml`, the daemon starts an HTTP server alongside the primary channel.

### Configuration

```toml
[http]
enabled = true
host = "127.0.0.1"        # localhost only — safe default
port = 8100
```

Token is loaded from the `LUCYD_HTTP_TOKEN` environment variable (set in `.env` in the same directory as `lucyd.toml`).

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
```

**`/api/v1/chat`** fields:
| Field | Required | Default | Description |
|---|---|---|---|
| `message` | yes | — | Message text |
| `sender` | no | `"default"` | Session key (each unique sender gets its own session, prefixed with `http-`) |
| `context` | no | — | Freeform label prepended as `[context]` (for debugging) |
| `tier` | no | `"full"` | Context tier override |

**`/api/v1/notify`** fields:
| Field | Required | Default | Description |
|---|---|---|---|
| `message` | yes | — | Natural language message for the LLM |
| `source` | no | — | Source label, prefixed as `[source: ...]` in LLM text |
| `ref` | no | — | Reference ID, prefixed as `[ref: ...]` in LLM text |
| `data` | no | — | Arbitrary JSON payload (passed through as `notify_meta` to webhook, not in LLM text) |
| `sender` | no | `"default"` | Session key (prefixed with `http-`) |

**`/api/v1/sessions`** — Returns list of active sessions with context %, cost, log size, and date range. Same data as `lucyd-send --sessions`.

**`/api/v1/cost`** — Query cost breakdown by period. Query parameter: `period` (`today` | `week` | `all`, default: `today`). Same data as `lucyd-send --cost`.

### Behavior

- `/chat` waits for the agentic loop to complete (up to `agent_timeout_seconds`). Returns 408 on timeout.
- `/notify` queues the event and returns immediately. The agent processes it asynchronously.
- HTTP messages bypass the debounce window — each `/chat` request is processed immediately.
- Channel delivery is suppressed for HTTP sources — replies go to the HTTP response, not to Telegram. The agent can still use the `message` tool to send notifications via Telegram during processing.

### Webhook Callback

When `[http] callback_url` is configured, the daemon POSTs a JSON payload after every processed message:

```json
{
  "reply": "agent response text",
  "session_id": "session-uuid",
  "sender": "http-default",
  "source": "http",
  "silent": false,
  "tokens": {"input": 1500, "output": 200},
  "notify_meta": {"source": "email-monitor", "ref": "msg-123", "data": {...}}
}
```

- **Auth:** Bearer token via `[http] callback_token_env` (env var name in config, value loaded from environment)
- **Timeout:** Configurable via `[http] callback_timeout` (default: 10 seconds). Log-and-forget on failure — webhook errors never block message processing.
- **Zero overhead** when unconfigured (empty `callback_url` = no-op).
- **`notify_meta`:** Echoes the `source`, `ref`, and `data` fields from `/notify` requests. `null` for non-notify messages.

Configuration:

```toml
[http]
callback_url = "https://n8n.local/webhook/abc"    # POST target after every message
callback_token_env = "MY_CALLBACK_TOKEN"           # Env var for bearer token
```

## Unix Signals

The daemon handles three Unix signals for runtime control:

| Signal | Effect |
|---|---|
| `SIGUSR1` | Reload workspace files and rescan skills. Context is rebuilt on the next message. |
| `SIGUSR2` | Write `status.json` to `~/.lucyd/` with PID, uptime, loaded tools, channel type, and model list. |
| `SIGTERM` | Graceful shutdown. Finishes the current message, cleans up PID file and FIFO, exits. |

```bash
# Reload workspace after editing personality/skill files
kill -USR1 $(cat ~/.lucyd/lucyd.pid)

# Dump status
kill -USR2 $(cat ~/.lucyd/lucyd.pid)
cat ~/.lucyd/status.json

# Graceful stop (equivalent to systemctl stop)
kill -TERM $(cat ~/.lucyd/lucyd.pid)
```

`SIGINT` (Ctrl+C) is handled identically to `SIGTERM`.

## CLI Mode

For testing without Telegram, run with `--channel cli`:

```bash
cd ~/lucyd
source .venv/bin/activate
python3 lucyd.py --channel cli
```

This reads from stdin and writes to stdout. The daemon runs the full agentic loop, tool execution, and session management -- just without Telegram transport. Useful for debugging prompts, tool behavior, or context building.

When stdin reaches EOF (piped input or Ctrl+D), the daemon drains pending messages and exits cleanly.

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

### Telegram channel connected

```bash
# Check that the Lucyd daemon is running and polling Telegram
systemctl is-active lucyd

# Check daemon log for Telegram connection
tail -20 ~/.lucyd/lucyd.log | grep -i telegram
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
- `channel` -- active channel type
- `models` -- loaded model names

## Cron Jobs

Recommended cron jobs for long-running deployments:

| Schedule | Job | Purpose |
|---|---|---|
| `5 * * * *` | Workspace git auto-commit | `git add -A && commit && push` in the workspace directory |
| `10 * * * *` | Memory indexer | `lucyd-index` — scans workspace, chunks, embeds, writes to FTS5 + vector DB |
| `15 * * * *` | Memory consolidation | `lucyd-consolidate` — extracts structured facts, episodes, commitments from sessions |
| `5 3 * * *` | Trash cleanup | Delete files in `.trash/` older than 30 days |
| `5 4 * * *` | Memory maintenance | `lucyd-consolidate --maintain` — clean up low-confidence facts and stale entries |
| `5 4 * * 0` | DB integrity check | `PRAGMA integrity_check` on memory SQLite DB |
| `5 8 * * *` | Heartbeat | `lucyd-send --system` to trigger `HEARTBEAT.md` tasks |

```bash
# View crontab
crontab -l

# Edit crontab
crontab -e
```

The heartbeat cron is commented out by default. Uncomment when ready:

```cron
5 8 * * * ~/lucyd/bin/lucyd-send --system "Execute HEARTBEAT.md. Follow task frequencies strictly." --tier operational
```

## Troubleshooting

### Daemon won't start: "Another instance is running"

The PID file (`~/.lucyd/lucyd.pid`) exists and the PID is live. Either stop the running instance or, if the process is genuinely dead, remove the stale PID file:

```bash
cat ~/.lucyd/lucyd.pid
ps -p $(cat ~/.lucyd/lucyd.pid)    # check if alive
rm ~/.lucyd/lucyd.pid              # only if dead
```

### Daemon starts but no messages are delivered

1. Check Telegram channel: the daemon handles Bot API long polling directly (no external service needed)
2. Check `allow_from` in `lucyd.toml` -- sender must be in the allowlist (numeric Telegram user IDs)
4. Check daemon log for errors: `tail -50 ~/.lucyd/lucyd.log`

### Messages processed but replies are empty

1. Check for silent tokens -- if the reply starts or ends with `HEARTBEAT_OK` or `NO_REPLY`, it is suppressed.
2. Check the agentic loop: `grep "Tool call" ~/.lucyd/lucyd.log | tail -20`
3. Check for API errors: `grep "ERROR" ~/.lucyd/lucyd.log | tail -10`

### API timeout errors

The default timeout is 600 seconds (`behavior.agent_timeout_seconds`). If consistently hitting it:

1. Check provider status
2. Check network connectivity
3. Review whether the agent is stuck in a tool loop: `grep "Tool call" ~/.lucyd/lucyd.log | tail -50`

### FIFO errors from lucyd-send

"No reader on FIFO" means the daemon is not running. Start it first:

```bash
sudo systemctl start lucyd
```

"FIFO not found" means the pipe was never created (daemon hasn't started) or was cleaned up. Start the daemon to recreate it.

### High token costs

```bash
# Check today's cost by model
~/lucyd/bin/lucyd-send --cost today

# Check weekly
~/lucyd/bin/lucyd-send --cost week
```

Review recent sessions for excessive tool loops:

```bash
ls -lt ~/.lucyd/sessions/ | head
```

The `max_turns_per_message` setting (default: 50) caps tool-use iterations per message.
