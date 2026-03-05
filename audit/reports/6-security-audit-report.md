# Security Audit Report

**Date:** 2026-03-04
**Audit Cycle:** 15
**EXIT STATUS:** PASS

## Changes Since Cycle 14

1. **New HTTP endpoint:** `POST /api/v1/compact` — behind auth middleware (verified)
2. **Media group batching:** `_merge_media_group()` replicates access control (`bot_id`, `allow_from`)
3. **Image caption enrichment:** `_enrich_image_caption()` processes assistant text only (trusted source)
4. **CVE-2026-28804 in pypdf 6.7.4** — fixed by updating to 6.7.5

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-003 (unchecked filesystem write) | CLEAN | No new file-path parameters in tools |
| P-009 (capability table stale) | CLEAN | Re-derived: 19 built-in + 1 plugin. No new tools. |
| P-012 (auto-populated misclassified) | CLEAN | Verified against Stage 5 |
| P-018 (unbounded structures) | CLEAN | `pending_groups` in telegram.py is bounded — groups flush after 0.5s, cannot accumulate |
| P-028 (control endpoint audit) | CLEAN | Compact endpoint uses queue via `_handle_compact`. AI-002 compliant. |

## Input Sources

| Source | Protocol | Authentication | Risk Level |
|--------|----------|---------------|------------|
| Telegram | Bot API long poll | Bot token (server-side) | Medium |
| HTTP API | REST | Bearer token + HMAC timing-safe | Medium |
| FIFO (CLI) | Named pipe | Local access only (0o600) | Low |
| Config files | Filesystem | Local access | Low |
| Skill files | Filesystem (text-only) | Local access | Low |

## Capabilities

| Capability | Tool | Danger Level | Boundaries |
|------------|------|-------------|------------|
| Shell execution | exec | CRITICAL | `_safe_env()`, timeout cap, process group kill |
| File read/write | read, write, edit | CRITICAL | `_check_path()` allowlist, symlink resolution |
| Web requests | web_fetch, web_search | HIGH | Scheme whitelist, `_is_private_ip()`, DNS-once IP pinning |
| Sub-agent spawn | sessions_spawn | CRITICAL | Deny-list, tool scoping |
| Message sending | message, react | HIGH | Contact allowlist, attachment path validation |
| Memory write | memory_write, memory_forget | MEDIUM | Parameterized SQL |
| Status query | session_status | LOW | Read-only |
| Skill loading | load_skill | MEDIUM | Name-based lookup, text-only |
| TTS | tts | LOW | API key guard, temp cleanup |
| Scheduling | schedule_message | LOW | Max 50, delay bounds |
| Window display | window | LOW | `shlex.quote()`, hardcoded SSH host |

## New Path Analysis

### Compact endpoint (POST /api/v1/compact)
- **Auth:** Behind `_auth_middleware` (Bearer token, `hmac.compare_digest`) — VERIFIED
- **Rate limiting:** Behind `_rate_middleware` — VERIFIED
- **Queue routing:** Uses `_handle_compact()` → `_process_message()` — AI-002 compliant
- **No new capabilities:** Routes through existing agentic loop
- **Risk:** LOW — authenticated, rate-limited, no new capabilities exposed

### Media group batching (_merge_media_group)
- **Access control:** Replicates `bot_id` and `allow_from` checks from `_parse_message` — VERIFIED
- **Test:** `test_media_group_from_disallowed_user_skipped` — VERIFIED
- **Bounded:** `pending_groups` dict flushes after 0.5s `_MEDIA_GROUP_DELAY` — cannot grow unbounded
- **Risk:** LOW — same trust level as individual messages

### Image caption enrichment (_enrich_image_caption)
- **Input source:** Assistant-generated text (from LLM response, trusted)
- **Output:** Stored in JSONL session, never executed
- **Truncation:** 200 chars max, whitespace normalized
- **Single replacement:** `replace(tag, ..., 1)` limits scope
- **Risk:** NEGLIGIBLE

## Supply Chain

**pip-audit:** 0 vulnerabilities (after pypdf update)

| Item | Status |
|------|--------|
| Known CVEs | 0 (was 1: CVE-2026-28804 in pypdf 6.7.4, FIXED) |
| `certifi` CA bundle | 2026.2.25 (current) |
| `pypdf` | 6.7.5 (updated from 6.7.4) |
| `anthropic` SDK | 0.81.0 (no CVEs) |
| `openai` SDK | 2.21.0 (no CVEs) |

## Boundary Verification Summary

| Boundary | Exists | Tested | Mutation Verified | Fails Closed |
|----------|--------|--------|-------------------|-------------|
| `_safe_env()` | Yes | 16 tests | 100% kill | Yes |
| `_safe_parse_args()` | Yes | 5 tests | 100% kill | Yes |
| `_check_path()` | Yes | 14 tests | 100% kill | Yes |
| `_is_private_ip()` | Yes | 20+ tests | 2 equiv | Yes |
| `_validate_url()` | Yes | 13+ tests | 3 cosmetic | Yes |
| `_subagent_deny` | Yes | 5+ tests | 100% kill | Yes |
| `_auth_middleware` | Yes | 15+ tests | 100% kill | Yes |
| `_rate_middleware` | Yes | 3+ tests | 100% kill | Yes |
| `hmac.compare_digest` | Yes | tested | 100% kill | Yes |
| `shlex.quote` (window) | Yes | 1 test | N/A (stdlib) | Yes |
| **`allow_from` in merge_media** | Yes | 1 test | N/A (new) | Yes |

## Vulnerabilities Found & Fixed

| # | Path | Severity | Status | Fix |
|---|------|----------|--------|-----|
| 1 | pypdf DoS | Medium | FIXED | Updated 6.7.4 → 6.7.5 |

## Confidence

**Overall: 97%**

- Security boundaries: 98% — all unchanged, all verified
- New code: 96% — compact behind auth, media group replicates access control
- Supply chain: 100% — pip-audit clean after pypdf fix
