# Security Audit Report

**Date:** 2026-03-09
**Audit Cycle:** 17
**EXIT STATUS:** PASS

## Changes Since Cycle 16

1. **Primary sender routing:** `[behavior] primary_sender` routes notifications to the named sender's session. New config property in `config.py`, routing logic in `lucyd.py:_message_loop()`.
2. **Passive telemetry buffer:** `[behavior] passive_notify_refs` buffers high-frequency notifications. New `_telemetry_buffer` dict in `LucydDaemon`, drained via `_drain_telemetry()` into next message.
3. **Compaction token awareness:** `{max_tokens}` placeholder in compaction prompt, `session.py` split-point boundary fix for `tool_results`.
4. **lucyd-send overhaul:** New `--status`, `--log` query commands. Restructured argument groups. `--notify` now sets `notify: true` flag. `--system` accepts `--from` for sender override.
5. **HTTP /notify:** Added `"notify": True` key to queue item (one line in `http_api.py`).

## Pattern Checks

| Pattern | Result |
|---------|--------|
| P-003 (unchecked filesystem write) | CLEAN — no new file-path parameters in any tool. Re-verified all 17 tool functions: `tool_read`, `tool_write`, `tool_edit` call `_check_path()`; `tool_message` validates attachment paths via `_check_path()`; `tool_tts` validates `output_file` via `_check_path()`; remaining tools have no file-path parameters. |
| P-009 (capability table stale) | CLEAN — re-derived: 19 built-in tools across 11 modules + plugin system. No new tools added since cycle 16. No tool files changed (`git diff 78ac477..HEAD -- tools/` empty). |
| P-011 (config-to-doc label mismatch) | NOT APPLICABLE to Stage 6 (Stage 7 pattern). Noted: no config label changes. |
| P-012 (auto-populated misclassified) | CLEAN — `_telemetry_buffer` is populated from runtime notifications, keyed by `ref` from config-defined `passive_notify_refs`. Buffer is latest-value (one entry per ref), bounded by config list size. No auto-populated data reaches tool arguments, file paths, or SQL. |
| P-014 (unhandled errors at boundaries) | CLEAN — new code paths (`_drain_telemetry`, `_handle_compact`, notification routing) operate within existing error-handled contexts. `_process_message` wraps all agentic loop failures. No new external API calls. |
| P-016 (resource lifecycle) | CLEAN — `_telemetry_buffer` is a plain dict, not a resource requiring close. No new connections, clients, or file handles. Existing lifecycle verified: `_memory_conn` closed in `run()` finally block; httpx client closed in `channel.disconnect()`. |
| P-018 (unbounded structures) | CLEAN — `_telemetry_buffer` bounded by `_passive_refs` (config-defined, not attacker-controlled). `_last_inbound_ts` bounded at 1000 via LRU eviction. `_RateLimiter._hits` has sweep at >1000 keys. No new unbounded structures. |
| P-022 (hardcoded channel identifiers) | CLEAN — grep of framework code (`lucyd.py`, `config.py`, `context.py`, `session.py`, `agentic.py`, `tools/`) shows no transport-specific identifiers beyond `tools/messaging.py` tool description ("Telegram-allowed reaction emoji") and `config.py` comment. Both are documentation, not logic. |
| P-028 (control endpoint audit) | **FINDING** — see Vulnerabilities section below. HTTP `POST /api/v1/compact` calls `_handle_compact()` → `_process_message()` directly, bypassing the message queue. Can race with `_message_loop`'s `_process_message`. |

## Input Sources

| Source | Protocol | Authentication | Risk Level |
|--------|----------|---------------|------------|
| Telegram | Bot API long polling | `allow_from` user ID whitelist | MEDIUM — authenticated external |
| HTTP API | REST (aiohttp) | Bearer token + `hmac.compare_digest` | MEDIUM — authenticated external |
| FIFO | Named pipe (`control.pipe`) | Unix filesystem permissions (`0o600`) | LOW — local only |
| Config files | TOML on disk | Filesystem permissions | LOW — admin-managed |
| Skill files | Markdown on disk | Filesystem permissions | LOW — admin-managed, text-only injection |
| Environment vars | Process env | OS-level | LOW — admin-managed |

