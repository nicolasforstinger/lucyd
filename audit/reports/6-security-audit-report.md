# Security Audit Report

**Date:** 2026-02-24
**Audit Cycle:** 7
**EXIT STATUS:** PASS

## Threat Model

Lucyd is an autonomous agent processing external data from Telegram messages, HTTP API requests, FIFO commands, and n8n webhook payloads. Data flows through the agentic loop (LLM) and can trigger tool execution: shell commands, filesystem access, web requests, sub-agent spawning, and message sending. The security model: **the LLM is UNTRUSTED for security decisions.** All security boundaries are code-enforced at the tool level.

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-003 (unchecked filesystem write) | CLEAN | All 19 tool functions verified. Path-accepting tools call `_check_path()` before I/O. `tool_memory_get` file_path is a SQL lookup key (parameterized), not filesystem I/O. New module `synthesis.py` has zero file I/O. |
| P-009 (capability table stale) | CLEAN | Full capability table re-derived from source. 19 tools across 11 modules. No new tools since Cycle 5. `synthesis.py` is not a tool — it is an internal transformation layer. |
| P-012 (misclassified static) | CLEAN | Config files genuinely static. Entity aliases correctly classified as LLM-extracted (Stage 5 P-012 confirmed). Ordering invariant preserved. |
| P-018 (resource exhaustion) | 2 NOTED | `asyncio.Queue` unbounded (lucyd.py:285). `_last_inbound_ts` now bounded (fixed in hardening batch — OrderedDict, 1000 cap). Queue depth remains unbounded — mitigated by rate limiter but not capped. `synthesis.py` adds no new data structures. |

## Input Sources

| Source | Protocol | Authentication | Risk Level |
|--------|----------|---------------|------------|
| Telegram | Bot API long polling | `allow_from` user ID allowlist | HIGH |
| HTTP /chat, /notify | REST POST | Bearer token (hmac.compare_digest) | HIGH |
| HTTP /status | REST GET | None (health check exempt) | LOW |
| HTTP /sessions, /cost | REST GET | Bearer token | LOW |
| FIFO | Named pipe, JSON/line | Unix file permissions (0o600) | LOW |
| CLI | stdin/stdout | Local terminal access | LOW |
| Config files | TOML (startup only) | Filesystem permissions | LOW |
| Skill files | Markdown (startup + SIGUSR1) | Filesystem permissions | MEDIUM |
| Plugin directory | Python (startup) | Filesystem permissions | CRITICAL |
| Memory DB | SQLite (WAL mode) | Filesystem permissions | MEDIUM |

**No new input sources.** `synthesis.py` does not introduce any input boundary. It receives data exclusively from internal callers (`lucyd.py:780` and `tools/memory_tools.py:59`).

## Capabilities

| # | Tool | Module | Danger | Boundaries |
|---|------|--------|--------|------------|
| 1 | exec | shell.py | CRITICAL | `_safe_env()`, timeout (600s), `start_new_session=True` |
| 2 | read | filesystem.py | MEDIUM | `_check_path()` allowlist, `Path.resolve()` |
| 3 | write | filesystem.py | MEDIUM | `_check_path()` allowlist, `Path.resolve()` |
| 4 | edit | filesystem.py | MEDIUM | `_check_path()` allowlist, `Path.resolve()` |
| 5 | sessions_spawn | agents.py | HIGH | `_subagent_deny` deny-list, `max_turns=10` (schema-hidden) |
| 6 | web_fetch | web.py | MEDIUM | `_validate_url()`, `_is_private_ip()`, IP pinning, redirect validation |
| 7 | message | messaging.py | MEDIUM | `_resolve_target()`, self-send block, `_check_path()` attachments |
| 8 | web_search | web.py | MEDIUM | Hardcoded Brave API URL, API key gated |
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

## Path Matrix

