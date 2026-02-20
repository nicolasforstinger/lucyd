# Security Audit Report

**Date:** 2026-02-20
**EXIT STATUS:** PASS
**Triggered by:** Vision/STT feature implementation + Memory v2 recall personality audit

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-003 (unchecked filesystem write in tool params) | CLEAN | 19 tools re-inspected. All path-like parameters verified: `tool_read/write/edit` → `_check_path()`; `tool_message` attachments → `_check_path()`; `tool_tts` output_file → `_check_path()`. `tool_memory_get` file_path is a SQLite query key (parameterized), NOT a filesystem path — no `_check_path` needed. |
| P-009 (capability table stale) | CLEAN | Re-derived from source: 19 tools across 11 modules. New since Cycle 2: `memory_write`, `memory_forget`, `commitment_update` (all parameterized SQL, no filesystem). Vision/STT are internal dispatch, not LLM-accessible tools. |
| P-012 (auto-populated misclassified as static) | CLEAN | No data sources referred to as "admin-managed" or "static" that are actually auto-populated. Config/skills confirmed manually authored. STT config from `lucyd.toml` confirmed static. |

## Threat Model

Lucyd is a single-user AI daemon on a Debian VM (local network, Cloudflare Tunnel for Telegram). The LLM is UNTRUSTED for security decisions — all boundaries are enforced at tool level in code.

**Attack surface:**
- Telegram Bot API (public, user-ID-filtered via `allow_from`)
- HTTP REST API (127.0.0.1 by default, bearer token auth)
- Local FIFO (owner-only permissions `0o600`)

**Primary threats:** prompt injection via Telegram/web content, SSRF via web tools, path traversal via filesystem tools, credential leakage via shell tool, sub-agent privilege escalation, structured memory poisoning via consolidation pipeline.

## Input Sources

| Source | Protocol | Authentication | Risk Level |
|--------|----------|---------------|------------|
| Telegram messages | Bot API long polling (HTTPS) | Bot token + `allow_from` user ID whitelist (telegram.py:174) | Medium |
| Telegram attachments | Bot API `getFile` (HTTPS) | Same as messages; size limit in lucyd.py:480 | Medium |
| Telegram voice messages | Bot API `getFile` → STT dispatch | Same; audio → transcription → text context | Medium |
| HTTP `/api/v1/chat` | REST POST | Bearer token (`hmac.compare_digest`, http_api.py:95) + rate limit (30/min) | Medium |
| HTTP `/api/v1/notify` | REST POST | Same as /chat | Medium |
| HTTP `/api/v1/status` | REST GET | Same (relaxed rate limit: 60/min) | Low |
| FIFO control pipe | Local IPC (JSON) | File permissions `0o600` (lucyd.py:75) | Low |
| Config (TOML) | Filesystem | Admin-managed, version-controlled | Low |
| Workspace/skills | Filesystem | Admin-managed workspace | Low |
| Session JSONL | Filesystem | Daemon-written; `json.loads()` per line | Low |
| Environment vars | Process env | Systemd unit context | Low |

## Capabilities (re-derived from source — P-009)

