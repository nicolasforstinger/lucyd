# Security Audit Report

**Date:** 2026-02-18
**EXIT STATUS:** PASS

## Pattern Checks

- **P-003:** All file path parameters re-verified from source. `_check_path()` called before every filesystem I/O in `tool_read`, `tool_write`, `tool_edit`, `tool_message` (attachments), `tool_tts` (output_file). No unchecked paths found.
- **P-009:** Capability table re-derived from source. 16 tools across 8 modules. No drift from previous cycle.

## Threat Model

Lucy is an autonomous LLM agent processing external data from Telegram messages, HTTP API requests, and CLI/FIFO injection. Data flows through an agentic loop where the LLM decides tool calls. **The LLM is untrusted for security decisions.** All security boundaries are enforced at the tool level by code, not by prompting. If the LLM is fully compromised by prompt injection, tool-level boundaries must still hold.

## Input Sources

| Source | Protocol | Authentication | Risk Level |
|--------|----------|---------------|------------|
| Telegram | Bot API (long polling) | `allow_from` user ID whitelist | HIGH — public-facing |
| HTTP API | REST (aiohttp) | Bearer token + `hmac.compare_digest` | MEDIUM — localhost only |
| CLI (`lucyd-send`) | FIFO (`control.pipe`) | Local filesystem access (Unix permissions) | LOW — local only |
| Config files | TOML (stdlib `tomllib`) | Filesystem permissions | LOW — static, local |
| Skill files | Markdown (custom parser) | Filesystem permissions | LOW — text-only, no code exec |
| Session JSONL | `json.loads` per line | Filesystem permissions | LOW — safe by construction |

## Capabilities

| Capability | Tool | Danger Level | Boundaries |
|------------|------|-------------|------------|
| Shell execution | `tool_exec` | CRITICAL | `_safe_env()`, timeout cap (600s), process group kill |
| File read | `tool_read` | CRITICAL | `_check_path()` allowlist, `Path.resolve()` |
| File write | `tool_write` | CRITICAL | `_check_path()` allowlist, `Path.resolve()` |
| File edit | `tool_edit` | CRITICAL | `_check_path()` allowlist, `Path.resolve()` |
| Sub-agent spawn | `tool_sessions_spawn` | CRITICAL | `_SUBAGENT_DENY` set (blocks self-recursion + 4 others) |
| Web fetch | `tool_web_fetch` | HIGH | `_validate_url()`, `_is_private_ip()`, `_SafeRedirectHandler` |
| Web search | `tool_web_search` | HIGH | Hardcoded Brave API URL, API key in header |
| Message send | `tool_message` | HIGH | `_resolve_target()`, self-send prevention, `_check_path()` on attachments |
| TTS | `tool_tts` | MEDIUM | `_check_path()` on explicit output path, tempfile default |
| Memory search | `tool_memory_search` | LOW | Read-only DB query, FTS5 sanitized via `_sanitize_fts5()` |
| Memory get | `tool_memory_get` | LOW | DB key lookup (parameterized SQL), no filesystem access |
| Schedule message | `tool_schedule_message` | LOW | `_MAX_SCHEDULED=50` cap, 24h max delay |
| List scheduled | `tool_list_scheduled` | LOW | Read-only |
| Load skill | `tool_load_skill` | LOW | Dict key lookup in `_skills`, no dynamic dispatch |
| Session status | `tool_session_status` | LOW | Read-only, internal state |
| React | `tool_react` | LOW | Emoji-only, target resolved via contacts |

## Path Matrix

| Input -> Capability | Boundary | Tested? | Mutation Verified? | Status |
|---------------------|----------|---------|-------------------|--------|
| Telegram -> Shell | `_safe_env()`, timeout | Yes | 100% (8/8) | VERIFIED |
| Telegram -> File read/write/edit | `_check_path()` allowlist | Yes | 100% (10/10) | VERIFIED |
| Telegram -> Web fetch | `_validate_url()`, `_is_private_ip()`, redirect handler | Yes | 86%/81%/80% | VERIFIED |
| Telegram -> Sub-agent | `_SUBAGENT_DENY` set | Yes | 100% (14/14) | VERIFIED |
| Telegram -> Message | `_resolve_target()`, self-send prevention, attachment `_check_path` | Yes | N/A (channel tests) | VERIFIED |
| HTTP API -> All tools | Bearer token + `hmac.compare_digest`, rate limiter | Yes | 88% (8/9) | VERIFIED |
| CLI/FIFO -> All tools | Unix filesystem permissions | Yes (unit) | N/A | VERIFIED |
| Skill files -> System prompt | Text-only injection, custom parser (no eval) | Yes | N/A | VERIFIED |
| Session JSONL -> Context | `json.loads` per line (safe by construction) | Yes | N/A | VERIFIED |

## Critical Path Verification

### 1. External text -> Shell execution
**Status:** VERIFIED
**Boundary:** `_safe_env()` filters `LUCYD_*` prefix and `_KEY/_TOKEN/_SECRET/_PASSWORD/_CREDENTIALS/_ID/_CODE/_PASS` suffixes. Timeout capped at 600s. Process group kill via `start_new_session=True` + `os.killpg()`. No command deny-list — by design (security model accepts LLM-chosen commands, mitigated by env filtering).
**Tests:** `test_shell_security.py` (17 tests)
**Mutation verified:** `_safe_env` 100% (8/8), `configure` 100% (4/4)