No new input sources since cycle 16.

## Capabilities (Re-derived from Source)

| Capability | Tool | Danger Level | Boundaries |
|------------|------|-------------|------------|
| Shell execution | `exec` | CRITICAL | `_safe_env()`, timeout (600s max), `start_new_session` |
| File read | `read` | CRITICAL | `_check_path()` allowlist, `resolve()` (symlink-safe) |
| File write | `write` | CRITICAL | `_check_path()` allowlist |
| File edit | `edit` | CRITICAL | `_check_path()` allowlist |
| Web fetch (SSRF) | `web_fetch` | HIGH | `_validate_url()`, `_is_private_ip()`, DNS pinning, redirect validation |
| Web search | `web_search` | MEDIUM | Hardcoded Brave API URL, API key |
| Send messages | `message` | HIGH | Contact name resolution, self-send block, `_check_path()` on attachments |
| Emoji reaction | `react` | LOW | Allowed emoji set, contact resolution |
| Sub-agent spawn | `sessions_spawn` | CRITICAL | `_subagent_deny` deny-list, `max_turns`, `timeout` |
| Skill loading | `load_skill` | MEDIUM | Dict key lookup in `_skills` (text-only, no code execution) |
| Schedule message | `schedule_message` | LOW | `_max_scheduled` (50), `_max_delay` (86400s), contact resolution |
| List scheduled | `list_scheduled` | LOW | Read-only |
| Memory search | `memory_search` | LOW | Read-only, parameterized SQL |
| Memory get | `memory_get` | LOW | Workspace-relative paths via `MemoryInterface` |
| Memory write | `memory_write` | LOW | Parameterized SQL, entity normalization |
| Memory forget | `memory_forget` | LOW | Parameterized SQL |
| Commitment update | `commitment_update` | LOW | Parameterized SQL, enum-restricted status |
| Session status | `session_status` | LOW | Read-only |
| TTS | `tts` | MEDIUM | `_check_path()` on output_file, tempfile for default |

## Input-Capability-Boundary Table

| Input -> Capability | Boundary | Tested? | Status |
|---------------------|----------|---------|--------|
| Telegram -> Shell | `allow_from` whitelist + `_safe_env()` + timeout | Yes (mutation) | VERIFIED |
| Telegram -> Filesystem | `allow_from` + `_check_path()` allowlist | Yes (mutation) | VERIFIED |
| Telegram -> Web fetch | `allow_from` + `_validate_url()` + `_is_private_ip()` + DNS pinning | Yes (mutation) | VERIFIED |
| Telegram -> Sub-agent | `allow_from` + `_subagent_deny` + limits | Yes (mutation) | VERIFIED |
| Telegram -> Message send | `allow_from` + contact resolution + self-send block | Yes | VERIFIED |
| HTTP API -> Shell | Bearer token + rate limit + `_safe_env()` + timeout | Yes (mutation) | VERIFIED |
| HTTP API -> Filesystem | Bearer token + rate limit + `_check_path()` | Yes (mutation) | VERIFIED |
| HTTP API -> Web fetch | Bearer token + rate limit + SSRF protection | Yes (mutation) | VERIFIED |
| HTTP API -> Sub-agent | Bearer token + rate limit + `_subagent_deny` | Yes (mutation) | VERIFIED |
| FIFO -> Shell | Unix perms (0o600) + `_safe_env()` + timeout | Yes | VERIFIED |
| FIFO -> Filesystem | Unix perms + `_check_path()` | Yes | VERIFIED |
| FIFO -> Web fetch | Unix perms + SSRF protection | Yes | VERIFIED |
| Config -> Behavior | Filesystem permissions (admin-managed) | N/A | ACCEPTED RISK |
| Skills -> System prompt | Filesystem permissions (text-only, no exec) | Yes | VERIFIED |
| Memory v2 -> Context | Parameterized SQL, text-only injection into LLM context | Yes | ACCEPTED RISK |

