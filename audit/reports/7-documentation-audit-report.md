# Documentation Audit Report

**Date:** 2026-02-23
**Audit Cycle:** 6
**EXIT STATUS:** PASS

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-007 (test count drift) | FIXED | CLAUDE.md said ~1187, README said 1207, actual is 1232. Fixed both. |
| P-008 (new module without docs) | CLEAN | All source modules documented. Tool count 19 matches across all docs. No new modules since Cycle 5. |
| P-011 (config-to-doc label consistency) | CLEAN | Model IDs consistent across providers.d/ and all docs: `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`, `text-embedding-3-small`. |

## Source Inventory

| Category | Count | Verified Against |
|----------|-------|------------------|
| Tools | 19 (across 11 tool-exporting modules) | `TOOLS` lists in `tools/*.py` |
| Channels | 3 (telegram, cli, http_api) | `channels/*.py` |
| Providers | 2 (anthropic-compat, openai-compat) | `providers/*.py` |
| Provider configs | 3 examples | `providers.d/*.toml.example` |
| CLI utilities | 3 (lucyd-send, lucyd-index, lucyd-consolidate) | `bin/` |
| Config sections | 14 top-level + sub-sections | `config.py` |
| Environment variables | 6 | `.env.example` + `config.py` |
| HTTP endpoints | 5 | `channels/http_api.py` |
| Source modules | 29 (8,135 lines) | All .py files excl tests/venv/mutants |
| Test functions | 1232 | `pytest --collect-only -q` |

## Files Audited

| File | Issues Found |
|------|--------------|
| CLAUDE.md | 5 (test count ×2, module count, line count, body size, ratio) |
| README.md | 1 (test count) |
| docs/architecture.md | 0 |
| docs/configuration.md | 0 |
| docs/operations.md | 0 |
| .env.example | 0 |
| lucyd.toml.example | 0 |
| workspace.example/TOOLS.md | 0 |
| providers.d/*.toml.example | 0 |

## Discrepancies Found & Fixed

| # | File | Issue | Fix Applied |
|---|------|-------|-------------|
| 1 | CLAUDE.md line 239 | `~1187 tests` | Updated to `~1232 tests` |
| 2 | CLAUDE.md line 295 | `~1187, all passing` | Updated to `~1232, all passing` |
| 3 | CLAUDE.md line 293 | `30 (~7,500 lines)` source modules | Updated to `29 (~8,100 lines)` |
| 4 | CLAUDE.md line 168 | `Body size capped at 1 MiB` | Updated to `10 MiB` (matches code default) |
| 5 | CLAUDE.md line 296 | `~2.2:1` test-to-source ratio | Updated to `~2.4:1` |
| 6 | README.md line 110 | `1207 tests` | Updated to `1232 tests` |

## Cross-Reference Check

| Check | Status | Details |
|-------|--------|---------|
| Tool counts consistent | PASS | 19 in CLAUDE.md, README, TOOLS.md, configuration.md |
| Tool names consistent | PASS | All 19 names match across docs |
| Env vars consistent | PASS | .env.example = configuration.md |
| Config sections consistent | PASS | lucyd.toml.example = configuration.md |
| HTTP endpoints consistent | PASS | 5 endpoints across all docs |
| Model names consistent | PASS | P-011 clean |
| File references valid | PASS | All paths in docs exist |
| Test counts consistent | PASS (after fix) | 1232 in CLAUDE.md, README, audit reports |

## Non-Doc Finding from Cycle 5

Finding #1 (missing table creation) from Cycle 5's Stage 7 was **resolved by the hardening batch** — `ensure_schema()` in `memory_schema.py` now creates all 10 tables including unstructured ones. Confirmed in Stage 5 dependency chain audit.

## Missing Documentation

| Feature | Documented? | Status |
|---------|-------------|--------|
| Plugin system (`plugins.d/`) | CLAUDE.md only | Carried from Cycle 4. Not in public docs. Low priority (empty directory). |
| `api_retries` / `api_retry_base_delay` config | Not in configuration.md | New from hardening batch. Low priority. |

## Confidence

Overall confidence: 96%

All factual errors fixed. Cross-references consistent. Test counts verified via pytest. Module counts verified via source enumeration. HTTP body size confirmed against code default.
