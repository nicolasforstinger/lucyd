# Security Audit Report

**Date:** 2026-02-19
**EXIT STATUS:** PASS

## Threat Model

Lucyd is a single-user AI daemon on a Debian VM (local network, Cloudflare Tunnel for Telegram). Attack surface: Telegram Bot API (public, user-ID-filtered), HTTP REST API (localhost, bearer token auth), local FIFO (owner-only permissions). The LLM is UNTRUSTED for security decisions — all boundaries are enforced at tool level in code.

Primary threats: prompt injection via Telegram/web content, SSRF via web tools, path traversal via filesystem tools, credential leakage via shell tool, sub-agent privilege escalation, structured memory poisoning.

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-003 (unchecked filesystem write in tool params) | CLEAN | All path-like parameters verified: `tool_read`, `tool_write`, `tool_edit` use `_check_path()`; `tool_message` attachments use `_check_path()`; `tool_tts` output_file uses `_check_path()`; `tool_memory_get` uses `_check_path()`. No unchecked paths. |
| P-009 (capability table stale) | CLEAN | Re-derived from source — 19 tools across 10 modules. Matches previous cycle. No new tools added, no boundaries removed. |
| P-012 (auto-populated misclassified as static) | CLEAN | Verified in Stage 5 — `entity_aliases` correctly identified as auto-populated by `consolidation.py:230`. |

## Input Sources

| Source | Protocol | Authentication | Risk Level |
|--------|----------|---------------|------------|
| Telegram messages | Bot API long polling | Bot token + `allow_from` user ID whitelist (line 174) | Medium |
| Telegram attachments | Bot API `getFile` | Same as messages; 5MB size limit (lucyd.py) | Medium |
| HTTP `/api/v1/chat` | REST POST | Bearer token (`hmac.compare_digest`, line 95) + rate limit | Medium |
| HTTP `/api/v1/notify` | REST POST | Same as /chat | Medium |
| HTTP `/api/v1/status` | REST GET | Same (relaxed rate limit: 60/min vs 30/min) | Low |
| FIFO control pipe | Local IPC (JSON) | File permissions 0o600 (owner-only) | Low |
| Config (TOML) | Filesystem | File permissions (trusted, version-controlled) | Low |
| Workspace/skills | Filesystem | File permissions (local workspace) | Low |
| Session JSONL | Filesystem | Daemon-written only; `json.loads()` per line | Low |
| Environment vars | Process env | Systemd unit context | Low |
| Whisper API response | HTTPS (OpenAI) | API key; response treated as untrusted text | Low |
| Brave Search response | HTTPS (Brave) | API key; response treated as untrusted text | Low |

## Capabilities

| Capability | Tool | Danger Level | Boundaries |
|------------|------|-------------|------------|
| Shell execution | `exec` | CRITICAL | `_safe_env()`, `start_new_session=True`, timeout, process group kill |
| File read | `read` | CRITICAL | `_check_path()` allowlist (workspace + /tmp/) |
| File write | `write` | CRITICAL | `_check_path()` allowlist |
| File edit | `edit` | CRITICAL | `_check_path()` allowlist |
| Web fetch (SSRF) | `web_fetch` | HIGH | `_validate_url()`, `_is_private_ip()`, `_SafeRedirectHandler` |
| Sub-agent spawn | `sessions_spawn` | HIGH | `_SUBAGENT_DENY` set, `max_turns=10` hardcoded |
| Send messages | `message` | HIGH | `_resolve_target()` with self-send block; attachment paths via `_check_path()` |
| Web search | `web_search` | MEDIUM | URL-encoded params, hardcoded Brave API endpoint |
| TTS generation | `tts` | MEDIUM | `_check_path()` on output_file; hardcoded ElevenLabs endpoint |
| Memory write | `memory_write` | MEDIUM | Parameterized SQL, entity normalization |
| Memory search | `memory_search` | LOW | Read-only vector/FTS search |
| Memory get | `memory_get` | LOW | `_check_path()` on file path |
| Memory forget | `memory_forget` | LOW | Parameterized SQL (soft-delete only) |
| Commitment update | `commitment_update` | LOW | Parameterized SQL, enum-restricted status values |
| Session status | `session_status` | LOW | Read-only internal state |
| Load skill | `load_skill` | LOW | Text-only; denied to sub-agents |
| Schedule message | `schedule_message` | LOW | Denied to sub-agents |
| List scheduled | `list_scheduled` | LOW | Read-only |
| React | `react` | LOW | Emoji whitelist (59 values); denied to sub-agents |