| Capability | Tool | Module | Danger Level | Boundaries |
|------------|------|--------|-------------|------------|
| Shell execution | `exec` | shell.py | CRITICAL | `_safe_env()`, timeout (max 600s), `start_new_session=True` |
| File read | `read` | filesystem.py | CRITICAL | `_check_path()` allowlist + `Path.resolve()` |
| File write | `write` | filesystem.py | CRITICAL | `_check_path()` allowlist + `Path.resolve()` |
| File edit | `edit` | filesystem.py | CRITICAL | `_check_path()` allowlist + `Path.resolve()` |
| Web fetch (SSRF) | `web_fetch` | web.py | HIGH | `_validate_url()` + `_is_private_ip()` + `_SafeRedirectHandler` |
| Sub-agent spawn | `sessions_spawn` | agents.py | HIGH | `_SUBAGENT_DENY` set, model via dict lookup, `max_turns=10` |
| Send messages | `message` | messaging.py | HIGH | `_resolve_target()` self-send block; attachment paths via `_check_path()` |
| TTS generation | `tts` | tts.py | MEDIUM | `_check_path()` on explicit output_file; temp file default in configured dir |
| Web search | `web_search` | web.py | MEDIUM | Hardcoded Brave API endpoint; query URL-encoded |
| Memory search | `memory_search` | memory_tools.py | LOW | Read-only; parameterized SQL + vector search |
| Memory get | `memory_get` | memory_tools.py | LOW | Read-only; parameterized SQL lookup by path key |
| Memory write | `memory_write` | structured_memory.py | LOW | Parameterized SQL, entity normalization |
| Memory forget | `memory_forget` | structured_memory.py | LOW | Parameterized SQL (soft-delete only) |
| Commitment update | `commitment_update` | structured_memory.py | LOW | Parameterized SQL, enum-restricted status (`done`/`expired`/`cancelled`) |
| Load skill | `load_skill` | skills_tool.py | LOW | Dict key lookup in pre-scanned skills; denied to sub-agents |
| Schedule message | `schedule_message` | scheduling.py | LOW | Max 50 messages, max 24h delay; denied to sub-agents |
| List scheduled | `list_scheduled` | scheduling.py | LOW | Read-only |
| Session status | `session_status` | status.py | LOW | Read-only internal state |
| React | `react` | messaging.py | LOW | `ALLOWED_REACTIONS` whitelist (59 emoji); denied to sub-agents |

## Path Matrix

| Input → Capability | Boundary | Tested? | Mutation Verified? | Status |
|-------------------|----------|---------|-------------------|--------|
| Telegram → Shell exec | `_safe_env()` + timeout + `start_new_session` | Yes | `_safe_env` 88% (7/8, P-004 survivor) | VERIFIED |
| Telegram → File read/write/edit | `_check_path()` allowlist | Yes | `_check_path` 100% (10/10) | VERIFIED |
| Telegram → Web fetch | `_validate_url()` + `_is_private_ip()` + `_SafeRedirectHandler` | Yes | 80–86% | VERIFIED |
| Telegram → Sub-agent spawn | `_SUBAGENT_DENY` set | Yes | deny-list 100% | VERIFIED |
| Telegram → Message send | `_resolve_target()` self-send block + `_check_path()` on attachments | Yes | N/A (contract test) | VERIFIED |
| Telegram voice → STT → text | Controlled download path + ffmpeg list args + timeout | Yes (13 tests) | N/A (not tool boundary) | VERIFIED |
| Telegram image → Vision API | Controlled download path + size limit + transient injection | Yes (3 tests) | N/A (not tool boundary) | VERIFIED |
| HTTP API → All tools | Bearer token (`hmac.compare_digest`) + rate limiter | Yes | `_RateLimiter` 88% (8/9) | VERIFIED |
| FIFO → All tools | File perms `0o600` + JSON validation + required fields check | Yes (contract tests) | N/A (local-only) | VERIFIED |
| Any → Structured memory | Parameterized SQL + entity normalization | Yes | structured_memory 80.3% (100% effective) | VERIFIED |

## Critical Path Verification

### 1. External text → Shell execution
**Status:** VERIFIED
- `asyncio.create_subprocess_shell(command, env=_safe_env(), start_new_session=True)` (shell.py:42-48)
- `_safe_env()` filters `LUCYD_*` prefixes and `_KEY/_TOKEN/_SECRET/_PASSWORD/_CREDENTIALS/_ID/_CODE/_PASS` suffixes (shell.py:13-14, 23-32)
- Timeout: configurable, capped at `_MAX_TIMEOUT=600` (shell.py:39)
- Kill: `os.killpg(proc.pid, SIGKILL)` on process group (shell.py:55)
- No command deny-list — intentional. Security model: LLM is autonomous agent; tool-level boundaries enforce env isolation, not command restriction.
- Tests: `test_shell_security.py` — `TestSafeEnv` (9 tests), `TestExecTimeout` (2 tests)
- Mutation: `_safe_env` 88% (7/8, 1 survivor: P-004 iteration order — dict position, not a bypass)

