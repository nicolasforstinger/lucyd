# Security Audit Report

**Date:** 2026-02-26
**Audit Cycle:** 9
**EXIT STATUS:** PASS

## Threat Model

Lucyd is an autonomous agent processing external data from Telegram messages, HTTP API requests, FIFO commands, and n8n webhook payloads. Data flows through the agentic loop (LLM) and can trigger tool execution: shell commands, filesystem access, web requests, sub-agent spawning, and message sending. The security model: **the LLM is UNTRUSTED for security decisions.** All security boundaries are code-enforced at the tool level.

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-003 (unchecked filesystem write) | CLEAN | All 19 tool functions verified. Path-accepting tools call `_check_path()` before I/O. New functions (`build_session_info`, `read_history_events`) use parameterized SQL and glob patterns only. |
| P-009 (capability table stale) | CLEAN | Full capability table re-derived from source. 19 tools across 12 modules. No new tools since Cycle 8. |
| P-012 (misclassified static) | CLEAN | Entity aliases correctly classified as LLM-extracted (Stage 5 confirmed). |
| P-018 (resource exhaustion) | 2 NOTED | `asyncio.Queue` unbounded (lucyd.py:285). Mitigated by rate limiter. Unchanged. |

## Input Sources

| Source | Protocol | Authentication | Risk Level |
|--------|----------|---------------|------------|
| Telegram | Bot API long polling | `allow_from` user ID allowlist | HIGH |
| HTTP /chat, /notify | REST POST | Bearer token (hmac.compare_digest) | HIGH |
| HTTP /sessions/reset | REST POST | Bearer token | HIGH (new) |
| HTTP /status | REST GET | None (health check exempt) | LOW |
| HTTP /sessions, /cost, /monitor | REST GET | Bearer token | LOW |
| HTTP /sessions/{id}/history | REST GET | Bearer token | LOW (new) |
| FIFO | Named pipe, JSON/line | Unix file permissions (0o600) | LOW |
| CLI | stdin/stdout | Local terminal access | LOW |
| Config files | TOML (startup only) | Filesystem permissions | LOW |
| Skill files | Markdown (startup + SIGUSR1) | Filesystem permissions | MEDIUM |
| Plugin directory | Python (startup) | Filesystem permissions | CRITICAL |
| Memory DB | SQLite (WAL mode) | Filesystem permissions | MEDIUM |

New input sources: `/api/v1/sessions/reset` (POST), `/api/v1/monitor` (GET), `/api/v1/sessions/{id}/history` (GET).

## Capabilities

| # | Tool | Module | Danger | Boundaries |
|---|------|--------|--------|------------|
| 1 | exec | shell.py | CRITICAL | `_safe_env()`, timeout (600s), `start_new_session=True` |
| 2 | read | filesystem.py | MEDIUM | `_check_path()` allowlist, `Path.resolve()`, `os.sep` prefix guard |
| 3 | write | filesystem.py | MEDIUM | `_check_path()` allowlist, `Path.resolve()`, `os.sep` prefix guard |
| 4 | edit | filesystem.py | MEDIUM | `_check_path()` allowlist, `Path.resolve()`, `os.sep` prefix guard |
| 5 | sessions_spawn | agents.py | HIGH | `_subagent_deny` deny-list, max_turns, timeout |
| 6 | web_fetch | web.py | MEDIUM | `_validate_url()`, `_is_private_ip()`, IP pinning, redirect validation |
| 7 | message | messaging.py | MEDIUM | `_resolve_target()`, self-send block, `_check_path()` attachments |
| 8 | web_search | web.py | LOW | Hardcoded Brave API URL, API key gated |
| 9 | tts | tts.py | MEDIUM | `_check_path()` on output_file, API key gated, tempfile fallback |
| 10 | load_skill | skills_tool.py | LOW | Dict key lookup (text-only) |
| 11 | memory_search | memory_tools.py | LOW | Read-only (SQLite + vector) |
| 12 | memory_get | memory_tools.py | LOW | Read-only (SQL parameterized) |
| 13 | memory_write | structured_memory.py | LOW | Parameterized SQL, entity normalization |
| 14 | memory_forget | structured_memory.py | LOW | Parameterized SQL, soft delete |
| 15 | commitment_update | structured_memory.py | LOW | Parameterized SQL, enum-restricted status |
| 16 | schedule_message | scheduling.py | LOW | Max 50 pending, max 24h delay |
| 17 | list_scheduled | scheduling.py | LOW | Read-only |
| 18 | session_status | status.py | LOW | Read-only |
| 19 | react | messaging.py | LOW | ALLOWED_REACTIONS emoji set |

No new tools added. Capability table unchanged from Cycle 8.

## Path Matrix