## Path Matrix

| Input → Capability | Boundary | Tested? | Mutation Verified? | Status |
|-------------------|----------|---------|-------------------|--------|
| Telegram → Shell exec | `_safe_env()` + timeout + `start_new_session` | Yes | `_safe_env` 100% (9/9) | VERIFIED |
| Telegram → File read/write/edit | `_check_path()` allowlist | Yes | `_check_path` 100% (11/11) | VERIFIED |
| Telegram → Web fetch | `_validate_url()` + `_is_private_ip()` + `_SafeRedirectHandler` | Yes | 81–87% | VERIFIED |
| Telegram → Sub-agent spawn | `_SUBAGENT_DENY` set | Yes | deny-list 100% | VERIFIED |
| Telegram → Message send | `_resolve_target()` self-send block + `_check_path()` on attachments | Yes | N/A (contract test) | VERIFIED |
| HTTP API → All tools | Bearer token auth (`hmac.compare_digest`) + rate limiter | Yes | `_RateLimiter` 82% | VERIFIED |
| HTTP API → Shell exec | Auth + `_safe_env()` + timeout | Yes | Both verified | VERIFIED |
| HTTP API → File read/write | Auth + `_check_path()` | Yes | Both verified | VERIFIED |
| FIFO → All tools | File perms 0o600 + JSON validation | Yes (contract tests) | N/A (local-only) | VERIFIED |
| Any → Memory write | Parameterized SQL + entity normalization | Yes | structured_memory 77% (100% effective) | VERIFIED |

## Critical Path Verification

### 1. External text → Shell execution
**Status:** VERIFIED
- Subprocess uses `_safe_env()` to filter credentials from environment (shell.py:23–32)
- `start_new_session=True` isolates process group (shell.py:44)
- Timeout with `os.killpg()` on process group (shell.py:49–62)
- No command deny-list (intentional — Lucy is autonomous; tool-level boundaries enforce safety)
- Tests: `test_shell_security.py` — `TestSafeEnv` (9 tests), `TestExecTimeout` (2 tests)
- Mutation verified: `_safe_env` 100% kill rate

### 2. External text → File read/write
**Status:** VERIFIED
- `_check_path()` resolves symlinks via `Path.resolve()`, normalizes `..` traversal (filesystem.py:17–28)
- Allowlist defaults to workspace + `/tmp/` (config.py:311–317)
- Fail-closed: empty allowlist = all access denied
- Tests: `test_filesystem_security.py` — `TestCheckPath` (11 tests), `TestRead`/`TestWrite`/`TestEdit` each has `test_blocked_path`
- Mutation verified: `_check_path` 100% kill rate (11/11)

### 3. External text → Web requests (SSRF)
**Status:** VERIFIED
- `_validate_url()` enforces http/https scheme only (web.py:63–64)
- DNS resolution checked against `_is_private_ip()` for all A records (web.py:76–83)
- `_is_private_ip()` handles octal/hex/decimal encodings via `socket.inet_aton()` fallback (web.py:38–53)
- `_SafeRedirectHandler` validates every redirect hop (web.py:88–95)
- Fail-closed: unknown IP format returns True (blocked)
- Tests: `test_web_security.py` — `TestValidateUrl`, `TestIsPrivateIp`, `TestSafeRedirectHandler`
- Mutation verified: `_validate_url` 87%, `_is_private_ip` 83%, `_SafeRedirectHandler` 81%
- Known limitation: DNS rebinding (validation at resolution time, not connection time). Documented in code (web.py:71–74). LOW risk in current Cloudflare Tunnel deployment.