## Tool Parameter Validation Matrix

| Tool | Parameter | Type | Validation | Tested |
|------|-----------|------|------------|--------|
| read | file_path | str | `_check_path()` | 14 tests, mutation |
| write | file_path | str | `_check_path()` | 14 tests, mutation |
| edit | file_path | str | `_check_path()` | 14 tests, mutation |
| exec | command | str | `_safe_env()` (env filtering, no command filtering) | 16 tests, mutation |
| exec | timeout | int | clamped to `_MAX_TIMEOUT` (600s) | Yes |
| web_fetch | url | str | `_validate_url()` + `_is_private_ip()` + DNS pin | 20+ tests, mutation |
| web_search | query | str | Passed to API as parameter (URL-encoded) | Yes |
| message | target | str | Contact name lookup, self-send block | Yes |
| message | attachments | list[str] | `_check_path()` per path | Yes |
| tts | output_file | str | `_check_path()` if non-empty; tempfile if empty | Yes |
| sessions_spawn | tools | list[str] | Intersected with registered tools, `_subagent_deny` applied | Yes |
| sessions_spawn | max_turns | int | Resolved from config default | Yes |
| load_skill | name | str | Dict key lookup (not filesystem) | Yes |
| memory_get | file_path | str | Workspace-relative, via MemoryInterface (not filesystem) | Yes |
| memory_write | entity/attribute | str | `_normalize_entity()`, parameterized SQL | Yes |
| commitment_update | status | str | Enum: done/expired/cancelled (schema validation) | Yes |

## Dependency Audit Results

```
pip-audit --strict: No known vulnerabilities found
```

| Package | Version | Status |
|---------|---------|--------|
| anthropic | 0.81.0 | Clean |
| openai | 2.21.0 | Clean |
| httpx | 0.28.1 | Clean |
| aiohttp | 3.13.3 | Clean |
| certifi | 2026.2.25 | Current |
| pypdf | 6.7.5 | Clean (CVE-2026-28804 fix retained) |

## Secret Handling Verification

| Check | Result |
|-------|--------|
| API keys via `api_key_env` pattern (env var name in config, resolved at runtime) | VERIFIED |
| `_safe_env()` filters `LUCYD_*` prefix and `_KEY`, `_TOKEN`, `_SECRET`, `_PASSWORD`, `_CREDENTIALS`, `_ID`, `_CODE`, `_PASS` suffixes | VERIFIED (16 tests, 100% mutation kill) |
| No credential values in log statements | VERIFIED — only env var names logged (e.g., `"No API key for primary model (env var '%s' not set)"`) |
| `.env` not committed (gitignored) | VERIFIED |
| HTTP auth token via env var, not hardcoded | VERIFIED |
| TTS API key passed at configure() time from env var resolution | VERIFIED |
| Webhook callback token via `callback_token_env` pattern | VERIFIED |

## Critical Path Verification

### 1. External text -> Shell execution
**Status:** VERIFIED
- Boundary: `_safe_env()` strips secrets from subprocess environment. `start_new_session=True` isolates process group. Timeout enforced (max 600s). Process group killed on timeout (`os.killpg`).
- No command deny-list (by design: LLM is responsible for command selection; tool-level boundary is env filtering + timeout + process isolation).
- Tests: 16 tests in `test_shell_security.py`, 100% mutation kill.

### 2. External text -> File read/write
**Status:** VERIFIED
- Boundary: `_check_path()` resolves symlinks via `Path.resolve()`, checks against configurable allowlist (`prefix + os.sep` match).
- Handles: path traversal (`../`), symlinks, tilde expansion.
- Default: empty allowlist = all access denied (fail-closed).
- Tests: 14 tests in `test_filesystem.py`, 100% mutation kill.

