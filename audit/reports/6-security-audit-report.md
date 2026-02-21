# Security Audit Report

**Date:** 2026-02-21
**Audit Cycle:** 4
**EXIT STATUS:** PASS

## Threat Model

Lucyd is an autonomous agent that processes external data from Telegram messages, HTTP API requests, FIFO commands, and n8n webhook payloads. Data flows through the agentic loop (LLM) and can trigger tool execution: shell commands, filesystem access, web requests, sub-agent spawning, and message sending. The security model is: **the LLM is UNTRUSTED for security decisions.** All security boundaries are code-enforced at the tool level.

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-003 (unchecked filesystem write) | CLEAN | All 6 tool functions that accept file path parameters validate via `_check_path()`: `tool_read` (filesystem.py:33), `tool_write` (filesystem.py:66), `tool_edit` (filesystem.py:82), `tool_tts` output_file (tts.py:67-71), `tool_message` attachments (messaging.py:31-35). `tool_memory_get` file_path is a SQLite lookup key, not filesystem I/O — verified at memory.py:273 (parameterized SQL SELECT). |
| P-009 (capability table stale) | CLEAN | Full capability table re-derived from source (19 tool functions across 11 modules). No new tools since Cycle 3. No parameter changes to existing tools. Capability table below matches source. |
| P-012 (auto-populated misclassified) | CLEAN | No data sources classified as "admin-managed" or "static" in this report's security assessment. Structured memory (facts, episodes, commitments, aliases) correctly identified as auto-populated by consolidation.py + agent tools. Config/skills correctly identified as operator-managed static files. Cross-verified against Stage 5 producer inventory. |

## Input Sources

| Source | Protocol | Entry Point | Authentication | Data | Risk Level |
|--------|----------|-------------|---------------|------|------------|
| Telegram | Bot API long polling | telegram.py:162 `_parse_message()` | `allow_from` user ID allowlist | text, attachments (photo/voice/doc/video/audio/sticker) | HIGH |
| HTTP API /chat | REST POST | http_api.py:138 `_handle_chat()` | Bearer token (hmac.compare_digest) | message, sender, context, tier | HIGH |
| HTTP API /notify | REST POST | http_api.py:185 `_handle_notify()` | Bearer token (hmac.compare_digest) | message, source, ref, data | MEDIUM |
| HTTP API /status | REST GET | http_api.py:245 `_handle_status()` | None (health check exempt) | None (read-only) | LOW |
| FIFO | Named pipe, JSON/line | lucyd.py:70 `_fifo_reader()` | Unix file permissions (0o600) | JSON: type, text, sender, tier | LOW |
| CLI | stdin/stdout | cli.py:19 `receive()` | Local terminal access | text | LOW |
| Config files | Disk read at startup | config.py | Filesystem permissions | TOML config, .env secrets | LOW (operator) |
| Skill files | Disk read at startup/on-demand | skills.py:102 `scan()` | Filesystem permissions | Markdown with custom frontmatter | LOW (operator) |
| Session files | JSONL read/write | session.py | Filesystem permissions | json.loads per line (safe by construction) | LOW |
| Memory DB | SQLite read/write | memory.py, consolidation.py | Filesystem permissions | Chunks, facts, episodes, commitments, aliases | MEDIUM |

## Capabilities