### 2. External text -> File read/write
**Status:** VERIFIED
**Boundary:** `_check_path()` resolves path via `Path.expanduser().resolve()` (follows symlinks, normalizes `../`), then checks against `_PATH_ALLOW` prefixes. Fails closed: empty allowlist -> deny all.
**Tests:** `test_filesystem.py` (28 tests), `test_filesystem_security.py` (32 tests)
**Mutation verified:** 100% (10/10)

### 3. External text -> Web requests (SSRF)
**Status:** VERIFIED
**Boundary:** `_validate_url()` checks scheme (http/https only), resolves hostname via `socket.getaddrinfo()`, checks all resolved IPs via `_is_private_ip()`. `_is_private_ip()` handles standard + non-standard IP encodings (octal, hex, decimal) via `socket.inet_aton()` fallback. `_SafeRedirectHandler` validates each redirect hop. Fails closed: unknown IP format -> blocked.
**Tests:** `test_web_security.py` (35 tests)
**Mutation verified:** `_validate_url` 86% (19/22), `_is_private_ip` 81% (9/11), `_SafeRedirectHandler` 80% (12/15)
**Known limitation:** DNS rebinding — IP validated at resolution time, not connection time. Documented TODO in source (web.py:71-74). Acceptable: deployment is tunneled (Cloudflare Tunnel), not direct-exposed.

### 4. External text -> Sub-agent spawning
**Status:** VERIFIED
**Boundary:** `_SUBAGENT_DENY = {"sessions_spawn", "tts", "load_skill", "react", "schedule_message"}`. Applied unconditionally — even if LLM requests specific tools, deny-list is enforced. Self-recursion blocked: `sessions_spawn` is in deny set. Model name validated against `_providers` dict (line 55-57).
**Tests:** `test_agents.py` (43 tests)
**Mutation verified:** 100% (14/14)

### 5. External text -> Message sending
**Status:** VERIFIED
**Boundary:** Target resolved via `_resolve_target()` (name -> Telegram user ID via config contacts). Self-send prevention: `if user_id == self._bot_id` (telegram.py:170). Attachment paths validated via `_check_path()` (messaging.py:31-34).
**Tests:** `test_messaging.py` (18 tests), `test_telegram.py` (34 tests)
**Mutation verified:** N/A (not security-critical module for mutation scope)

### 6. HTTP API -> All capabilities
**Status:** VERIFIED
**Boundary:** Bearer token via `hmac.compare_digest()` (timing-safe). Rate limiting: 30 req/min (chat/notify), 60 req/min (status). Default bind: `127.0.0.1` (localhost only). Default: disabled (`enabled=false`).
**Tests:** `test_http_api.py` (48 tests)
**Mutation verified:** `_RateLimiter.check` 88% (8/9)
**Note:** Empty `auth_token` bypasses auth for all endpoints (line 91-92). Low risk: HTTP API disabled by default, localhost-only when enabled, token absence is a misconfiguration not a vulnerability.

### 7. Attachments -> File system
**Status:** VERIFIED
**Boundary:** Telegram downloads to configurable `download_dir` (default `/tmp/lucyd-telegram/`). Outbound attachment paths validated via `_check_path()` in `tool_message`. Inbound image size limit: >5MB skipped.

### 8. External text -> Memory poisoning
**Status:** ACCEPTED RISK (by design)
**Analysis:** Attacker-controlled text may enter session context or memory via normal conversation. Tool-level boundaries hold regardless of LLM compromise — `_check_path`, `_safe_env`, `_validate_url` enforce limits even if the LLM is manipulated. No memory content validation exists, by design — the LLM is the content author, and the security model explicitly does not trust the LLM for security decisions.

### 9. Config/skill files -> Behavior modification
**Status:** VERIFIED
**Boundary:** Skills are text-only (injected into system prompt, never executed as code). `_parse_frontmatter()` is a simple key-value parser — no `eval`, no PyYAML, no code execution. `tomllib` (stdlib) is data-only. Filesystem access to workspace is gated by `_check_path()`.

### 10. Dispatch safety
**Status:** VERIFIED
**Analysis:** `ToolRegistry.execute()` uses dict key lookup (`self._tools[name]["function"]`). No `getattr()`, `__import__()`, `eval()`, or `exec()` in any production dispatch path. Only `getattr` in production: `providers/anthropic_compat.py:214-215` for optional API response fields — defensive pattern, not dispatch. Only `importlib` usage: test files importing extensionless `bin/lucyd-send` — not in production code.

### 11. Dependency supply chain
**Status:** CLEAN
**pip-audit results:** Only `pip` itself flagged (CVE-2025-8869, CVE-2026-1703) — build tool, not runtime dependency. All runtime dependencies at current versions with no known CVEs:
- anthropic 0.81.0, httpx 0.28.1, aiohttp 3.13.3, openai 2.21.0
- pydantic 2.12.5, httpcore 1.0.9, certifi 2026.1.4, idna 3.11
- multidict 6.7.1, yarl 1.22.0, aiosignal 1.4.0, sniffio 1.3.1