| Input -> Capability | Boundary | Tested? | Mutation Verified? | Status |
|-------------------|----------|---------|-------------------|--------|
| Telegram -> exec | `_safe_env()`, timeout | Yes | Yes (100% kill) | VERIFIED |
| Telegram -> read/write/edit | `_check_path()` allowlist | Yes | Yes (100% kill) | VERIFIED |
| Telegram -> web_fetch | `_validate_url()` + SSRF stack | Yes | Yes (80-86% kill, equivalent survivors) | VERIFIED |
| Telegram -> sessions_spawn | `_subagent_deny` deny-list | Yes | Yes (100% kill) | VERIFIED |
| Telegram -> message | `_resolve_target()`, `_check_path()` attachments | Yes | Yes | VERIFIED |
| Telegram -> tts | `_check_path()` on output_file | Yes | Yes | VERIFIED |
| HTTP API -> all tools | Bearer token (hmac.compare_digest) + rate limiting + 10 MiB body | Yes | N/A | VERIFIED |
| FIFO -> all tools | Unix permissions (0o600), JSON validation | Yes | N/A | VERIFIED |

## Critical Path Verification

### 1. External text -> Shell execution
**Status:** VERIFIED (accepted risk — exec is unrestricted by design)
**Boundary:** `_safe_env()` filters `LUCYD_*` prefix and secret suffixes. Timeout 600s. Process group isolation.
**Tests:** test_shell_security.py. `_safe_env()` 100% mutation kill rate.