### 3. External text -> Web requests (SSRF)
**Status:** VERIFIED
- Boundary: `_validate_url()` checks scheme (http/https only), resolves hostname via `socket.getaddrinfo()`, checks all IPs via `_is_private_ip()`.
- `_is_private_ip()` handles standard IPs + octal/hex/decimal encoding via `socket.inet_aton()` fallback. Fails closed (unknown format = blocked).
- DNS rebinding protection: IP pinned at validation time, custom `_IPPinnedHTTPS*` handlers force connection to validated IP.
- Redirect validation: `_SafeRedirectHandler` re-validates each hop.
- Tests: 20+ tests in `test_web_security.py` covering standard IPs, octal, hex, decimal, IPv6 loopback, redirects, scheme filtering.

### 4. External text -> Sub-agent spawning
**Status:** VERIFIED
- Deny-list: `_DEFAULT_SUBAGENT_DENY = frozenset({"sessions_spawn", "tts", "react", "schedule_message"})`. Configurable via `tools.subagent_deny`.
- `sessions_spawn` is in its own deny-list (no recursive spawning).
- Limits: `max_turns`, `timeout` from config.
- Sub-agents use same `_tool_registry` (same boundaries apply to all tools).
- Tests: 5+ tests.

### 5. External text -> Message sending
**Status:** VERIFIED
- Contact resolution: dict lookup (`_contacts`), case-insensitive. Unknown contacts raise `ValueError`.
- Self-send: blocked at channel level (`chat_id == self._bot_id`).
- Attachment paths: `_check_path()` in `tool_message` (line 51).

### 6. HTTP API -> All capabilities
**Status:** VERIFIED
- Auth: Bearer token, timing-safe via `hmac.compare_digest`.
- No token configured = 503 (fail-closed, not bypass).
- Rate limiting: per-IP, configurable. Status endpoints have separate (higher) limit.
- Body size cap: `client_max_size` in aiohttp application.

### 7. Quote reply context -> LLM injection
**Status:** VERIFIED (ACCEPTED RISK)
- Quote text truncated at 200 chars.
- Injected as `[replying to: {quote}]` prefix — standard text, no special processing.
- Same trust level as direct message input. Prompt injection via quote is theoretically possible but no different from prompt injection via the message itself.

### 8. Attachments -> File system
**Status:** VERIFIED
- Telegram: downloads to `download_dir/timestamp_filename`. `Path(filename).name` strips directory components.
- HTTP API: base64-decoded, saved to `download_dir/timestamp_Path(filename).name`. Same name sanitization.
- No path traversal possible: `Path(filename).name` returns only the final component.

### 9. External text -> Memory poisoning
**Status:** ACCEPTED RISK
- Structured facts: stored via parameterized SQL, never reach tool arguments or file paths.
- Session JSONL: `json.dumps()` / `json.loads()` round-trip, control characters escaped.
- Memory v2 facts appear in context as text only. The LLM may be influenced by poisoned facts, but tool-level boundaries remain intact regardless of LLM state.
- Verified: no code path uses `fact.value` as tool input, file path, or SQL beyond parameterized lookups.

### 10. Config/skill files -> Behavior modification
**Status:** VERIFIED
- Skills: text-only, injected into system prompt. Custom frontmatter parser (no PyYAML, no `eval`). `_parse_frontmatter` handles only `key: value`, `>`, `|` block scalars.
- TOML config: Python `tomllib` (stdlib), data-only, no code execution.
- `load_skill` tool: dict key lookup in pre-scanned `_skills` dict. No filesystem path from LLM.