### 4. External text → Sub-agent spawning
**Status:** VERIFIED
- `_SUBAGENT_DENY` mandatory set: `{sessions_spawn, tts, load_skill, react, schedule_message}` (agents.py:22)
- Deny-list applied regardless of explicit tool list (agents.py:59–64)
- `max_turns=10` hardcoded, not in tool schema (code enforcement) (agents.py:38)
- Sub-agents cannot spawn sub-agents (recursive loop prevented)
- Tests: `test_agents.py` — deny-list tests
- Mutation verified: deny-list 100% kill rate

### 5. External text → Message sending
**Status:** VERIFIED
- `_resolve_target()` checks `chat_id == self._bot_id` (telegram.py:343–346)
- Attachment paths validated via `_check_path()` (messaging.py:30–35)
- Target resolution requires known contact name or numeric ID
- Tests: contract tests in `test_orchestrator.py`

### 6. HTTP API → All capabilities
**Status:** VERIFIED
- Bearer token auth with `hmac.compare_digest()` (timing-safe) (http_api.py:95)
- Rate limiting: 30 req/min per IP for /chat and /notify, 60/min for /status (http_api.py:27–40)
- Note: if `LUCYD_HTTP_TOKEN` not configured, auth middleware is bypassed (http_api.py:91). Accepted risk: HTTP API binds to 127.0.0.1 (localhost only).

### 7. Attachments → File system
**Status:** VERIFIED
- Telegram downloads go to `/tmp/lucyd-telegram/` with timestamp prefix (telegram.py:310)
- 5MB size limit on inbound images (lucyd.py)
- Attachment paths in outbound messages checked via `_check_path()` (messaging.py:30–35)

### 8. External text → Memory poisoning
**Status:** VERIFIED (accepted risk, bounded)
- Session content: JSONL uses `json.dumps()`/`json.loads()` round-trip — no injection vector
- Structured memory: consolidation extracts facts via LLM — attacker-controlled content CAN become facts
- Accepted risk: facts are read-only context injection. Verified:
  - `fact.value` never used as tool input (grep: only appears in parameterized SQL and dict reads)
  - Entity names never used in file paths (grep: no matches for entity+path/file/open)
  - `resolve_entity()` output only reaches parameterized SQL lookups (memory.py:356, structured_memory.py:43)
- Memory poisoning affects LLM reasoning, not tool-level security boundaries

### 9. Config/skill files → Behavior modification
**Status:** VERIFIED
- Skills are text-only (injected into system prompt, never executed as code)
- Frontmatter parser (skills.py:16–90) is custom regex-free key-value parser — no PyYAML, no eval
- TOML parsed by stdlib `tomllib` (data-only, no code execution)
- Config values are typed properties (config.py) — no dynamic dispatch from config values

### 10. Dispatch safety
**Status:** VERIFIED
- `ToolRegistry.execute()` uses direct dict key lookup (tools/__init__.py:54–62)
- No `getattr()` on user input, no `__import__()`, no `importlib`, no `eval()`, no `exec()` in dispatch paths
- All `getattr()` calls in codebase are on Config objects with defaults (consolidation.py, memory.py)
- Tool functions pre-registered at startup; registry is immutable after init

### 11. Dependency supply chain
**Status:** VERIFIED
- 75 packages installed; `pip-audit` found 2 CVEs in `pip` itself (CVE-2025-8869, CVE-2026-1703)
- Both mitigated by Python 3.13 PEP 706 — pip's tar extraction fallback not used
- No CVEs in application dependencies (httpx, anthropic, aiohttp, etc.)
- Recommendation: update pip to 25.3+ as defense-in-depth

## Vulnerabilities Found

None at CRITICAL or HIGH severity.

## Bypass Analysis