### 2. External text -> File read/write
**Status:** VERIFIED
**Boundary:** `_check_path()` — `Path.resolve()` normalizes traversal/symlinks, then prefix allowlist.
**Finding:** Prefix match without trailing separator (Finding #1, carried from Cycle 3).

### 3. External text -> Web requests (SSRF)
**Status:** VERIFIED
**Boundary:** Full SSRF protection stack: scheme whitelist, DNS resolution, `_is_private_ip()` with octal/hex normalization, IP pinning, redirect-hop validation. Fail-closed.

### 4. External text -> Sub-agent spawning
**Status:** VERIFIED
**Boundary:** `sessions_spawn` in own deny-list (recursion blocked). `max_turns=10` hidden from schema.

### 5. External text -> Message sending
**Status:** VERIFIED
**Boundary:** `_resolve_target()` contacts dict, self-send blocked, attachment paths checked.

### 6. HTTP API -> All capabilities
**Status:** VERIFIED
**Boundary:** `hmac.compare_digest()` timing-safe. No-token -> 503. Rate limiting. 10 MiB body cap.

### 7. Attachments -> File system
**Status:** VERIFIED (hardening opportunity)
**Telegram/HTTP:** Timestamp prefix makes traversal non-exploitable (intermediate dir doesn't exist). Defense is accidental.
**Finding:** Unsanitized filename (Finding #2, carried from Cycle 4).

### 8. Memory poisoning
**Status:** ACCEPTED RISK
**Analysis:** Facts are text context only — never become tool arguments. All SQL parameterized. `resolve_entity()` output used only in parameterized WHERE clauses.

### 9. Config/skill files
**Status:** VERIFIED
Skills text-only. Config TOML data-only. Plugins guarded by filesystem permissions.

### 10. Dispatch safety
**Status:** VERIFIED
Dict key lookup only. No eval/exec/__import__/getattr in dispatch paths.

### 11. Supply chain
**Status:** CLEAN
pip-audit: 0 runtime CVEs. 2 CVEs in pip 25.1.1 (dev tool only).

## Changes Since Cycle 6

| Change | Security Impact | Verified? |
|--------|----------------|-----------|
| `synthesis.py` (new module) | **Zero new attack surface** — see analysis below | Yes — 23 tests in test_synthesis.py |
| `set_synthesis_provider()` in memory_tools.py | Module-global wiring (same pattern as `_memory`, `_conn`, `_config`) | Yes — TestToolPathSynthesis (3 tests) |
| `synthesize_recall()` call in lucyd.py:780-796 | Internal data transform, no new input boundary | Yes — tested in test_synthesis.py |
| `synthesize_recall()` call in memory_tools.py:59-63 | Internal data transform via tool path | Yes — TestToolPathSynthesis |

### synthesis.py Security Analysis

**Module purpose:** Transforms raw recall blocks (internally generated by `inject_recall()`) into style-appropriate prose before injection into the system prompt. Optional — defaults to passthrough for `style="structured"`.

**Attack surface assessment: ZERO new attack surface.** Verified point-by-point:

| Concern | Finding |
|---------|---------|
| User-controlled input? | **No.** `recall_text` comes from `inject_recall()` (internal). `style` comes from `config.recall_synthesis_style` (TOML config). `provider` is the routed LLM provider instance. None are user-controlled. |
| subprocess / eval / exec? | **No.** `grep` confirms zero matches for `eval(`, `exec(`, `__import__`, `getattr(`, `importlib`, `subprocess`, `os.system`, `os.popen`. |
| File I/O? | **No.** `grep` confirms zero matches for `open(`, `.write(`, `.read(`, `sqlite`, `sql`, `Path(`. |
| Network access? | **No.** Network is delegated entirely to `provider.complete()`, which is the existing LLM call path already audited. |
| SQL injection? | **No.** Module contains no SQL. |
| Format string injection? | **No.** `prompt_template.format(recall_text=recall_text)` uses a named key. The template is a module-level constant (`PROMPTS` dict). Even if `recall_text` contained `{...}` placeholders, Python's `str.format()` only substitutes named keys present in the call — `{recall_text}` is the only key, so `{anything_else}` would raise `KeyError`, caught by the blanket `except Exception` which falls back to raw recall. |
| Fail-open risk? | **No.** All failure modes (empty result, unknown style, provider exception) fall back to returning the original `recall_text` unchanged. This is fail-safe — the worst case is no synthesis, not data loss or behavior change. |
| Resource exhaustion? | **No new vector.** One LLM call per session start (or per `memory_search` tool call). Both paths already have existing rate controls. `SynthesisResult` is a small object with `__slots__`. |

**`set_synthesis_provider()` wiring pattern:**

The function sets a module-level global `_synth_provider` in `tools/memory_tools.py`. This follows the identical pattern used by all 7 other tool module globals:

| Global | Setter | Module |
|--------|--------|--------|
| `_memory` | `set_memory()` | memory_tools.py |
| `_conn` | `set_structured_memory()` | memory_tools.py |
| `_config` | `set_structured_memory()` | memory_tools.py |
| `_synth_provider` | `set_synthesis_provider()` | memory_tools.py |
| `_channel` | `configure()` | messaging.py |
| `_channel` | `configure()` | scheduling.py |
| `_config` | `configure()` | agents.py |
| `_skill_loader` | `configure()` | skills_tool.py |

All are set per-message in `_process_message()` or at daemon startup. No user input reaches any setter. The `_synth_provider` is set at `lucyd.py:825`, guarded by `config.recall_synthesis_style != "structured"`, using the same `provider` variable that powers the main agentic loop. No privilege escalation, no model mismatch.

## Vulnerabilities Found

| # | Path | Severity | Status | Description |
|---|------|----------|--------|-------------|
| 1 | Filesystem `_check_path()` | Low | OPEN (Cycle 3) | Prefix match without trailing separator |
| 2 | Attachments -> download | Low | OPEN (Cycle 4) | Unsanitized filename in both channels |

Both carried forward — unchanged since previous cycles. Neither is exploitable in current deployment model.

## Bypass Analysis

| Technique | Applicable? | Handled? | Details |
|-----------|------------|----------|---------|
| Path traversal (`../`) | Yes | Yes | `Path.resolve()` before prefix check |
| Path prefix ambiguity | Yes | **Partial** | Finding #1 |
| SSRF encoding (octal/hex/decimal) | Yes | Yes | `inet_aton()` normalization |
| SSRF IPv6 (`[::1]`) | Yes | Yes | `ipaddress.ip_address()` handles IPv6 |
| SSRF DNS rebinding | Yes | Yes | IP pinning via custom handlers |
| SSRF redirect chain | Yes | Yes | `_SafeRedirectHandler` validates each hop |
| Command injection | N/A | N/A | `exec` takes entire command by design |
| Env var leakage | Yes | Yes | `_safe_env()` filters by prefix + suffix |
| Session poisoning | Yes | Accepted | Tool-level boundaries sufficient |
| Structured memory poisoning | Yes | Verified | Facts never reach tool arguments |
| Resource exhaustion (cost) | Partial | Partial | Sub-agent recursion blocked. Rate limiting. No per-turn tool limit |
| Resource exhaustion (queue) | Yes | **Partial** | asyncio.Queue unbounded. Mitigated by rate limiter |
| Dynamic dispatch injection | Yes | Yes | Dict-key lookup only |
| Supply chain (dep CVEs) | Yes | Clean | 0 runtime CVEs |
| Attachment filename traversal | Yes | **Accidental** | Finding #2 |
| Synthesis prompt injection | N/A | N/A | `recall_text` is internally generated, not user-controlled. Even if attacker-influenced text appears in recall (via memory poisoning), synthesis output goes into system prompt context — same trust level as raw recall. No escalation. |

## Boundary Verification Summary

| Boundary | Exists | Tested | Mutation Verified | Fails Closed |
|----------|--------|--------|-------------------|-------------|
| `_check_path()` (allowlist) | Yes | Yes | Yes (100%) | Yes |
| `_safe_env()` (env filter) | Yes | Yes | Yes (100%) | Yes |
| `_safe_parse_args()` (JSON fallback) | Yes | Yes | Yes (100%) | Yes |
| `_validate_url()` (SSRF) | Yes | Yes | Yes (86.4%) | Yes |
| `_is_private_ip()` (IP check) | Yes | Yes | Yes (81.8%) | Yes |
| `_SafeRedirectHandler` | Yes | Yes | Yes (80%) | Yes |
| IP pinning (DNS rebinding) | Yes | Yes | N/A | Yes |
| `_subagent_deny` (deny-list) | Yes | Yes | Yes (100%) | Yes |
| HTTP Bearer auth | Yes | Yes | N/A | Yes (503 on no-token) |
| `hmac.compare_digest` | Yes | Yes | N/A | Yes |
| HTTP rate limiting | Yes | Yes | N/A | Yes (-> 429) |
| HTTP body size cap | Yes | Yes | N/A (aiohttp) | Yes (-> 413) |
| Telegram `allow_from` | Yes | Yes | N/A | Yes |
| FIFO permissions | Yes | N/A (OS) | N/A | Yes (0o600) |

## Security Test Results

All security-focused tests pass (run 2026-02-24):

```
tests/test_shell_security.py    — 57 passed
tests/test_web_security.py      — 47 passed
tests/test_filesystem.py        — 39 passed
tests/test_synthesis.py         — 23 passed
                         Total: 166 passed, 0 failed
```

## Recommendations

1. **(Low)** Fix `_check_path()` prefix matching — add trailing separator. Carried from Cycle 3.
2. **(Low)** Sanitize attachment filenames — `Path(filename).name`. Carried from Cycle 4.
3. **(Info)** Upgrade pip to 26.0 for CVE fixes (dev tool only).
4. **(Info)** Consider `asyncio.Queue(maxsize=N)` for defense against queue-flooding.

## Confidence

Overall confidence: 96%

- **CRITICAL capabilities (exec, filesystem, sub-agents):** 98%. All boundaries mutation-verified at 100% kill.
- **HIGH capabilities (web_fetch, messaging):** 97%. SSRF protection comprehensive.
- **New module (synthesis.py):** 99%. Zero attack surface — no I/O, no subprocess, no SQL, no user input. Pure data transformation with fail-safe fallback. 23 dedicated tests.
- **New wiring (set_synthesis_provider):** 99%. Identical pattern to 7 existing module globals. No user input in setter path.
- **Authentication (HTTP API):** 98%. Timing-safe. Fail-closed.
- **Indirect paths (memory poisoning):** 95%. No code routes fact values to tool arguments. Synthesis does not change this — synthesized text enters system prompt at same trust level as raw recall.
- **Supply chain:** 98%. Zero runtime CVEs.