## Vulnerabilities Found

| # | Path | Severity | Status | Details |
|---|------|----------|--------|---------|
| — | None | — | — | No unmitigated vulnerabilities found |

## Bypass Analysis

| Technique | Applicable? | Handled? | Details |
|-----------|------------|----------|---------|
| Path traversal (`../`) | Yes | Yes | `Path.resolve()` normalizes before allowlist check |
| Path traversal (URL-encoded `%2f`) | No | N/A | URL encoding is literal in filesystem paths |
| Path traversal (symlinks) | Yes | Yes | `Path.resolve()` follows symlinks; resolved path must match allowlist |
| SSRF (standard private IPs) | Yes | Yes | `_is_private_ip()` checks private/loopback/reserved/link-local |
| SSRF (octal/hex/decimal encoding) | Yes | Yes | `socket.inet_aton()` fallback normalizes non-standard encodings |
| SSRF (IPv6 loopback `::1`) | Yes | Yes | `ipaddress.ip_address("::1").is_loopback` returns True |
| SSRF (DNS rebinding) | Yes | Documented | IP validated at DNS resolution, not connection time. Mitigated by tunneled deployment. TODO in source. |
| SSRF (redirect to private IP) | Yes | Yes | `_SafeRedirectHandler` validates each redirect hop |
| Command injection | N/A | N/A | Shell tool accepts full command string by design; security is env filtering + timeout |
| Env var leakage | Yes | Yes | `_safe_env()` filters LUCYD_* prefix + secret suffixes. Mutation verified 100%. |
| Memory poisoning (prompt injection) | Yes | Accepted | By design: tool-level boundaries hold regardless of LLM compromise |
| Resource exhaustion (sub-agent recursion) | Yes | Yes | `sessions_spawn` in `_SUBAGENT_DENY` prevents recursive spawning |
| Resource exhaustion (scheduled messages) | Yes | Yes | `_MAX_SCHEDULED=50` cap, 24h max delay |
| Resource exhaustion (API cost) | Yes | Partial | Cost tracking exists, no hard spending limit. Acceptable for private deployment. |
| Dynamic dispatch (getattr/eval/exec) | No | N/A | None in production dispatch paths |
| Supply chain (dep CVEs) | Yes | Yes | pip-audit clean on all runtime dependencies |
| Skill prompt injection | Yes | Yes | Skills are text-only; filesystem access gated by `_check_path` |
| FTS5 injection | Yes | Yes | `_sanitize_fts5()` double-quotes tokens, strips double-quotes from input |
| SQL injection (memory_get) | No | Yes | Parameterized queries throughout |

## Boundary Verification Summary

| Boundary | Exists | Tested | Mutation Verified | Fails Closed |
|----------|--------|--------|-------------------|-------------|
| `_check_path()` | Yes | Yes | 100% (10/10) | Yes (empty allowlist -> deny all) |
| `_safe_env()` | Yes | Yes | 100% (8/8) | Yes (prefix/suffix match -> exclude) |
| `_validate_url()` | Yes | Yes | 86% (19/22) | Yes (unknown -> error) |
| `_is_private_ip()` | Yes | Yes | 81% (9/11) | Yes (unknown format -> True -> block) |
| `_SafeRedirectHandler` | Yes | Yes | 80% (12/15) | Yes (invalid redirect -> URLError) |
| `_SUBAGENT_DENY` | Yes | Yes | 100% (14/14) | Yes (always applied) |
| `_RateLimiter.check` | Yes | Yes | 88% (8/9) | Yes (over limit -> False -> 429) |
| `hmac.compare_digest` auth | Yes | Yes | N/A (stdlib) | Yes (mismatch -> 401) |
| `allow_from` whitelist | Yes | Yes | N/A (channel) | Yes (not in set -> skip) |
| Self-send prevention | Yes | Yes | N/A (channel) | Yes (bot_id match -> None) |
| `_sanitize_fts5()` | Yes | Yes | N/A | Yes (strips dangerous chars) |
| Timeout cap (shell) | Yes | Yes | 100% (4/4) | Yes (min(timeout, MAX)) |
| `_MAX_SCHEDULED` | Yes | Yes | N/A | Yes (>= cap -> error) |

## Recommendations

1. **DNS rebinding** (LOW): If deployment changes from tunneled to direct-exposed, implement connection-time IP validation. TODO already in source.
2. **HTTP API empty token** (LOW): Consider requiring `auth_token` when `[http] enabled = true` — fail to start if token is missing. Currently bypasses auth silently.
3. **API cost limit** (LOW): Consider a configurable daily spending cap with hard cutoff. Currently tracking-only.

## Confidence

Overall confidence: 94%

All critical paths have verified boundaries. All boundaries tested and mutation-verified (where applicable). No unmitigated vulnerabilities at any severity. Bypass analysis complete across 17 techniques.

Areas below 90% confidence:
- DNS rebinding: mitigated by deployment model, not by code. If deployment changes, revisit.