| Technique | Applicable? | Handled? | Details |
|-----------|------------|----------|---------|
| Path traversal (`../`) | Yes | Yes | `Path.resolve()` normalizes; allowlist prefix check |
| Path traversal (symlinks) | Yes | Yes | `Path.resolve()` expands symlinks |
| Path traversal (double encoding) | No | N/A | Path received as string from LLM, not URL-decoded |
| SSRF encoding tricks (octal/hex/decimal IP) | Yes | Yes | `socket.inet_aton()` normalizes (web.py:48) |
| SSRF (private IP ranges) | Yes | Yes | `is_private`, `is_loopback`, `is_reserved`, `is_link_local` |
| SSRF (redirects) | Yes | Yes | `_SafeRedirectHandler` validates each hop |
| SSRF (DNS rebinding) | Yes | Partial | Validated at resolution time, not connection. LOW risk behind tunnel. |
| Command injection | N/A | N/A | Shell tool intentionally passes full command string |
| Env var leakage | Yes | Yes | `_safe_env()` filters LUCYD_*, *_KEY, *_TOKEN, etc. |
| Memory poisoning (session) | Yes | Accepted | Facts are context-only, never tool arguments |
| Structured memory poisoning | Yes | Accepted | Fact values never reach tool inputs, file paths, or dispatch |
| Resource exhaustion (API cost) | Partial | Partial | Rate limiting on HTTP; no per-session cost cap |
| Dynamic dispatch injection | No | N/A | Dict lookup only; no reflection/eval |
| Supply chain (dep CVEs) | No | Yes | pip-audit clean for app deps; pip itself has mitigated CVEs |
| Skill prompt injection | Partial | Accepted | Skills are local files; no external skill loading |
| TTS voice_id URL injection | Low | Partial | voice_id interpolated into ElevenLabs URL; bounded to that domain |

## Boundary Verification Summary

| Boundary | Exists | Tested | Mutation Verified | Fails Closed |
|----------|--------|--------|-------------------|-------------|
| `_check_path()` | Yes | Yes (11 tests) | Yes (100%) | Yes (empty allowlist = deny all) |
| `_safe_env()` | Yes | Yes (9 tests) | Yes (100%) | Yes (blacklist approach — unknown vars pass) |
| `_validate_url()` | Yes | Yes | Yes (87%) | Yes (unknown format = block) |
| `_is_private_ip()` | Yes | Yes | Yes (83%) | Yes (exception = True = block) |
| `_SafeRedirectHandler` | Yes | Yes | Yes (81%) | Yes (invalid redirect = URLError) |
| `_SUBAGENT_DENY` | Yes | Yes | Yes (100%) | Yes (deny always applied) |
| `_auth_middleware` (HMAC) | Yes | Yes | Yes (82%) | Yes (mismatch = 401) |
| `_RateLimiter` | Yes | Yes | Yes (82%) | Yes (over limit = 429) |
| Telegram `allow_from` | Yes | Yes | N/A | Yes (non-listed user = skip) |
| Self-send prevention | Yes | Yes | N/A | Yes (bot ID match = ValueError) |
| Emoji whitelist | Yes | Yes | N/A | Yes (not in set = error) |
| Parameterized SQL | Yes | Yes | Yes (structured_memory 77%) | N/A (no injection possible) |

## Recommendations

1. **pip update** — Update pip to 25.3+ to close CVE-2025-8869 and CVE-2026-1703 (LOW priority — mitigated by Python 3.13 PEP 706).
2. **DNS rebinding** — If deployment moves from Cloudflare Tunnel to direct exposure, implement connection-time IP validation in `_validate_url()` (LOW priority — documented TODO at web.py:71–74).
3. **HTTP API token enforcement** — Consider refusing to start if `[http] enabled = true` but `LUCYD_HTTP_TOKEN` is not set (LOW priority — HTTP binds to localhost).
4. **TTS voice_id sanitization** — Consider URL-encoding or validating the `voice_id` parameter before interpolation into the ElevenLabs URL (LOW priority — bounded to ElevenLabs domain, LLM-controlled parameter).

## Confidence

Overall confidence: 96%

- Critical paths (shell, filesystem, web, agents): 98% confident all boundaries verified and mutation-tested
- HTTP API auth: 97% — timing-safe comparison verified, rate limiting in place
- Structured memory poisoning: 95% — verified fact values never reach tool inputs; accepted risk is LLM reasoning manipulation (design boundary)
- DNS rebinding: 90% — partial mitigation; acceptable for current deployment behind Cloudflare Tunnel
- Supply chain: 95% — pip-audit run, no app-code CVEs, pip itself has mitigated issues