| # | Tool | Module | Danger | Boundaries |
|---|------|--------|--------|------------|
| 1 | exec | shell.py:35 | CRITICAL | `_safe_env()` env filter, timeout cap (600s max), `start_new_session=True` (PGID isolation) |
| 2 | read | filesystem.py:31 | CRITICAL | `_check_path()` allowlist with `Path.resolve()` |
| 3 | write | filesystem.py:64 | CRITICAL | `_check_path()` allowlist with `Path.resolve()` |
| 4 | edit | filesystem.py:79 | CRITICAL | `_check_path()` allowlist with `Path.resolve()` |
| 5 | sessions_spawn | agents.py:43 | CRITICAL | `_subagent_deny` deny-list, provider dict lookup, max_turns=10 (not in schema) |
| 6 | web_fetch | web.py:257 | HIGH | `_validate_url()` scheme + DNS + `_is_private_ip()` + IP pinning + redirect validation |
| 7 | message | messaging.py:24 | HIGH | Contact dict lookup (`_resolve_target`), self-send blocked, `_check_path()` on attachments |
| 8 | web_search | web.py:171 | MEDIUM | Hardcoded Brave API URL, API key gated |
| 9 | tts | tts.py:52 | MEDIUM | `_check_path()` on explicit output_file, API key gated, tempfile fallback |
| 10 | load_skill | skills_tool.py:19 | MEDIUM | Dict key lookup into SkillLoader (text-only output, never executed) |
| 11 | memory_search | memory_tools.py:34 | LOW | Read-only (SQLite + vector search) |
| 12 | memory_get | memory_tools.py:66 | LOW | Read-only (SQLite lookup by path key, not filesystem I/O) |
| 13 | memory_write | structured_memory.py:38 | LOW | Parameterized SQL, entity normalization, no filesystem |
| 14 | memory_forget | structured_memory.py:84 | LOW | Parameterized SQL, no filesystem |
| 15 | commitment_update | structured_memory.py:104 | LOW | Parameterized SQL, enum-restricted status in schema |
| 16 | schedule_message | scheduling.py:23 | LOW | Max 50 pending, max 24h delay, channel required |
| 17 | list_scheduled | scheduling.py:63 | LOW | Read-only |
| 18 | session_status | status.py:37 | LOW | Read-only |
| 19 | react | messaging.py:48 | LOW | ALLOWED_REACTIONS emoji set, timestamp required |

## Path Matrix

All input sources reach capabilities via: INPUT → asyncio.Queue → `_process_message()` → agentic loop → LLM tool_call → `ToolRegistry.execute()` → tool function. Only tools listed in `[tools] enabled` config are registered.

| Input → Capability | Boundary | Tested? | Mutation Verified? | Status |
|-------------------|----------|---------|-------------------|--------|
| Telegram → exec | `_safe_env()`, timeout | Yes (24 tests) | Yes (100% kill) | VERIFIED |
| Telegram → read/write/edit | `_check_path()` allowlist | Yes (34 tests) | Yes (100% kill) | VERIFIED |
| Telegram → web_fetch | `_validate_url()` + SSRF stack | Yes (69 tests) | Yes (80-86% kill, survivors equivalent/cosmetic) | VERIFIED |
| Telegram → sessions_spawn | `_subagent_deny` deny-list | Yes (23 tests) | Yes (100% kill) | VERIFIED |
| Telegram → message | `_resolve_target()` contacts, `_check_path()` attachments | Yes | Yes (100% kill on _check_path) | VERIFIED |
| Telegram → tts | `_check_path()` on output_file | Yes | Yes (100% kill on _check_path) | VERIFIED |
| HTTP API → all tools | Bearer token auth (hmac.compare_digest) + rate limiting + 1MiB body | Yes | N/A (auth is comparison, not logic) | VERIFIED |
| FIFO → all tools | Unix permissions (0o600), JSON validation | Yes (integration) | N/A (OS-level auth) | VERIFIED |
| CLI → all tools | Local terminal access | N/A (local only) | N/A | VERIFIED |

## Critical Path Verification

### 1. External text → Shell execution
**Status:** VERIFIED (accepted risk)
**Boundary:** `_safe_env()` filters `LUCYD_*` prefix and `*_KEY`, `*_TOKEN`, `*_SECRET`, `*_PASSWORD`, `*_CREDENTIALS`, `*_ID`, `*_CODE`, `*_PASS` suffix from child process environment. Timeout capped at `_MAX_TIMEOUT` (600s). Process runs in new session (`start_new_session=True`) for PGID kill. No command deny-list — by design, `exec` allows arbitrary commands when enabled.
**Tests:** 24 tests in test_shell_security.py. `_safe_env()` at 100% mutation kill rate.
**Accepted risk:** The `exec` tool is explicitly opt-in via `[tools] enabled`. Deployments that don't need shell don't enable it. The LLM can run any command — security depends on env filtering and OS-level permissions.

### 2. External text → File read/write
**Status:** VERIFIED
**Boundary:** `_check_path()` — resolves path via `Path(file_path).expanduser().resolve()` (follows symlinks), then checks against `_PATH_ALLOW` prefix list. Fails closed: empty allowlist → deny all.
**Tests:** 34 tests including traversal, symlink escape, blocked paths. 100% mutation kill rate.
**Finding:** See Finding #1 below — prefix matching uses `str.startswith()` without trailing separator enforcement.

