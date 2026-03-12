# Security Audit Report

**Date:** 2026-03-12
**Audit Cycle:** 18
**EXIT STATUS:** PASS

## Threat Model

Lucyd agents process external data (Telegram, HTTP, CLI) through an agentic loop that can execute shell commands, access files, fetch URLs, and send messages. Security model: LLM is untrusted for security decisions. Boundaries are at the tool level, enforced by code.

## Input Sources

| Source | Protocol | Authentication | Risk Level |
|--------|----------|---------------|------------|
| Telegram | Bot API (long poll) | `allow_from` user ID whitelist | Medium |
| HTTP API | REST | Bearer token (HMAC) | Medium |
| FIFO (CLI) | Named pipe | Local access only | Low |
| Config/Skills | Filesystem | Local access only | Low |

## Capabilities & Boundaries

| Capability | Tool | Danger | Boundaries | Mutation Verified |
|-----------|------|--------|-----------|-------------------|
| Shell execution | `exec` | CRITICAL | `_safe_env()`, timeout, pid group kill | Yes — 100% |
| File read/write/edit | `read/write/edit` | CRITICAL | `_check_path()` allowlist | Yes — 100% |
| Web fetch | `web_fetch` | HIGH | `_validate_url()`, `_is_private_ip()`, redirect validation, DNS pinning | Yes — 100% |
| Sub-agent spawn | `sessions_spawn` | HIGH | `_subagent_deny` deny-list, timeout, max_turns | Yes — 100% |
| Message sending | `message` | HIGH | Contact name resolution, attachment `_check_path()` | Yes |
| Memory write | `memory_write` | MEDIUM | Parameterized SQL, entity normalization | Yes |
| TTS output | `tts` | MEDIUM | `_check_path()` on output_file | Yes |
| Web search | `web_search` | MEDIUM | Provider API, timeout | Yes |
| Skill loading | `load_skill` | MEDIUM | Text-only (system prompt injection), no code execution | Yes |
| Scheduling | `schedule_message` | LOW | Config max_scheduled, max_delay | Yes |
| React/Status | `react/session_status` | LOW | Contact validation / read-only | Yes |

## Critical Path Verification

### 1. External text → Shell execution
**Status:** VERIFIED
**Boundary:** `_safe_env()` strips LUCYD_* and *_KEY/*_TOKEN/*_SECRET env vars. Subprocess uses `subprocess.Popen` with explicit args list (no shell=True). Timeout enforced. Process group kill on timeout.
**Tests:** test_shell_security.py (51 tests). Mutation: 100% kill on security functions.

### 2. External text → File read/write
**Status:** VERIFIED
**Boundary:** `_check_path()` validates against configurable allowlist. Rejects paths outside allowed prefixes. Symlink resolution tested.
**Tests:** test_filesystem.py (36 tests). Mutation: 100% kill.

### 3. External text → Web requests (SSRF)
**Status:** VERIFIED
**Boundary:** `_validate_url()` validates scheme (http/https only), resolves DNS, checks `_is_private_ip()` (handles IPv4, IPv6, octal, hex, decimal encodings). Redirect handler validates each hop. DNS pinning prevents rebinding.
**Tests:** test_web_security.py (101 tests). Mutation: 100% kill on security functions.

### 4. External text → Sub-agent spawning
**Status:** VERIFIED
**Boundary:** `_subagent_deny` set filters tools available to sub-agents. Default denies: `sessions_spawn`, `tts`, `react`, `schedule_message`. Configurable via `[tools] subagent_deny`.
**Tests:** test_agents.py (26 tests). Mutation: 100% kill.

### 5. HTTP API authentication
**Status:** VERIFIED
**Boundary:** Bearer token from env var. `hmac.compare_digest()` for timing-safe comparison. Rate limiting per IP. No token configured = 503 on protected endpoints.
**Tests:** test_http_api.py (143 tests). Mutation: 100% kill on auth/rate functions.

### 6. Memory poisoning (structured)
**Status:** ACCEPTED RISK
**Analysis:** Structured facts are read-only context injection. No code path uses `fact.value` as tool input, entity names in file paths, or `resolve_entity()` output in dispatch paths. Verified via grep — all access is parameterized SQL read.

## Pattern Checks

| Pattern | Result |
|---------|--------|
| P-003 unchecked filesystem write | All tool file operations go through `_check_path()`. TTS output_file validated. |
| P-009 capability table stale | Re-derived from source. 19 tools across 11 modules, all boundaries verified. |
| P-012 auto-populated pipeline | `entity_aliases` correctly identified as auto-populated. No misclassification. |
| P-018 unbounded data structures | All collections bounded by config (allow_from, tools, contacts). `_RateLimiter` has cleanup threshold. |
| P-028 control endpoint audit | All POST handlers route through queue (verified by TestQueueRoutingInvariant). |

## Bypass Analysis

| Technique | Applicable? | Handled? | Details |
|-----------|------------|----------|---------|
| Path traversal | Yes | Yes | `_check_path()` validates against prefix allowlist |
| SSRF encoding | Yes | Yes | `_is_private_ip()` handles octal/hex/decimal/IPv6 via `socket.inet_aton()` fallback |
| Command injection | Yes | Yes | No shell=True, explicit args list, env filtered |
| Env var leakage | Yes | Yes | `_safe_env()` strips sensitive patterns |
| Memory poisoning | Yes | Accepted | Facts are context-only, never tool inputs |
| Resource exhaustion | Yes | Yes | Rate limiting, max_turns, timeout, queue capacity |
| Dynamic dispatch | No | N/A | Dict lookup only, no getattr/eval/exec in dispatch |
| Supply chain (CVEs) | Yes | Yes | `pip-audit`: 0 known vulnerabilities |
| Skill prompt injection | Yes | Accepted | Skills are text-only system prompt content, no code execution |

## Vulnerabilities Found

None.

## Known Gaps

| Gap | Severity | Status |
|-----|----------|--------|
| Provider `complete()` mock-boundary | Low | ACCEPTED (permanent) |

## Confidence

96% — all critical paths verified with mutation-tested boundaries. Supply chain clean. No new attack surfaces since Cycle 17.
