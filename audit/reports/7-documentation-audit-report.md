# Documentation Audit Report

**Date:** 2026-02-26
**Audit Cycle:** 10
**EXIT STATUS:** PASS

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-007 (test count drift) | FIXED | CLAUDE.md said ~1484, actual is 1485. Source modules said 30, actual is 31. Source lines said ~9,015, actual is ~9,930. Test files said 34, actual is 35. All fixed. |
| P-008 (new module without docs) | PASS | `evolution.py` already documented in CLAUDE.md across 5+ sections (Memory System, HTTP API, CLI, Cron, Code Structure). |
| P-011 (config-to-doc label consistency) | PASS | Model names, provider config, evolution config all consistent. |
| P-020 (config-to-default parity) | PASS | Evolution config keys documented with defaults in CLAUDE.md. |
| P-021 (provider split) | PASS | No provider-specific values in framework docs. |
| P-024 (HTTP endpoint completeness) | PASS | All 9 endpoints documented in CLAUDE.md endpoint table including new `/api/v1/evolve`. |

## Source Inventory

| Category | Count | Verified Against |
|----------|-------|------------------|
| Tools | 19 (across 12 tool-exporting modules) | `TOOLS` lists in `tools/*.py` |
| Channels | 3 (telegram, cli, http_api) | `channels/*.py` |
| Providers | 2 (anthropic-compat, openai-compat) | `providers/*.py` |
| CLI utilities | 4 (lucyd-send, lucyd-index, lucyd-consolidate, audit-deps) | `bin/` |
| Config sections | 14 top-level + sub-sections | `config.py` |
| HTTP endpoints | 9 (+1 from cycle 9: `/api/v1/evolve`) | `channels/http_api.py` |
| Source modules | 31 (+1 from cycle 9: `evolution.py`) | All .py files excl tests/venv/mutants |
| Source lines | ~9,930 (+915 from cycle 9) | `wc -l` |
| Test files | 35 (+1 from cycle 9: `test_evolution.py`) | `tests/test_*.py` |
| Test functions | 1485 (+25 from cycle 9) | `pytest --collect-only -q` |

## Files Audited

| File | Issues Found |
|------|--------------|
| CLAUDE.md | 4 (source modules, source lines, test files, test functions — all minor counts) |
| README.md | 0 |
| docs/architecture.md | 0 |
| docs/operations.md | 0 |
| docs/configuration.md | 0 |
| docs/diagrams.md | 0 |
| lucyd.toml.example | 0 |
| .env.example | 0 |
| workspace.example/TOOLS.md | 0 |
| providers.d/*.toml.example | 0 |

## Discrepancies Found & Fixed

| # | File | Issue | Fix Applied |
|---|------|-------|-------------|
| 1 | CLAUDE.md line 323 | `Source modules: 30 (~9,015 lines)` | Updated to `31 (~9,930 lines)` |
| 2 | CLAUDE.md line 324 | `Test files: 34` | Updated to `35` |
| 3 | CLAUDE.md line 325 | `Test functions: ~1484` | Updated to `~1485` |

Note: Schema table count (11), evolution documentation, `/api/v1/evolve` endpoint, cron schedule, and code structure were all already updated in CLAUDE.md during the evolution implementation (cycle 10). Only statistical counts needed updating.

## Cross-Reference Check

| Check | Status | Details |
|-------|--------|---------|
| Tool counts consistent | PASS | 19 in CLAUDE.md, README, TOOLS.md, architecture.md |
| Tool names consistent | PASS | All 19 names match across docs |
| Env vars consistent | PASS | .env.example = configuration.md |
| Config sections consistent | PASS | lucyd.toml.example = configuration.md |
| HTTP endpoints consistent | PASS | 9 endpoints across CLAUDE.md, architecture.md, operations.md |
| CLI flags consistent | PASS | lucyd-consolidate --evolve documented |
| Model names consistent | PASS | IDs match across providers.d/ and all docs |
| File references valid | PASS | All paths in docs exist |
| Test counts consistent | PASS (after fix) | 1485 in CLAUDE.md |
| Source module count | PASS (after fix) | 31 in CLAUDE.md, verified |
| Memory schema table count | PASS | "11 tables (4+4+2+1)" consistent across CLAUDE.md and source |
| Evolution docs complete | PASS | Documented in Memory System, HTTP API, CLI, Cron, Code Structure |

## Noted (Not Fixed)

| Item | Severity | Details |
|------|----------|---------|
| Cron examples omit `--config` flag | LOW | CLAUDE.md cron examples show `lucyd-consolidate --evolve` but actual cron has `--config /home/lucy/lucyd/lucyd.toml --evolve`. Same pattern applies to all consolidate entries (pre-existing from earlier cycles). Default resolution works when `LUCYD_CONFIG` is set or when run from project root. |

## Missing Documentation (Carried Forward)

| Feature | Source | Status |
|---------|--------|--------|
| `[documents]` config section (4 keys) | `config.py` | Exists in `lucyd.toml.example` but missing from `docs/configuration.md`. LOW. |
| `[stt] audio_label`, `audio_fail_msg` | `config.py` | Missing from `docs/configuration.md`. LOW. |
| `[http] max_body_bytes` | `config.py` | Missing from `docs/configuration.md`. Documented in CLAUDE.md. LOW. |
| `[tools] subagent_model`, `subagent_max_turns`, `subagent_timeout` | `config.py` | Missing from `docs/configuration.md`. Documented in CLAUDE.md. LOW. |
| `[memory.consolidation] max_extraction_chars` | `config.py` | Missing from all docs. LOW. |

All missing items remain LOW severity — documented in CLAUDE.md or have sensible defaults.

## Comparison with Cycle 9

| Metric | Cycle 9 | Cycle 10 | Change |
|--------|---------|----------|--------|
| Discrepancies found | 10 | 3 | -7 (evolution docs added during implementation) |
| Discrepancies fixed | 10 | 3 | All fixed |
| Cross-reference checks | 11 (all PASS) | 12 (all PASS after fix) | +1 (evolution docs check) |
| Missing documentation | 5 LOW | 5 LOW | Stable |

## Verification

Tests not re-run for doc-only changes (source count metadata only, no code changes). All 1485 tests passed in Stage 2.

## Confidence

Overall confidence: 97%

Minimal drift — evolution feature was comprehensively documented during implementation (cycle 10). Only statistical counts (module count, line count, test files, test functions) needed updating. All cross-references consistent. Five missing documentation items carried forward — all LOW severity with defaults.