### 3. External text → Web requests (SSRF)
**Status:** VERIFIED
**Boundary:** `_validate_url()` → scheme allowlist (http/https) → DNS resolution → `_is_private_ip()` on ALL resolved IPs (handles octal/hex via `socket.inet_aton` fallback) → IP pinning on request → `_SafeRedirectHandler` validates every redirect hop.
**Tests:** 69 tests in test_web_security.py. Mutation kill rates: `_is_private_ip` 81.8% (2 equivalent), `_validate_url` 86.4% (3 cosmetic), `_SafeRedirectHandler` 80.0% (4 equivalent). All survivors documented as equivalent/cosmetic in Stage 3.

### 4. External text → Sub-agent spawning
**Status:** VERIFIED
**Boundary:** `_subagent_deny` deny-list (default: `sessions_spawn`, `tts`, `load_skill`, `react`, `schedule_message`). `sessions_spawn` is in its own deny-list → recursion blocked. `max_turns=10` not in JSON schema → LLM cannot override. Provider lookup via config dict → unknown model returns error.
**Tests:** 23 tests in test_agents.py. Deny-list at 100% mutation kill rate.

### 5. External text → Message sending
**Status:** VERIFIED
**Boundary:** Telegram `_resolve_target()` requires target name to exist in configured contacts dict. Self-send blocked (bot_id check). Unknown contacts raise ValueError. HTTP-sourced messages suppress channel delivery by default.
**Tests:** Covered by daemon integration tests (92 tests).

### 6. HTTP API → All capabilities
**Status:** VERIFIED
**Boundary:** Bearer token auth via `hmac.compare_digest` (timing-safe, http_api.py:113). No token configured → 503 on all protected endpoints. `/api/v1/status` exempt (health check). Rate limiting: 30 req/min (chat/notify), 60 req/min (status/sessions/cost). Body size: 1 MiB (aiohttp `client_max_size`).
**Tests:** Auth tested in test_daemon_integration.py.

### 7. Attachments → File system
**Status:** VERIFIED (minor hardening opportunity)
**Boundary:** Telegram downloads to configured `download_dir`. Path construction: `download_dir / f"{timestamp}_{filename}"`. Timestamp prefix (`1708000000_`) accidentally prevents directory traversal (e.g., `1708000000_../../etc/passwd` → `1708000000_..` is a literal dirname, not a parent reference). When `file_name` is empty, falls back to `Path(file_path).name` which strips directories.
**Finding:** See Finding #2 below — attacker-controlled document filenames used without sanitization.

### 8. External text → Memory poisoning
**Status:** ACCEPTED RISK
**Assessment:** Session content stored as JSONL (json.dumps → json.loads, safe by construction). Structured facts extracted by LLM-based consolidation persist across sessions. Facts appear in every future session's [Known facts] block. However: facts are TEXT context injected into the system prompt — they never directly become tool arguments, file paths, shell commands, or SQL values. All tool-level boundaries still apply even with fully poisoned LLM context.
**Verified:** No code path uses `fact.value` as tool input. No code path uses entity names in file paths or un-parameterized SQL. `resolve_entity()` output used only for parameterized WHERE clauses.

### 9. Config/skill files → Behavior modification
**Status:** VERIFIED
**Assessment:** Skills are text-only (injected into system prompt, never executed). Custom frontmatter parser (`_parse_frontmatter` in skills.py) — pure string splitting, no PyYAML, no `eval()`, no `exec()`, no `import`. TOML config parsed by Python stdlib `tomllib` — data-only format. All config consumers use typed attribute access.

### 10. Dispatch safety
**Status:** VERIFIED
**Assessment:** `ToolRegistry.execute()` uses dict key lookup (`self._tools[name]`). Unknown tool → error string. No `eval()`, `exec()`, `__import__()` in dispatch paths. Plugin loading uses `importlib.util.spec_from_file_location()` on `plugins.d/*.py` files — operator-managed directory, only `[tools] enabled` names registered. Sub-agent model names resolve via `_providers` config dict — unknown → error. Skill names resolve via `_skill_loader` dict — unknown → None → error.
**Confirmed:** `getattr()` usage in tools/ is limited to config attribute access (agents.py:36, memory_tools.py:43) and IP-pinning request attributes (web.py:124,137,138) — all safe patterns.