### 2. External text → File read/write
**Status:** VERIFIED
- `_check_path()` calls `Path(file_path).expanduser().resolve()` — follows symlinks, normalizes `..` (filesystem.py:20)
- Prefix match against `_PATH_ALLOW` list (filesystem.py:25-27)
- Fail-closed: empty allowlist returns "filesystem access denied" (filesystem.py:23-24)
- Tests: `test_filesystem_security.py` — `TestCheckPath` (10 tests)
- Mutation: `_check_path` 100% (10/10)

### 3. External text → Web requests (SSRF)
**Status:** VERIFIED
- `_validate_url()`: scheme check (`http`/`https` only), hostname resolution, `_is_private_ip()` check on ALL A records (web.py:56-85)
- `_is_private_ip()`: handles octal/hex/decimal via `socket.inet_aton()` fallback (web.py:38-53). Checks `is_private`, `is_loopback`, `is_reserved`, `is_link_local`. Fail-closed: unknown format → `True` (blocked).
- `_SafeRedirectHandler`: validates every redirect hop through `_validate_url()` (web.py:88-95)
- Known limitation: DNS rebinding — validation at resolution time, not connection time. LOW risk behind Cloudflare Tunnel. TODO comment in source (web.py:71-74).
- Tests: `test_web_security.py` — `TestValidateUrl`, `TestIsPrivateIp`, `TestSafeRedirectHandler`
- Mutation: `_validate_url` 86%, `_is_private_ip` 82%, `_SafeRedirectHandler` 80%

### 4. External text → Sub-agent spawning
**Status:** VERIFIED
- `_SUBAGENT_DENY = {"sessions_spawn", "tts", "load_skill", "react", "schedule_message"}` (agents.py:22)
- Deny-list applied unconditionally — even when tools are explicitly listed (agents.py:62)
- Model lookup: `_providers.get(model)` — only pre-configured models (agents.py:55-57)
- `max_turns` not exposed in tool schema — hardcoded default 10 (agents.py:39)
- Recursive spawning blocked: `sessions_spawn` in deny-list
- Tests: `test_agents.py` — deny-list tests
- Mutation: deny-list 100%

### 5. External text → Message sending
**Status:** VERIFIED
- `_resolve_target()` (telegram.py:325-348): case-insensitive contact name lookup → `self._contacts.get(target.lower())`
- Numeric strings bypass contact dict: `int(target)` used directly as chat_id (telegram.py:340). This is by design for group chat IDs (negative integers like `-100123456`). Mitigated by: (1) Telegram requires users to have started a conversation with the bot, (2) self-send blocked at line 343.
- Attachment paths validated via `_check_path()` (messaging.py:31-34)
- Tests: `test_telegram_channel.py` — 7 `_resolve_target` tests including numeric, self-send, unknown contact

### 6. HTTP API → All capabilities
**Status:** VERIFIED
- Bearer token auth: `hmac.compare_digest(auth[7:], self.auth_token)` — timing-safe (http_api.py:95)
- Rate limiting: `_RateLimiter` with sliding window (http_api.py:27-40). 30/min general, 60/min status.
- **Configuration note:** If `auth_token` is empty (`not self.auth_token`), auth middleware is bypassed for ALL routes (http_api.py:91-92). Intentional for health-check-only deployments. HTTP binds to `127.0.0.1` by default, so external access requires explicit port forwarding.
- Tests: `test_http_api.py` — auth, rate limiting, endpoint tests
- Mutation: `_RateLimiter.check` 88% (8/9)

### 7. Attachments → File system
**Status:** VERIFIED
- Telegram downloads: `local_path = self.download_dir / f"{int(time.time())}_{filename}"` (telegram.py:310)
- Timestamp prefix prevents path traversal: `1234567890_../evil` creates a literal directory name `1234567890_..`, NOT directory traversal (non-existent intermediate dir → write fails).
- For photos: `filename` from `Path(file_path).name` (Telegram API-controlled, not user-controlled)
- For documents: `filename` from `doc.get("file_name", "")` (user-controlled, but timestamp prefix protects)
- Image size: `img_path.stat().st_size > max_image_bytes` checked BEFORE `read_bytes()` (lucyd.py:480)
- Outbound attachment paths validated via `_check_path()` (messaging.py:31-34)