| Input -> Capability | Boundary | Tested? | Mutation Verified? | Status |
|-------------------|----------|---------|-------------------|--------|
| Telegram -> exec | `_safe_env()`, timeout | Yes | Yes (100% kill) | VERIFIED |
| Telegram -> read/write/edit | `_check_path()` allowlist | Yes | Yes (100% kill) | VERIFIED |
| Telegram -> web_fetch | `_validate_url()` + SSRF stack | Yes | Yes (equivalent survivors only) | VERIFIED |
| Telegram -> sessions_spawn | `_subagent_deny` deny-list | Yes | Yes (100% kill) | VERIFIED |
| Telegram -> message | `_resolve_target()`, `_check_path()` attachments | Yes | Yes | VERIFIED |
| Telegram -> tts | `_check_path()` on output_file | Yes | Yes | VERIFIED |
| HTTP API -> all tools | Bearer token (hmac.compare_digest) + rate limiting + 10 MiB body | Yes | Yes (100% kill) | VERIFIED |
| HTTP -> reset | Bearer token + string validation | Yes (5 tests) | N/A | VERIFIED (new) |
| HTTP -> history | Bearer token + glob pattern (safe) | Yes (5 tests) | N/A | VERIFIED (new) |
| HTTP -> monitor | Bearer token + read-only rate limit | Yes (3 tests) | N/A | VERIFIED (new) |
| FIFO -> all tools | Unix permissions (0o600), JSON validation | Yes | N/A | VERIFIED |
| Attachments -> filesystem | `Path.name` filename sanitization (both channels) | Yes | N/A | VERIFIED |

## Critical Path Verification

### 1. External text -> Shell execution
**Status:** VERIFIED (accepted risk — exec is unrestricted by design)
**Boundary:** `_safe_env()` filters `LUCYD_*` prefix and secret suffixes. Timeout 600s. Process group isolation.
**Tests:** test_shell_security.py — 57 tests. `_safe_env()` 100% mutation kill rate.

### 2. External text -> File read/write
**Status:** VERIFIED
**Boundary:** `_check_path()` — `Path.resolve()` + prefix allowlist + `os.sep` trailing separator guard.
**Tests:** test_filesystem.py — 39 tests. 100% mutation kill rate.

### 3. External text -> Web requests (SSRF)
**Status:** VERIFIED
**Boundary:** Scheme whitelist, DNS resolution, `_is_private_ip()` with octal/hex normalization, IP pinning, redirect-hop validation.
**Tests:** test_web_security.py — 47 tests.

### 4. External text -> Sub-agent spawning
**Status:** VERIFIED
**Boundary:** `sessions_spawn` in own deny-list. Configurable. Sub-agents inherit tool boundaries.
**Tests:** 14 tests on deny-list, tool scoping, model resolution.

### 5. External text -> Message sending
**Status:** VERIFIED
**Boundary:** `_resolve_target()` contacts dict lookup. Self-send blocked. Attachment paths validated.
**Tests:** 5 tests on path validation, self-send blocking.

### 6. HTTP API -> All capabilities
**Status:** VERIFIED
**Boundary:** `hmac.compare_digest()` timing-safe. No-token -> 503. Rate limiting. 10 MiB body cap.
**Tests:** 22 auth tests + 2 rate limit tests.

### 7. HTTP -> Reset endpoint (new)
**Status:** VERIFIED
**Boundary:** Bearer token required. Input validated (`isinstance(target, str)`, non-empty). `_reset_session()` uses index-based lookup only — no file path construction from target. UUID format validated via compiled regex. Unknown targets return graceful error.
**Tests:** 5 tests (reset_all, reset_by_contact, requires_auth, invalid_body, no_callback).

### 8. HTTP -> History endpoint (new)
**Status:** VERIFIED
**Boundary:** Bearer token required. Session ID from URL path used in glob pattern `{session_id}.????-??-??.jsonl` — glob treats `..` as literal characters, not directory traversal. Date suffix `.????-??-??.jsonl` constrains matches. Archive searched in `.archive/` subdirectory with same pattern.
**Tests:** 5 tests (returns_events, full_param, requires_auth, no_callback, rate_limited_as_read_only).

### 9. HTTP -> Monitor endpoint (new)
**Status:** VERIFIED
**Boundary:** Bearer token required. In `_READ_ONLY_PATHS` for rate limiting. No user input parameters. Returns daemon state dict from callback.
**Tests:** 3 tests (returns_data, no_callback, rate_limited_as_read_only).

### 10. Agent identity injection
**Status:** VERIFIED
**Boundary:** `agent_name` sourced from TOML config (admin-controlled, not user input). Injected into JSON body (safe — aiohttp JSON serialization) and `X-Lucyd-Agent` header. Config-sourced values don't contain control characters.
**Tests:** 5 tests (status, sessions, notify include agent; absent when empty; error responses excluded).

### 11. Attachments -> File system
**Status:** VERIFIED
**Boundary:** Both channels sanitize via `Path(filename).name`.

### 12. Memory poisoning
**Status:** ACCEPTED RISK (unchanged)
**Analysis:** Facts are text context only — never reach tool arguments, file paths, shell commands, or network requests. All SQL parameterized.

### 13. Supply chain
**Status:** CLEAN
pip-audit: 0 runtime CVEs. 2 CVEs in pip 25.1.1 (dev tool only).

## Vulnerabilities Found

| # | Path | Severity | Status | Description |
|---|------|----------|--------|-------------|
| — | — | — | — | No new vulnerabilities found |

Previously resolved findings (prefix match, filename sanitization) remain resolved.

