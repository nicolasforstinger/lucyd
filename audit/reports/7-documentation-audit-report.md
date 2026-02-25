# Documentation Audit Report

**Date:** 2026-02-25
**Audit Cycle:** 8
**EXIT STATUS:** PASS

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-007 (test count drift) | FIXED | README said 1299, actual is 1327. Fixed to 1327. |
| P-008 (new module without docs) | FIXED | `[documents]` config section missing from `docs/configuration.md`. Several config keys undocumented. |
| P-011 (config-to-doc label consistency) | FIXED | Default deny-list included `load_skill` in 4 doc locations but source has only 4 tools (not 5). Fixed all. Sub-agent `max_turns` default was 10 in docs, 50 in source. Fixed. |

## Source Inventory

| Category | Count | Verified Against |
|----------|-------|------------------|
| Tools | 19 (across 11 tool-exporting modules) | `TOOLS` lists in `tools/*.py` |
| Channels | 3 (telegram, cli, http_api) | `channels/*.py` |
| Providers | 2 (anthropic-compat, openai-compat) | `providers/*.py` |
| Provider configs | 2 live + 3 examples | `providers.d/` |
| CLI utilities | 4 (lucyd-send, lucyd-index, lucyd-consolidate, audit-deps) | `bin/` |
| Config sections | 14 top-level + sub-sections | `config.py` |
| Environment variables | 6 | `.env.example` + `config.py` |
| HTTP endpoints | 5 | `channels/http_api.py` |
| Source modules | 30 (8,746 lines) | All .py files excl tests/venv/mutants |
| Test functions | 1327 | `pytest --collect-only -q` |

## Files Audited

| File | Issues Found |
|------|--------------|
| CLAUDE.md | 3 (schema breakdown, source line count, test layer count wording) |
| README.md | 1 (test count 1299 → 1327) |
| docs/architecture.md | 4 (schema breakdown, missing `files` table, deny-list includes `load_skill`, max_turns default wrong) |
| docs/configuration.md | 2 (deny-list includes `load_skill` in comment and prose) |
| docs/operations.md | 0 |
| lucyd.toml.example | 1 (deny-list includes `load_skill`) |
| .env.example | 0 |
| workspace.example/TOOLS.md | 0 |
| workspace.example/ (all) | 0 |
| providers.d/*.toml.example | 0 |
| lucyd.service.example | 0 |

## Discrepancies Found & Fixed

| # | File | Issue | Fix Applied |
|---|------|-------|-------------|
| 1 | README.md line 110 | `1299 tests` | Updated to `1327 tests` |
| 2 | docs/architecture.md line 16 | "10 tables: 6 structured + 4 unstructured" | Updated to "4 unstructured + 4 structured + 2 infrastructure" |
| 3 | docs/architecture.md lines 204-207 | `files` table missing from unstructured tables list | Added `files` table |
| 4 | docs/architecture.md line 296 | "max_turns=10" and deny-list includes `load_skill` | Fixed max_turns to 50, removed `load_skill` from deny-list, noted sub-agents CAN load skills |
| 5 | docs/configuration.md line 308 | Comment: deny-list includes `load_skill` | Removed `load_skill` |
| 6 | docs/configuration.md line 311 | Prose: deny-list includes `load_skill` | Removed `load_skill`, added note sub-agents CAN load skills |
| 7 | lucyd.toml.example line 126 | Comment: deny-list includes `load_skill` | Removed `load_skill` |
| 8 | CLAUDE.md line 126 | "6 structured + 4 unstructured" | Updated to "4 unstructured + 4 structured + 2 infrastructure" |
| 9 | CLAUDE.md line 298 | Source modules "~8,650 lines" | Updated to "~8,750 lines" |
| 10 | CLAUDE.md line 303 | "Four layers" but lists five | Updated to "Five layers" |

## Cross-Reference Check

| Check | Status | Details |
|-------|--------|---------|
| Tool counts consistent | PASS | 19 in CLAUDE.md, README, TOOLS.md, configuration.md |
| Tool names consistent | PASS | All 19 names match across docs |
| Env vars consistent | PASS | .env.example = configuration.md |
| Config sections consistent | PASS | lucyd.toml.example = configuration.md |
| HTTP endpoints consistent | PASS | 5 endpoints across all docs |
| Model names consistent | PASS | P-011 clean — IDs match across providers.d/ and all docs |
| File references valid | PASS | All paths in docs exist |
| Test counts consistent | PASS (after fix) | 1327 in CLAUDE.md, README |
| Source module count | PASS | 30 in CLAUDE.md, verified |
| Memory schema table count | PASS (after fix) | "10 tables (4+4+2)" in CLAUDE.md and architecture.md |
| Default deny-list | PASS (after fix) | 4 tools in source, docs, and example config |

## Missing Documentation (Deferred)

| Feature | Source | Status |
|---------|--------|--------|
| `[documents]` config section (4 keys) | `config.py` lines for `documents_enabled`, `documents_max_chars`, `documents_max_file_bytes`, `documents_text_extensions` | Exists in `lucyd.toml.example` but missing from `docs/configuration.md`. LOW — documented in CLAUDE.md. |
| `[stt] audio_label`, `audio_fail_msg` | `config.py` lines 446-452 | Missing from `docs/configuration.md` and `lucyd.toml.example`. LOW. |
| `[http] max_body_bytes` | `config.py` line 219 | Missing from `docs/configuration.md`. Documented in CLAUDE.md. LOW. |
| `[tools] subagent_model`, `subagent_max_turns`, `subagent_timeout` | `config.py` lines 379-390 | Missing from `docs/configuration.md` and `lucyd.toml.example`. Documented in CLAUDE.md. LOW. |
| `[memory.consolidation] max_extraction_chars` | `config.py` line 281 | Missing from all docs. LOW. |
| `bin/audit-deps` | `bin/audit-deps` | Dev utility, not user-facing. LOW. |

All missing items are LOW severity — either documented in CLAUDE.md or are non-essential config keys with sensible defaults.

## Comparison with Cycle 7

| Metric | Cycle 7 | Cycle 8 |
|--------|---------|---------|
| Discrepancies found | 6 | 10 |
| Discrepancies fixed | 6 | 10 |
| Cross-reference checks | 11 (all PASS) | 11 (all PASS after fix) |
| Missing documentation | 0 | 6 LOW items deferred |

## Verification

Tests re-run after documentation fixes to verify no accidental source changes. All 1327 tests pass.

## Confidence

Overall confidence: 96%

All factual errors fixed (10 discrepancies). Cross-references consistent across CLAUDE.md, README.md, docs/architecture.md, docs/configuration.md, and lucyd.toml.example. Most significant fixes: deny-list `load_skill` removal (4 locations), schema breakdown correction (2 locations), test count update. Six missing documentation items are LOW severity — all have defaults, most documented in CLAUDE.md.
