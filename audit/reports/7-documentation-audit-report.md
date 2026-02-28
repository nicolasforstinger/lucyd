# Documentation Audit Report

**Date:** 2026-02-28
**Audit Cycle:** 11
**EXIT STATUS:** PASS

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-007 (test count drift) | FIXED | CLAUDE.md Code Structure section said `~1472 tests`, Testing section said `~1489`. Actual: 1489. Internal inconsistency fixed. README.md per-module test counts stale (177/109/231 → 190/137/278). Fixed. |
| P-008 (new module without docs) | PASS | No new source modules since cycle 10. All 31 modules (incl. `__init__.py`) documented in architecture.md. Two new features (quote reply extraction, auto-close system sessions) are undocumented in CLAUDE.md — noted as findings. |
| P-011 (config-to-doc label consistency) | PASS | All model references match: `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`, `text-embedding-3-small` consistent across `providers.d/*.toml`, CLAUDE.md, README.md, docs/. |
| P-024 (HTTP endpoint completeness) | PASS | All 9 endpoints documented with full request/response schemas in `docs/operations.md`. Rate limit groups, status codes, and field types all documented. |

## Source Inventory

| Category | Count | Verified Against |
|----------|-------|------------------|
| Tools | 19 (across 11 TOOLS-exporting modules + 1 utility: indexer.py) | `TOOLS` lists in `tools/*.py` |
| Channels | 3 (telegram, cli, http_api) | `channels/*.py` |
| Providers | 2 (anthropic-compat, openai-compat) | `providers/*.py` |
| CLI utilities | 5 (lucyd-send, lucyd-index, lucyd-consolidate, lucyd-evolve, audit-deps) | `bin/` |
| Config sections | 14 top-level + sub-sections | `config.py` |
| HTTP endpoints | 9 | `channels/http_api.py` |
| Source modules | 31 (including 3 `__init__.py`) | All .py files excl tests/venv/mutants |
| Source lines | ~9,600 | `wc -l` |
| Test files | 37 (35 `test_*.py` + conftest.py + `__init__.py`) | `tests/*.py` |
| Test functions | 1489 | `pytest --collect-only -q` |

## Files Audited