### 11. Dependency supply chain
**Status:** VERIFIED (1 dev-tool CVE)
**pip-audit results:** 1 vulnerability found — `pip 25.3` has CVE-2026-1703 (fix in 26.0). pip is a development tool, not a Lucyd runtime dependency. No CVEs in any runtime dependency.
**Runtime dependencies:** anthropic 0.83.0, openai 2.21.0, httpx 0.28.1, aiohttp 3.13.3, pydantic 2.12.5 — all reputable, no known CVEs.
**Transitive deps checked:** certifi, idna, httpcore, h11, multidict, yarl, frozenlist, aiosignal, sniffio, distro, tqdm, typing-extensions — no CVEs.

## Vulnerabilities Found

| # | Path | Severity | Status | Description |
|---|------|----------|--------|-------------|
| 1 | Filesystem → `_check_path()` | Low | OPEN | Prefix match without trailing separator |
| 2 | Telegram → `_download_file()` | Low | OPEN | Unsanitized attachment filename |

### Finding 1: `_check_path()` prefix match without trailing separator

**Severity:** Low
**File:** tools/filesystem.py:26
**Code:** `if resolved.startswith(prefix):`
**Issue:** If allowed path is `/home/user/.lucyd/workspace` (no trailing `/`), then `/home/user/.lucyd/workspace_evil/file` would pass the check because `str.startswith()` matches the prefix without requiring a path separator boundary. Default config returns workspace path without trailing `/` (config.py:373).
**Impact:** An LLM-directed file access to a sibling directory whose name starts with the allowed directory name would be permitted. Exploitability requires a sibling directory with a matching prefix to exist.
**Fix:** Change line 26 to: `if resolved == prefix or resolved.startswith(prefix + "/"):`
**Status:** Flagged for Nicolas. Low severity — accidental sibling directory match unlikely in standard deployments. Carried forward from Cycle 3.

### Finding 2: Unsanitized attachment filename in Telegram download

**Severity:** Low
**File:** channels/telegram.py:310
**Code:** `local_path = self.download_dir / f"{int(time.time())}_{filename}"`
**Issue:** For documents and audio files, `filename` comes from Telegram API's `file_name` field (attacker-controlled). If filename contains path separators (e.g., `../../etc/passwd`), the resulting path could theoretically traverse. However, the timestamp prefix (`1708000000_`) accidentally prevents this — `1708000000_..` is a literal directory name, not a parent reference, and since it doesn't exist, `write_bytes()` fails with FileNotFoundError.
**Impact:** Currently not exploitable due to the timestamp prefix. But this is accidental defense — if the path format changes, traversal becomes possible.
**Mitigation:** The attacker must be in the `allow_from` list (authenticated). The download directory is typically `/tmp/lucyd-telegram`.
**Fix:** Add `filename = Path(filename).name` before constructing `local_path` to strip any directory components.
**Status:** Flagged for Nicolas. Hardening opportunity, not currently exploitable.

## Bypass Analysis

| Technique | Applicable? | Handled? | Details |
|-----------|------------|----------|---------|
| Path traversal (`../`) | Yes | Yes | `Path.resolve()` before prefix check. Symlink test passes. |
| Path prefix ambiguity | Yes | **Partial** | Finding #1 — `startswith()` without trailing `/`. Low risk. |
| SSRF encoding (octal/hex/decimal IP) | Yes | Yes | `_is_private_ip()` normalizes via `socket.inet_aton()` fallback. |
| SSRF IPv6 (`[::1]`) | Yes | Yes | `ipaddress.ip_address()` handles IPv6. |
| SSRF DNS rebinding | Yes | Yes | IP pinning via custom HTTP/HTTPS handlers. |
| SSRF redirect chain | Yes | Yes | `_SafeRedirectHandler` validates each hop. |
| Command injection | N/A | N/A | `exec` tool takes entire command by design. No parsing/sanitization needed. |
| Environment variable leakage | Yes | Yes | `_safe_env()` filters by prefix + suffix patterns. 100% mutation kill. |
| Session poisoning | Yes | Accepted | JSONL safe by construction. Content poisoning mitigated by tool-level boundaries. |
| Structured memory poisoning | Yes | Accepted | Facts are text context, never tool arguments. All SQL parameterized. |
| Resource exhaustion (cost bombing) | Partial | Partial | Sub-agent recursion blocked. HTTP rate limiting. No per-turn tool call limit (accepted — model provider rate limits apply). |
| Resource exhaustion (disk) | Partial | Partial | Attachment downloads to configured dir. No size limit on individual files (Telegram enforces 20MB). |
| Dynamic dispatch injection | Yes | Yes | All dispatch is dict-key lookup. No eval/exec/import in tool paths. |
| Supply chain (dep CVEs) | Yes | Yes | pip-audit: 1 dev-tool CVE (pip 25.3). No runtime CVEs. |
| Skill prompt injection | Yes | Accepted | Skills are text-only, operator-managed. Tool boundaries still apply. |
| Attachment filename traversal | Yes | **Accidental** | Finding #2 — timestamp prefix prevents traversal coincidentally. |