### 11. Dispatch safety
**Status:** VERIFIED
- `ToolRegistry.execute()`: dict key lookup (`self._tools[name]`). Name from LLM can only match registered tools.
- No `getattr()`, `__import__()`, `importlib`, `eval()`, or `exec()` in dispatch paths.
- `getattr()` in tools/agents.py: used on `_config` object with literal attribute names, not user input.
- `getattr()` in tools/web.py: used on urllib `Request` objects for IP pinning attributes (framework-controlled).

### 12. Dependency supply chain
**Status:** VERIFIED
- `pip-audit --strict`: 0 known vulnerabilities.
- All runtime deps reputable: anthropic, openai, httpx, aiohttp.
- Versions minimum-pinned (`>=`) — installed versions verified clean.

## Vulnerabilities Found

| # | Path | Severity | Status | Details |
|---|------|----------|--------|---------|
| 1 | HTTP `/compact` -> `_process_message` | LOW | OPEN | See below |

### V-1: HTTP `/api/v1/compact` bypasses message queue (P-028)

**Path:** HTTP `POST /api/v1/compact` -> `_handle_compact()` -> `_process_message()` (direct call, not queued)

**Issue:** The compact endpoint calls `_process_message()` directly, not through the message queue. If the `_message_loop` is concurrently processing a message for the same session, both coroutines can interleave at `await` points, potentially corrupting session state (duplicate messages, wrong message order, state file inconsistency).

**Contrast:** FIFO compact goes through the queue (serialized). HTTP evolve goes through the queue. HTTP reset goes through the control queue. Only HTTP compact bypasses.

**Severity:** LOW — requires exact timing (compact request while primary session message is mid-agentic-loop). Single-operator deployment makes this unlikely. No data loss (session recovers from JSONL on restart). No security boundary bypass.

**Mitigation available:** Route HTTP compact through the message queue (like FIFO compact already does). The queue item type `"compact"` already exists and is handled in `_message_loop` at line 1647.

**Status:** OPEN — documenting for fix in next maintenance cycle. Not a security boundary failure (both paths are authenticated). This is a concurrency correctness issue, not a security vulnerability.

## Bypass Analysis

| Technique | Applicable? | Handled? | Details |
|-----------|------------|----------|---------|
| Path traversal (`../`) | Yes | YES | `Path.resolve()` in `_check_path()`, tested with symlinks |
| SSRF encoding (octal, hex, decimal) | Yes | YES | `socket.inet_aton()` fallback normalizes all encodings |
| SSRF DNS rebinding | Yes | YES | IP pinned at validation, custom handlers connect to validated IP |
| SSRF redirects | Yes | YES | `_SafeRedirectHandler` re-validates each hop |
| Command injection | Yes | N/A | LLM controls full command (by design); boundary is env filtering |
| Env var leakage | Yes | YES | `_safe_env()` strips all `LUCYD_*` + secret suffixes |
| Memory poisoning (session) | Yes | ACCEPTED | JSONL-safe; tool boundaries contain LLM regardless of context |
| Structured memory poisoning (facts) | Yes | ACCEPTED | Parameterized SQL only; facts never reach tool args or file paths |
| Resource exhaustion (API cost) | Yes | MITIGATED | `max_cost_per_message`, `max_turns` limits |
| Resource exhaustion (sub-agents) | Yes | MITIGATED | `sessions_spawn` self-denied, `max_turns`/`timeout` per sub-agent |
| Resource exhaustion (disk) | Partial | MITIGATED | Download dirs cleaned on disconnect; output truncation |
| Dynamic dispatch injection | No | YES | Dict key lookup only; no eval/getattr on user input |
| Supply chain (dep CVEs) | Yes | YES | pip-audit clean; 0 known vulnerabilities |
| Skill prompt injection | Yes | ACCEPTED | Skills are text-only; tool boundaries contain LLM regardless |

## Boundary Verification Summary

All boundaries unchanged from Cycle 16. All mutation-verified kill rates carry forward:

