# Documentation Audit Report

**Date:** 2026-03-09
**Audit Cycle:** 17
**Mode:** Research only (no files edited)
**EXIT STATUS:** PARTIAL

---

## Count Verification

| Metric | CLAUDE.md Claim | Actual | Status |
|--------|----------------|--------|--------|
| Test functions | ~1725 | 1725 | PASS |
| Source modules | 34 | 33 (.py files in source tree) | DRIFT — off by 1 |
| Source lines | ~10,147 | 10,233 | DRIFT — grew +86 lines |
| Tool count | 19 built-in across 11 modules | 19 across 11 | PASS |
| Test files | 40 | 38 test_*.py + conftest.py + __init__.py = 40 total | PASS (ambiguous) |
| Diagrams | 8 | 8 | PASS |
| Test-to-source ratio | ~2.5:1 | ~2.64:1 | DRIFT (minor) |

---

## Pattern Checks

| Pattern | Result |
|---------|--------|
| P-007 (test count drift) | PASS — README says ~1725, CLAUDE.md says ~1725, actual is 1725 |
| P-008 (new module undocumented) | PASS — no new source modules since last audit |
| P-011 (config/doc label consistency) | PASS — model names match between providers.d/ and docs |

---

## Documentation Accuracy Matrix

| File | Discrepancies Found | Severity |
|------|-------------------|----------|
| CLAUDE.md | 3 | Low–Medium |
| README.md | 0 | — |
| docs/architecture.md | 0 | — |
| docs/operations.md | 5 | Medium–High |
| docs/configuration.md | 0 | — |
| docs/diagrams.md | ~10 line number drifts | Low |

---

## Findings

### F-01: `operations.md` — Missing `--status` and `--log` from flag table (Medium)

**Location:** `/home/lucy/lucyd/docs/operations.md` lines 74-96

The `lucyd-send` flag table is missing two flags that exist in the actual CLI:
- `--status` — Daemon status (pid, uptime, model, sessions, cost)
- `--log [N]` — Last N lines of daemon log (default: 20)

Both are documented in CLAUDE.md (lines 332, 337) and in the `lucyd-send` script docstring, but `operations.md` — the primary reference for operators — omits them from both the usage examples (lines 38-72) and the flag table (lines 76-95).

### F-02: `operations.md` — Stale `tier` field in `/chat` endpoint (Medium)

**Location:** `/home/lucy/lucyd/docs/operations.md` line 257

The `/api/v1/chat` request fields table still lists:
```
| `tier` | no | `"full"` | Context tier override |
```

Context tiers were removed in the 2026-03-06 single-provider refactoring. The `tier` field does not exist in `channels/http_api.py`. This is a stale reference.

### F-03: `operations.md` — Stale model override in evolve description (Low)

**Location:** `/home/lucy/lucyd/docs/operations.md` line 846

States: `Sends a system message to the daemon FIFO with "model": "primary" override (ensures Sonnet, not Haiku)`

The multi-model routing was removed. The evolve FIFO message no longer sends a `"model"` key. The parenthetical "(ensures Sonnet, not Haiku)" references a retired concept.

### F-04: `operations.md` — `/compact` missing from rate limit group table (Low)

**Location:** `/home/lucy/lucyd/docs/operations.md` line 195

The Standard rate limit group lists: `/chat`, `/notify`, `/sessions/reset`, `/evolve`

Missing: `/compact`. Per `http_api.py`, `/compact` is not in `_READ_ONLY_PATHS`, so it uses the standard rate limiter.

### F-05: `operations.md` — Missing `--status` and `--log` from usage examples (Low)

**Location:** `/home/lucy/lucyd/docs/operations.md` lines 38-72

The bash usage examples section shows send, cost, sessions, and reset examples but no `--status` or `--log` examples.

### F-06: CLAUDE.md — Source module count drift (Low)

**Location:** `/home/lucy/CLAUDE.md` line 300

Claims "Source modules | 34 (~10,147 lines)". Actual: 33 source .py files totaling 10,233 lines. Off by 1 module and +86 lines.

### F-07: CLAUDE.md — HTTP route list in channel table incomplete (Low)

**Location:** `/home/lucy/CLAUDE.md` line 113

The inline route list in the Channels table omits `/evolve` and `/compact`:
```
REST (`/api/v1/chat`, `/notify`, `/status`, `/sessions`, `/cost`, `/monitor`, `/sessions/reset`, `/sessions/{id}/history`)
```

The full endpoint table at lines 175-186 is complete. Only the inline summary is stale.

### F-08: CLAUDE.md — Test-to-source ratio drift (Low)

**Location:** `/home/lucy/CLAUDE.md` line 303

Claims "~2.5:1 (lines)". Actual: 27,030 test lines / 10,233 source lines = ~2.64:1.

### F-09: `docs/diagrams.md` — Line number drift in lucyd.py, session.py, memory.py, context.py (Low)

**Location:** `/home/lucy/lucyd/docs/diagrams.md` (multiple)

Several line number references in Mermaid diagram labels have drifted due to code changes:

| Diagram Reference | Claimed | Actual | Delta |
|---|---|---|---|
| `asyncio.Queue lucyd.py:296` | 296 | 339 | +43 |
| `_message_loop lucyd.py:1523` | 1523 | 1546 | +23 |
| `_process_message lucyd.py:665` | 665 | 715 | +50 |
| `Registration lucyd.py:378` | 378 | 412 | +34 |
| `get_or_create session.py:288` | 288 | 296 | +8 |
| `_save_state session.py:167` | 167 | 168 | +1 |
| `compact_session session.py:443` | 443 | 449 | +6 |
| `close_session session.py:326` | 326 | 334 | +8 |
| `_embed memory.py:168` | 168 | 166 | -2 |
| `_vector_search memory.py:128` | 128 | 127 | -1 |
| `lookup_facts memory.py:379` | 379 | 374 | -5 |
| `search_episodes memory.py:414` | 414 | 409 | -5 |
| `get_open_commitments memory.py:446` | 446 | 441 | -5 |
| `inject_recall memory.py:548` | 548 | 537 | -11 |
| `ContextBuilder.build context.py:31` | 31 | 29 | -2 |