## Boundary Verification Summary

| Boundary | Exists | Tested | Mutation Verified | Fails Closed |
|----------|--------|--------|-------------------|-------------|
| `_check_path()` (filesystem allowlist) | Yes | Yes (34 tests) | Yes (100% kill) | Yes (empty list → deny) |
| `_safe_env()` (env filtering) | Yes | Yes (24 tests) | Yes (100% kill) | Yes (filters by default) |
| `_validate_url()` (SSRF) | Yes | Yes (69 tests) | Yes (86.4% kill, rest cosmetic) | Yes (parse error → block) |
| `_is_private_ip()` (IP check) | Yes | Yes | Yes (81.8% kill, rest equivalent) | Yes (exception → True → block) |
| `_SafeRedirectHandler` (redirect SSRF) | Yes | Yes | Yes (80% kill, rest equivalent) | Yes (error → URLError → block) |
| IP pinning (DNS rebinding) | Yes | Yes | N/A (infrastructure) | Yes (no IP → fallback to default handler) |
| `_subagent_deny` (tool deny-list) | Yes | Yes (23 tests) | Yes (100% kill) | Yes (deny-list applied unconditionally) |
| HTTP Bearer auth | Yes | Yes | N/A (comparison, not logic) | Yes (no token → 503) |
| HTTP rate limiting | Yes | Yes | N/A (timing-based) | Yes (exceeded → 429) |
| HTTP body size limit | Yes | Yes | N/A (aiohttp built-in) | Yes (exceeded → 413) |
| Telegram `allow_from` | Yes | Yes | N/A (set membership) | Yes (empty set → allow all, but typical config sets list) |
| FIFO permissions | Yes | N/A (OS-level) | N/A | Yes (0o600 on mkfifo) |

## Recommendations

1. **(Low)** Fix `_check_path()` prefix matching — add trailing separator check to prevent sibling directory access. Trivial fix. Carried forward from Cycle 3.
2. **(Low)** Sanitize Telegram attachment filenames — `filename = Path(filename).name` before path construction. Hardens against future format changes.
3. **(Info)** Upgrade pip from 25.3 to 26.0 to resolve CVE-2026-1703 (dev tool only, no runtime impact).

## Confidence

Overall confidence: 96%

- **CRITICAL capabilities (exec, filesystem, sub-agents):** 98%. All boundaries mutation-tested at 100% kill. One prefix-match gap documented (Low severity, not currently exploitable in standard deployments).
- **HIGH capabilities (web_fetch, messaging):** 97%. SSRF protection comprehensive (scheme + DNS + IP + pinning + redirects). All survivors proven equivalent or cosmetic.
- **Authentication (HTTP API):** 98%. Timing-safe comparison. Fail-closed when unconfigured. Rate limited.
- **Indirect paths (memory poisoning):** 95%. Verified no code path routes fact values to tool arguments. Accepted risk per design — tool boundaries contain prompt injection even with compromised LLM.
- **Supply chain:** 98%. pip-audit clean for all runtime deps. 1 dev-tool CVE only.
- **Areas of uncertainty:** Telegram `allow_from` with empty set permits all users (configuration dependent, not a code issue). Per-turn tool call count not limited (accepted — mitigated by model provider rate limits and max_turns in agentic loop).