### 8. External text → Memory poisoning
**Status:** VERIFIED (accepted risk)
- Session content: JSONL uses `json.dumps()`/`json.loads()` round-trip — control characters escaped, no injection vector
- Structured memory (v2): consolidation extracts facts via LLM from sessions — attacker-controlled text CAN become facts
- **Verification that fact.value never reaches tool inputs:**
  - `fact['value']` used only in string formatting for recall text blocks (memory.py:311, 318) → injected as system prompt context
  - Entity names used only in parameterized SQL `WHERE entity = ?` clauses
  - `resolve_entity()` output used only for query normalization in parameterized SQL
  - No code path uses structured data as tool arguments, file paths, shell commands, or URLs
- Accepted risk: facts influence LLM reasoning (behavioral manipulation), but tool-level boundaries are code-enforced and unaffected

### 9. Config/skill files → Behavior modification
**Status:** VERIFIED
- Skills are text-only (skills.py:128: `"body": body.strip()`) — injected into system prompt, never executed as code
- Frontmatter parser (skills.py:16-90): custom key-value parser — `partition(":")`, no eval, no exec, no PyYAML
- TOML: stdlib `tomllib` (data-only, no code execution)
- Config values consumed as typed properties — no dynamic dispatch from config strings

### 10. Dispatch safety
**Status:** VERIFIED
- `ToolRegistry.execute()`: `self._tools[name]["function"]` — pure dict key lookup (tools/__init__.py:56-59)
- Unknown tool: returns `"Error: Unknown tool '{name}'"` (tools/__init__.py:57)
- No `getattr()` on user input in dispatch paths. All `getattr()` in codebase on Config objects with hardcoded attribute names + defaults (consolidation.py:421-430, memory.py:488-496)
- No `__import__()`, `importlib`, `eval()`, `exec()` in production code (only in test helpers)
- Tool functions pre-registered at startup; registry is immutable after `_init_tools()`

### 11. Dependency supply chain
**Status:** VERIFIED
- `pip-audit` run against 67 installed packages
- **2 CVEs found in `pip==25.1.1`:**
  - CVE-2025-8869: tar extraction symlink bypass (fix: pip 25.3)
  - CVE-2026-1703: wheel extraction path traversal (fix: pip 26.0)
  - Risk: dev-time only — Lucy does not install packages at runtime. Python 3.13 implements PEP 706 which mitigates.
- **0 CVEs in runtime dependencies:** anthropic 0.81.0, openai 2.21.0, httpx 0.28.1, aiohttp 3.13.3, all clean
- No new runtime dependencies added for vision/STT (httpx already present, ffmpeg is system binary)

## Vulnerabilities Found

None at CRITICAL or HIGH severity.

| # | Path | Severity | Status | Details |
|---|------|----------|--------|---------|
| — | — | — | — | No vulnerabilities found |

## Bypass Analysis

| Technique | Applicable? | Handled? | Details |
|-----------|------------|----------|---------|
| Path traversal (`../`, symlinks) | Yes | Yes | `Path.resolve()` normalizes all traversal and symlinks; allowlist prefix check |
| Path traversal (URL encoding) | No | N/A | Paths from LLM as plain strings, not URL-decoded |
| Path traversal (attachment filenames) | Low | Yes | Timestamp prefix creates non-existent intermediate dir; Telegram sanitizes `file_path` |
| SSRF encoding (octal/hex/decimal IP) | Yes | Yes | `socket.inet_aton()` normalizes (web.py:49) |
| SSRF (private IP ranges) | Yes | Yes | All `ipaddress` classification checks (web.py:51) |
| SSRF (redirects) | Yes | Yes | `_SafeRedirectHandler` validates each hop (web.py:91-94) |
| SSRF (DNS rebinding) | Yes | Partial | Validated at resolution, not connection. LOW risk behind tunnel. Documented TODO. |
| Command injection (subprocess) | No | N/A | `tool_exec` takes full command string by design; `_transcribe_local` uses list args |
| Env var leakage | Yes | Yes | `_safe_env()` filters by prefix/suffix (shell.py:13-14) |
| Memory poisoning (structured) | Yes | Accepted | Facts are context-only text; never reach tool inputs or dispatch paths |
| STT response poisoning | Low | Accepted | Transcription → session text. Same trust model as user messages. |
| Resource exhaustion (API cost) | Partial | Partial | `max_cost` circuit breaker in agentic loop; HTTP rate limiting; max_turns cap |
| Resource exhaustion (ffmpeg) | Low | Yes | `timeout=ffmpeg_timeout` (default 30s); temp file cleaned in `finally` |
| Resource exhaustion (image size) | Low | Yes | `stat().st_size` checked BEFORE `read_bytes()` |
| Dynamic dispatch injection | No | N/A | Dict lookup only; no reflection, eval, or exec |
| Supply chain (dep CVEs) | No | Yes | `pip-audit` clean for runtime deps; pip itself has 2 CVEs (dev-time only) |
| Skill prompt injection | Partial | Accepted | Skills are admin-managed local files; no external skill loading |
| TTS voice_id interpolation | Low | Partial | `voice_id` in f-string URL to ElevenLabs; bounded to that API domain |