## Bypass Analysis

| Technique | Applicable? | Handled? | Details |
|-----------|------------|----------|---------|
| Path traversal (`../`) | Yes | Yes | `Path.resolve()` before prefix check |
| Path prefix ambiguity | Yes | Yes | `os.sep` guard at filesystem.py:35 |
| Symlink escape | Yes | Yes | `Path.resolve()` follows symlinks to real path |
| SSRF encoding (octal/hex/decimal) | Yes | Yes | `inet_aton()` normalization |
| SSRF IPv6 (`[::1]`) | Yes | Yes | `ipaddress.ip_address()` handles IPv6 |
| SSRF DNS rebinding | Yes | Yes | IP pinning via custom handlers |
| SSRF redirect chain | Yes | Yes | `_SafeRedirectHandler` validates each hop |
| Command injection | N/A | N/A | `exec` takes entire command by design |
| Env var leakage | Yes | Yes | `_safe_env()` filters by prefix + suffix |
| Session poisoning | Yes | Accepted | Tool-level boundaries sufficient |
| Structured memory poisoning | Yes | Verified | Facts never reach tool arguments |
| Resource exhaustion (queue) | Yes | Partial | asyncio.Queue unbounded, mitigated by rate limiter |
| Dynamic dispatch injection | Yes | Yes | Dict-key lookup only |
| Supply chain (dep CVEs) | Yes | Clean | 0 runtime CVEs |
| Attachment filename traversal | Yes | Yes | Both channels use `Path.name` |
| History endpoint path traversal | Yes | Yes | Glob treats `..` as literal (new) |
| Reset endpoint abuse | Yes | Yes | Index-based lookup, no file ops (new) |
| Agent identity header injection | No | N/A | Config-sourced, not user input (new) |

## Boundary Verification Summary

| Boundary | Exists | Tested | Mutation Verified | Fails Closed |
|----------|--------|--------|-------------------|-------------|
| `_check_path()` (allowlist + `os.sep`) | Yes | Yes | Yes (100%) | Yes |
| `_safe_env()` (env filter) | Yes | Yes | Yes (100%) | Yes |
| `_safe_parse_args()` (JSON fallback) | Yes | Yes | Yes (100%) | Yes |
| `_validate_url()` (SSRF) | Yes | Yes | Yes (equivalent survivors) | Yes |
| `_is_private_ip()` (IP check) | Yes | Yes | Yes (equivalent survivors) | Yes |
| `_SafeRedirectHandler` | Yes | Yes | Yes (equivalent survivors) | Yes |
| IP pinning (DNS rebinding) | Yes | Yes | N/A | Yes |
| `_subagent_deny` (deny-list) | Yes | Yes | Yes (100%) | Yes |
| HTTP Bearer auth | Yes | Yes | Yes (100%) | Yes (503 on no-token) |
| `hmac.compare_digest` | Yes | Yes | Yes (100%) | Yes |
| HTTP rate limiting | Yes | Yes | Yes (100%) | Yes (-> 429) |
| HTTP body size cap | Yes | Yes | N/A (aiohttp) | Yes (-> 413) |
| Telegram `allow_from` | Yes | Yes | N/A | Yes |
| FIFO permissions | Yes | N/A (OS) | N/A | Yes (0o600) |
| Attachment `Path.name` sanitization | Yes | Yes | N/A | Yes |
| `_json_response()` agent identity | Yes | Yes | Yes (cosmetic survivors) | N/A |
| Reset input validation | Yes | Yes | N/A | Yes |
| History glob pattern | Yes | Yes | N/A | Yes |

## Security Test Results

| Test File | Count |
|-----------|-------|
| test_shell_security.py | 57 |
| test_web_security.py | 47 |
| test_filesystem.py | 39 |
| test_synthesis.py | 23 |
| test_http_api.py (auth/rate/identity/reset/history/monitor) | 40 |
| **Total security-focused** | **206** |

## Recommendations

1. **(Info)** Upgrade pip to fix CVE-2025-8869 and CVE-2026-1703 (dev tool only). Carried forward.
2. **(Info)** Consider `asyncio.Queue(maxsize=N)` for defense against queue-flooding. Carried forward.
3. **(Info)** Consider sanitizing `agent_name` to `[a-zA-Z0-9_-]+` before HTTP header injection (defense-in-depth — current risk negligible as value is config-sourced).

## Confidence

Overall confidence: 97%

- **CRITICAL capabilities (exec, filesystem, sub-agents):** 98%. All boundaries mutation-verified at 100% kill.
- **HIGH capabilities (web_fetch, messaging):** 97%. SSRF protection comprehensive.
- **Authentication (HTTP API):** 98%. Timing-safe. Fail-closed. All new endpoints behind auth.
- **New endpoints (reset, history, monitor):** 96%. All properly authenticated, rate-limited, input-validated. History glob pattern safe against traversal. Reset uses index-based lookup only.
- **Agent identity:** 98%. Config-sourced, not user input. JSON serialization handles escaping.
- **Indirect paths (memory poisoning):** 95%. Accepted risk unchanged.
- **Supply chain:** 98%. Zero runtime CVEs.