| File | Issues Found |
|------|--------------|
| CLAUDE.md | 1 (internal test count inconsistency: Code Structure said ~1472, Testing said ~1489) |
| README.md | 4 (per-module test counts stale: telegram 177→190, http_api 109→137, orchestrator 231→278, cli.py 46→48 lines) |
| docs/architecture.md | 0 |
| docs/operations.md | 0 |
| docs/configuration.md | 0 |
| docs/diagrams.md | 8 (stale line number references from agentic.py code shifts + session.py + anthropic_compat.py) |
| lucyd.toml.example | 0 |
| .env.example | 0 |
| workspace.example/TOOLS.md | 0 |
| providers.d/*.toml.example | 0 |

## Discrepancies Found & Fixed

| # | File | Issue | Fix Applied |
|---|------|-------|-------------|
| 1 | CLAUDE.md line 266 | Code Structure: `~1472 tests` (stale, inconsistent with Testing section's `~1489`) | Updated to `~1489 tests` |
| 2 | README.md line 138 | `telegram.py` said 177 tests | Updated to 190 |
| 3 | README.md line 138 | `http_api.py` said 109 tests | Updated to 137 |
| 4 | README.md line 138 | Orchestrator said 231 tests | Updated to 278 |
| 5 | README.md line 138 | `cli.py` said 46 lines | Updated to 48 |
| 6 | docs/diagrams.md Diagram 1 | `agentic.py:107` (Agentic Loop) | Updated to `agentic.py:125` |
| 7 | docs/diagrams.md Diagram 1 | `session.py:450` (Compact Session) | Updated to `session.py:440` |
| 8 | docs/diagrams.md Diagram 2 | `agentic.py:107` (run_agentic_loop) | Updated to `agentic.py:125` |
| 9 | docs/diagrams.md Diagram 2 | `agentic.py:162` (provider.complete) | Updated to `agentic.py:179` |
| 10 | docs/diagrams.md Diagram 2 | `agentic.py:61` (record cost) | Updated to `agentic.py:79` |
| 11 | docs/diagrams.md Diagram 2 | `agentic.py:235` (asyncio.gather) | Updated to `agentic.py:253` |
| 12 | docs/diagrams.md Diagram 3 | `anthropic_compat.py:79` (format_system) | Updated to `anthropic_compat.py:83` |
| 13 | docs/diagrams.md Diagram 7 | `agentic.py:231` (Dispatch) | Updated to `agentic.py:249` |

## Cross-Reference Check

| Check | Status | Details |
|-------|--------|---------|
| Tool counts consistent | PASS | 19 in CLAUDE.md, README, TOOLS.md, architecture.md |
| Tool names consistent | PASS | All 19 names match across docs |
| Env vars consistent | PASS | .env.example = configuration.md |
| Config sections consistent | PASS | lucyd.toml.example = configuration.md |
| HTTP endpoints consistent | PASS | 9 endpoints across CLAUDE.md, architecture.md, operations.md |
| Model names consistent | PASS | IDs match across providers.d/ and all docs |
| File references valid | PASS | All paths in docs exist |
| Test counts consistent | PASS (after fix) | 1489 in both CLAUDE.md sections, README |
| Source module count | PASS | 31 in CLAUDE.md (includes `__init__.py`), verified |
| Memory schema table count | PASS | "11 tables (4+4+2+1)" consistent across CLAUDE.md and source |
| Diagram line numbers | PASS (after fix) | All 28 file:line references in diagrams.md verified against source |
| Per-module test counts | PASS (after fix) | telegram 190, http_api 137, orchestrator 278 in README |

## Noted (Not Fixed — Documentation Gaps)

| Item | Severity | Details |
|------|----------|---------|
| Quote reply extraction undocumented | LOW | `telegram.py` extracts `reply_to_message` context, passes as `quote` on `InboundMessage`, daemon injects as `[replying to: ...]` prefix. Not mentioned in CLAUDE.md Telegram section, architecture.md, or README features list. Feature works correctly; just undocumented. |
| Auto-close system sessions undocumented | LOW | `lucyd.py` auto-closes sessions for `source == "system"` after processing (line 1113-1118). Prevents lingering one-shot sessions from evolution/heartbeat/notify. Not documented in CLAUDE.md Sessions section or operations.md System Message Behavior. |
| `lucyd-send --attach` flag not in CLAUDE.md | LOW | The `-a, --attach` flag is documented in `docs/operations.md` (line 80) but missing from CLAUDE.md's CLI flag table. Pre-existing gap. |
| Cron examples omit `--config` flag | LOW | CLAUDE.md cron examples show full paths with `--config` for `lucyd-evolve` but consolidate entries pre-dating evolution don't all include it. Default resolution works via `LUCYD_CONFIG` env var. Pre-existing. |

## Previously Missing Documentation — Now Resolved

All 5 items from cycle 10's "Missing Documentation" list are now present in `docs/configuration.md`:

| Feature | Status | Where |
|---------|--------|-------|
| `[documents]` config section | RESOLVED | `docs/configuration.md` line 441 |
| `[stt] audio_label`, `audio_fail_msg` | RESOLVED | `docs/configuration.md` lines 414-415 |
| `[http] max_body_bytes` | RESOLVED | `docs/configuration.md` line 109 |
| `[tools] subagent_model`, `subagent_max_turns`, `subagent_timeout` | RESOLVED | `docs/configuration.md` lines 346-348 |
| `[memory.consolidation] max_extraction_chars` | RESOLVED | `docs/configuration.md` line 234 |

## Comparison with Cycle 10

| Metric | Cycle 10 | Cycle 11 | Change |
|--------|----------|----------|--------|
| Discrepancies found | 3 | 13 | +10 (8 diagram line numbers, 4 test counts, 1 internal inconsistency) |
| Discrepancies fixed | 3 | 13 | All fixed |
| Cross-reference checks | 12 (all PASS) | 12 (all PASS after fix) | Stable |
| Missing documentation (LOW) | 5 | 4 (2 new features + 2 pre-existing) | Resolved 5, gained 4 new |

## Verification

Tests not re-run for doc-only changes (no source code modified). All 1489 tests passed in Stage 2 of this audit cycle.

## Confidence

Overall confidence: 97%

The diagram line number drift (8 of 13 findings) came from the "full codebase audit" commit (`e8677a0`) which restructured `agentic.py` but did not update diagram references. All line numbers now verified against current source. Two new features (quote reply, auto-close) are functional but undocumented — LOW severity since they have no config surface and work transparently. All 5 previously missing config documentation items from cycle 10 have been resolved in `docs/configuration.md`.