Line numbers for `agentic.py`, `tools/__init__.py`, `synthesis.py`, and `anthropic_compat.py` are still correct.

---

## New Features Check (Since 2026-03-06)

| Feature | CLAUDE.md | README | architecture.md | operations.md | configuration.md |
|---------|-----------|--------|-----------------|---------------|-----------------|
| `primary_sender` routing | Documented | Documented | Documented | Flag table: yes | Documented |
| `passive_notify_refs` telemetry | Documented | Documented | Documented | Not in flag table (N/A — config-only) | Documented |
| `--status` flag | Documented | Not mentioned | N/A | **MISSING from flag table** | N/A |
| `--log` flag | Documented | Not mentioned | N/A | **MISSING from flag table** | N/A |
| `MUTMUT_RUNNING` env var | N/A | N/A | N/A | N/A | N/A (internal) |
| Compaction token limit in prompt | Documented (CLAUDE.md) | N/A | N/A | Documented (config docs) | Documented |

---

## HTTP API Endpoint Verification

| Endpoint | Source | CLAUDE.md Table | architecture.md | operations.md |
|----------|--------|----------------|-----------------|---------------|
| POST `/chat` | Yes | Yes | Yes | Yes |
| POST `/notify` | Yes | Yes | Yes | Yes |
| GET `/status` | Yes | Yes | Yes | Yes |
| GET `/sessions` | Yes | Yes | Yes | Yes |
| GET `/cost` | Yes | Yes | Yes | Yes |
| GET `/monitor` | Yes | Yes | Yes | Yes |
| POST `/sessions/reset` | Yes | Yes | Yes | Yes |
| GET `/sessions/{id}/history` | Yes | Yes | Yes | Yes |
| POST `/evolve` | Yes | Yes | Yes | Yes |
| POST `/compact` | Yes | Yes | Yes | Yes (but missing from rate limit group) |

---

## CLI Flag Verification

| Flag | Source | CLAUDE.md | operations.md |
|------|--------|-----------|---------------|
| `-m, --message` | Yes | Yes | Yes |
| `-s, --system` | Yes | Yes | Yes |
| `-n, --notify` | Yes | Yes | Yes |
| `--evolve` | Yes | Yes | Yes |
| `--compact` | Yes | Yes | Yes |
| `--reset` | Yes | Yes | Yes |
| `--status` | Yes | Yes | **MISSING** |
| `--cost` | Yes | Yes | Yes |
| `--sessions` | Yes | Yes | Yes |
| `--monitor` | Yes | Yes | Yes |
| `--history` | Yes | Yes | Yes |
| `--log` | Yes | Yes | **MISSING** |
| `--from` | Yes | Yes | Yes |
| `--source` | Yes | Yes | Yes |
| `--ref` | Yes | Yes | Yes |
| `--data` | Yes | Yes | Yes |
| `--force` | Yes | Yes | Yes |
| `--full` | Yes | Yes | Yes |
| `-a, --attach` | Yes | Yes | Yes |
| `--state-dir` | Yes | Yes | Yes |

---

## Cross-Reference Check

| Check | Status |
|-------|--------|
| Tool counts consistent across docs | PASS — 19 tools, 11 modules everywhere |
| Endpoint tables consistent | PARTIAL — CLAUDE.md line 113 inline list missing `/evolve`, `/compact`; tables are complete |
| CLI flags consistent | FAIL — operations.md missing `--status` and `--log` |
| Config keys consistent | PASS |
| Features documented | PASS — primary_sender and passive_notify_refs documented in all relevant files |
| File references valid | PASS — GROWERS.md, all module paths exist |
| Source counts consistent | FAIL — CLAUDE.md source modules 34 vs actual 33, lines 10,147 vs 10,233 |

---

## Summary of Required Fixes

### Priority 1 — Factually wrong (copy-paste would fail)
1. `operations.md` line 257: Remove `tier` field from `/chat` request fields table

### Priority 2 — Missing content
2. `operations.md` flag table: Add `--status` and `--log` flags
3. `operations.md` usage examples: Add `--status` and `--log` examples
4. `operations.md` rate limit table line 195: Add `/compact` to Standard group

### Priority 3 — Stale content
5. `operations.md` line 846: Remove `"model": "primary"` override reference and Sonnet/Haiku parenthetical
6. CLAUDE.md line 300: Update source modules 34 → 33, lines ~10,147 → ~10,233
7. CLAUDE.md line 113: Add `/evolve` and `/compact` to inline HTTP route list
8. CLAUDE.md line 303: Update test-to-source ratio ~2.5:1 → ~2.6:1

### Priority 4 — Low severity
9. `docs/diagrams.md`: Update 15 drifted line number references across lucyd.py, session.py, memory.py, context.py

---

## Confidence

92% — all claims traced to source code. No ambiguity on findings. Diagram line numbers are cosmetic but add up to 15 drifted references. The `operations.md` omissions (F-01, F-02) are the most user-impacting since operators rely on that file.

**EXIT STATUS: PARTIAL** — No factual errors in README, architecture, or configuration docs. operations.md has one stale field (tier) and two missing CLI flags. CLAUDE.md has minor count drift. No fixes applied (research-only mode).
