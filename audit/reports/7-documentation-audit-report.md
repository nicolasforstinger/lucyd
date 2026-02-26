# Documentation Audit Report

**Date:** 2026-02-26
**Audit Cycle:** 9
**EXIT STATUS:** PASS

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-007 (test count drift) | FIXED | README said 1394, actual is 1460. CLAUDE.md said ~1401. Both fixed. |
| P-008 (new module without docs) | FIXED | Three new HTTP endpoints (`/monitor`, `/sessions/reset`, `/sessions/{id}/history`) missing from endpoint tables. Two new CLI flags (`--history`, `--full`) missing from flag tables. |
| P-011 (config-to-doc label consistency) | PASS | Model names, provider config, deny-list all consistent. No drift. |
| P-020 (config-to-default parity) | PASS | No new config keys without documentation since cycle 8. |
| P-021 (provider split) | PASS | No provider-specific values in `lucyd.toml.example`. Clean. |

## Source Inventory

| Category | Count | Verified Against |
|----------|-------|------------------|
| Tools | 19 (across 12 tool-exporting modules) | `TOOLS` lists in `tools/*.py` |
| Channels | 3 (telegram, cli, http_api) | `channels/*.py` |
| Providers | 2 (anthropic-compat, openai-compat) | `providers/*.py` |
| CLI utilities | 4 (lucyd-send, lucyd-index, lucyd-consolidate, audit-deps) | `bin/` |
| Config sections | 14 top-level + sub-sections | `config.py` |
| HTTP endpoints | 8 (+3 from cycle 8) | `channels/http_api.py` |
| Source modules | 30 (~9,377 lines) | All .py files excl tests/venv/mutants |
| Test files | 34 (+2 from cycle 8) | `tests/test_*.py` |
| Test functions | 1460 (+66 from cycle 8) | `pytest --collect-only -q` |

## Files Audited

| File | Issues Found |
|------|--------------|
| CLAUDE.md | 4 (test count, test files, endpoint table, CLI flags) |
| README.md | 1 (test count 1394 → 1460) |
| docs/architecture.md | 1 (endpoint table missing 3 entries) |
| docs/operations.md | 2 (CLI flag table missing --history/--full, endpoint docs missing 3) |
| docs/configuration.md | 0 |
| docs/diagrams.md | 0 |
| lucyd.toml.example | 0 |
| .env.example | 0 |
| workspace.example/TOOLS.md | 0 |
| workspace.example/ (all) | 0 |
| providers.d/*.toml.example | 0 |
| lucyd.service.example | 0 |

## Discrepancies Found & Fixed

| # | File | Issue | Fix Applied |
|---|------|-------|-------------|
| 1 | README.md line 110 | `1394 tests` | Updated to `1460 tests` |
| 2 | CLAUDE.md line 108 | Channel table only lists 5 HTTP endpoints | Added `/monitor`, `/sessions/reset`, `/sessions/{id}/history` |
| 3 | CLAUDE.md lines 170-174 | Endpoint table only lists 5 endpoints | Added 3 new endpoint rows |
| 4 | CLAUDE.md line 257 | `~1401 tests` in tree | Updated to `~1460 tests` |
| 5 | CLAUDE.md line 317 | `Test files: 33` | Updated to `34` |
| 6 | CLAUDE.md line 318 | `Test functions: ~1401` | Updated to `~1460` |
| 7 | CLAUDE.md line 353 | CLI flag table missing `--history` and `--full` | Added both flags |
| 8 | docs/architecture.md lines 149-153 | Endpoint table only lists 5 endpoints | Added 3 new endpoint rows |
| 9 | docs/operations.md lines 68-79 | CLI flag table missing `--history` and `--full` | Added both flags |
| 10 | docs/operations.md lines 197-199 | HTTP endpoint section missing monitor, reset, history | Added documentation for 3 new endpoints |

## Cross-Reference Check

| Check | Status | Details |
|-------|--------|---------|
| Tool counts consistent | PASS | 19 in CLAUDE.md, README, TOOLS.md, architecture.md |
| Tool names consistent | PASS | All 19 names match across docs |
| Env vars consistent | PASS | .env.example = configuration.md |
| Config sections consistent | PASS | lucyd.toml.example = configuration.md |
| HTTP endpoints consistent | PASS (after fix) | 8 endpoints across CLAUDE.md, architecture.md, operations.md |
| CLI flags consistent | PASS (after fix) | 12 flags in CLAUDE.md and operations.md |
| Model names consistent | PASS | IDs match across providers.d/ and all docs |
| File references valid | PASS | All paths in docs exist |
| Test counts consistent | PASS (after fix) | 1460 in CLAUDE.md, README |
| Source module count | PASS | 30 in CLAUDE.md, verified |
| Memory schema table count | PASS | "10 tables (4+4+2)" consistent |

## Missing Documentation (Carried Forward)

| Feature | Source | Status |
|---------|--------|--------|
| `[documents]` config section (4 keys) | `config.py` | Exists in `lucyd.toml.example` but missing from `docs/configuration.md`. LOW. |
| `[stt] audio_label`, `audio_fail_msg` | `config.py` | Missing from `docs/configuration.md`. LOW. |
| `[http] max_body_bytes` | `config.py` | Missing from `docs/configuration.md`. Documented in CLAUDE.md. LOW. |
| `[tools] subagent_model`, `subagent_max_turns`, `subagent_timeout` | `config.py` | Missing from `docs/configuration.md`. Documented in CLAUDE.md. LOW. |
| `[memory.consolidation] max_extraction_chars` | `config.py` | Missing from all docs. LOW. |

All missing items remain LOW severity — documented in CLAUDE.md or have sensible defaults.

## Comparison with Cycle 8

| Metric | Cycle 8 | Cycle 9 | Change |
|--------|---------|---------|--------|
| Discrepancies found | 10 | 10 | Same count, all new (previous all resolved) |
| Discrepancies fixed | 10 | 10 | All fixed |
| Cross-reference checks | 11 (all PASS) | 11 (all PASS after fix) | Stable |
| Missing documentation | 6 LOW | 5 LOW | -1 (`bin/audit-deps` documented) |

## Verification

Tests re-run after documentation fixes. All 1460 tests pass. No accidental source changes.

## Confidence

Overall confidence: 96%

All factual errors fixed (10 discrepancies). The primary drift pattern is the HTTP parity feature adding 3 new endpoints and 2 new CLI flags without updating documentation tables. Cross-references consistent across all docs after fix. Five missing documentation items carried forward — all LOW severity with defaults.