| Boundary | Exists | Tested | Mutation Verified | Fails Closed |
|----------|--------|--------|-------------------|-------------|
| `_safe_env()` | YES | 16 tests | 100% kill | YES |
| `_safe_parse_args()` | YES | 5 tests | 100% kill | YES (returns `{"raw": ...}`) |
| `_check_path()` | YES | 14 tests | 100% kill | YES (empty allowlist = deny all) |
| `_is_private_ip()` | YES | 20+ tests | 2 equiv | YES (unknown = blocked) |
| `_validate_url()` | YES | 13+ tests | 3 cosmetic | YES (blocked scheme = reject) |
| `_subagent_deny` | YES | 5+ tests | 100% kill | YES (default deny set includes self) |
| `_auth_middleware` | YES | 15+ tests | 100% kill | YES (no token = 503) |
| `_rate_middleware` | YES | 3+ tests | 100% kill | YES (exceeded = 429) |
| `hmac.compare_digest` | YES | tested | 100% kill | YES |
| `verify_compaction_summary` | YES | 39 tests | 81.5% kill | YES (fallback to deterministic) |
| Telegram `allow_from` | YES | 4 tests | — | YES (empty = deny all non-whitelisted) |
| HTTP body size `client_max_size` | YES | tested | — | YES (aiohttp rejects oversized) |

## New Path Analysis

### Primary sender routing (`config.primary_sender`)

- **Input:** `item.get("notify")` flag + `self.config.primary_sender` value
- **Processing:** In `_message_loop`, replaces `sender` with config value before debounce
- **Security impact:** NONE — `primary_sender` is admin-configured, not attacker-controlled. Route change only affects session key, not tool boundaries. Same agentic loop, same tool permissions regardless of sender name.
- **Session safety:** `session_preexisted` check prevents auto-close of the primary session when notifications route to it.

### Passive telemetry buffer (`_telemetry_buffer`)

- **Input:** Notification text from FIFO/HTTP `/notify` matching `passive_notify_refs` config
- **Processing:** Stored as latest-value per ref, drained into next user message as `[telemetry: ...]` prefix
- **Security impact:** NONE — telemetry text enters the LLM context as regular text. Same prompt injection exposure as any notification. Tool boundaries contain the LLM regardless.
- **DoS protection:** Buffer keyed by `ref` (config-bounded set). Non-matching refs skip buffer and process normally. `priority=active` bypasses buffer. `max_age` (30s) in `_drain_telemetry` evicts stale entries.

### lucyd-send --status / --log

- **Input:** Reads state files from `state_dir` (PID file, config, sessions.json, cost.db, log file)
- **Security impact:** NONE — read-only filesystem operations on daemon state. No FIFO write. No daemon interaction. `show_log` uses `deque(maxlen=lines)` for bounded memory.
- **Permissions:** Runs as the same user that owns state files. No privilege escalation path.

### Compaction token limit in prompt

- **Change:** `{max_tokens}` placeholder in `compaction_prompt` string, replaced at runtime
- **Security impact:** NONE — string replacement on admin-configured prompt template with integer value from config

## Recommendations

1. **Route HTTP `/compact` through queue** (V-1 fix): Change `http_api.py` `_handle_compact` to enqueue a `compact` type item instead of calling `_handle_compact_cb()` directly. The queue handler already exists in `_message_loop`. This matches the pattern used by FIFO compact and HTTP evolve.

## Test Suite

- **Total tests:** 1725, all passing
- **Security-related tests:** 209 (selected by keyword: safe_env, check_path, private_ip, validate_url, subagent_deny, auth, rate, security, ssrf, path_traversal, boundary, shell)

## Confidence

**Overall: 96%**

- Security boundaries: 98% — all verified, no changes to tool or boundary code since cycle 16
- Supply chain: 100% — pip-audit clean
- New code: 95% — telemetry buffer and primary routing are safe by construction; compact endpoint concurrency issue documented
- Concurrency: 90% — V-1 is a correctness issue, not a security boundary failure; serialization via queue would eliminate the risk entirely