## Boundary Verification Summary

| Boundary | Exists | Tested | Mutation Verified | Fails Closed |
|----------|--------|--------|-------------------|-------------|
| `_check_path()` (filesystem.py:17) | Yes | Yes (10 tests) | 100% (10/10) | Yes (empty allowlist → deny all) |
| `_safe_env()` (shell.py:23) | Yes | Yes (9 tests) | 88% (7/8, P-004 survivor) | Yes (pattern match → filter) |
| `_validate_url()` (web.py:56) | Yes | Yes | 86% | Yes (unknown format → block) |
| `_is_private_ip()` (web.py:38) | Yes | Yes | 82% | Yes (exception → True → block) |
| `_SafeRedirectHandler` (web.py:88) | Yes | Yes | 80% | Yes (invalid → URLError) |
| `_SUBAGENT_DENY` (agents.py:22) | Yes | Yes | 100% | Yes (always applied) |
| HTTP auth middleware (http_api.py:89) | Yes | Yes | 88% (RateLimiter) | Yes (mismatch → 401) |
| Telegram `allow_from` (telegram.py:174) | Yes | Yes | N/A | Yes (not in set → skip) |
| Self-send prevention (telegram.py:343) | Yes | Yes (3 tests) | N/A | Yes (bot ID match → ValueError) |
| `ALLOWED_REACTIONS` (telegram.py:33) | Yes | Yes | N/A | Yes (not in set → ValueError) |
| Parameterized SQL (structured_memory.py) | Yes | Yes | 80.3% (100% effective) | N/A (injection impossible) |
| FIFO permissions (lucyd.py:75) | Yes | No (local-only) | N/A | Yes (`0o600` owner-only) |
| ffmpeg list args (lucyd.py:862) | Yes | Yes (7 STT tests) | N/A | Yes (no shell interpretation) |
| Image size limit (lucyd.py:480) | Yes | Yes (1 test) | N/A | Yes (over-limit → text fallback) |

## Recommendations

1. **pip update** — Update pip to 26.0+ to close CVE-2025-8869 and CVE-2026-1703 (LOW — dev-time only, mitigated by PEP 706).
2. **DNS rebinding** — If deployment moves from Cloudflare Tunnel to direct exposure, implement connection-time IP validation in `_validate_url()` (LOW — current deployment is tunneled).
3. **HTTP token enforcement** — Consider refusing to start HTTP API if `LUCYD_HTTP_TOKEN` is not set when `[http] enabled = true` (LOW — HTTP binds to localhost by default).

## Confidence

Overall confidence: 97%

- Critical paths (shell, filesystem, web, agents): 98% — all boundaries verified, mutation-tested, code traced
- New STT paths: 97% — controlled input (Telegram download), list args for subprocess, timeouts, temp cleanup
- New vision paths: 97% — size limit before I/O, transient injection with restore in error/success, controlled content_type
- HTTP API auth: 97% — timing-safe comparison, rate limiting, localhost binding
- Structured memory poisoning: 95% — verified fact values never reach tool inputs; accepted risk is LLM reasoning manipulation
- Dispatch safety: 99% — all dispatch via dict lookup, no dynamic execution patterns
- DNS rebinding: 90% — partial mitigation; acceptable for current tunneled deployment
- Supply chain: 98% — pip-audit clean for runtime deps, pip CVEs are dev-time only
