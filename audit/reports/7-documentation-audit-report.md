# Documentation Audit Report

**Date:** 2026-03-12
**Audit Cycle:** 18
**EXIT STATUS:** PASS

## Source Inventory

| Category | Count |
|----------|-------|
| Tools | 19 (across 11 modules) |
| Channels | 3 (telegram, cli, http_api) |
| Providers | 2 (anthropic_compat, openai_compat) |
| Config sections | 17 top-level TOML sections |
| Environment variables | 6 (in .env.example) |
| CLI utilities | 4 (lucyd-send, lucyd-index, lucyd-consolidate, audit-deps) |
| Source modules | 33 (~10,263 lines, excluding plugins.d) |
| Test functions | 1721 (38 test files) |

## Files Audited

| File | Status |
|------|--------|
| README.md (152 lines) | Fixed — test counts updated |
| docs/architecture.md (365 lines) | Clean |
| docs/configuration.md (550 lines) | Fixed — missing config keys added |
| docs/operations.md (914 lines) | Clean |
| docs/diagrams.md (364 lines) | Clean |
| workspace.example/TOOLS.md (31 lines) | Clean — 19 tools, matches source |
| lucyd.toml.example (245→290 lines) | Fixed — 40+ missing required keys added |
| .env.example (10 lines) | Clean |
| lucyd.service.example (38 lines) | Clean |
| providers.d/anthropic.toml.example | Clean |
| providers.d/openai.toml.example | Clean |
| providers.d/vision.toml.example | Clean |

## Discrepancies Found

| File | Issue | Fix Applied |
|------|-------|-------------|
| README.md:113 | Test count "~1725" — actual 1721 | Updated to "~1721" |
| README.md:140 | HTTP API "145 tests" — actual 143 | Updated to "143 tests" |
| README.md:140 | Orchestrator "283 tests" — actual 297 | Updated to "297 tests" |
| lucyd.toml.example | 40+ config keys missing or commented out that `_require()` needs present | All keys uncommented with generic defaults |
| lucyd.toml.example | No context budget documentation (P-031) | Added comment block explaining system prompt vs context window |
| configuration.md | Missing `[http] token_env`, `download_dir`, `rate_limit_cleanup_threshold` | Added |
| configuration.md | Missing `[memory] vector_search_limit`, `fts_min_results` | Added |
| configuration.md | Missing `[behavior] queue_capacity`, `queue_poll_interval`, `quote_max_chars`, `sqlite_timeout`, `telemetry_max_age` | Added |
| configuration.md | Missing `[behavior.compaction] warning_pct`, `min_messages`, `keep_recent_pct_min/max`, `tool_result_max_chars`, `prompt`, `diary_prompt` | Added |
| configuration.md | Missing `[logging] suppress` | Added |
| configuration.md | Missing `[vision] caption_max_chars` | Added |
| configuration.md | Missing `[tools] plugins_dir`, `[tools.web_search] api_key_env` | Added |

## Critical Finding: lucyd.toml.example Incomplete

The Stage 1 `_require()` conversion changed all config property access from `_deep_get()` (returns default on missing) to `_require()` (raises ConfigError on missing). However, the lucyd.toml.example was not updated to include all required keys. A new deployment copying the example would crash on startup with ConfigError for dozens of missing keys.

**Root cause:** Stage 1 static analysis updated config.py but not the example file.
**Fix applied:** All required keys now present (uncommented) in lucyd.toml.example with generic framework defaults.
**Carried to Stage 8:** Consider reverting tunable parameters to `_deep_get()` with built-in defaults — `_require()` is correct for truly required values (agent.name, channel.type, models.primary) but overly strict for optional behavior tuning (queue_capacity, warning_pct, etc.).

## Cycle 17 Findings Resolution

Findings from Cycle 17 (F-01 through F-09) were resolved:
- F-01/F-05 (missing --status/--log in operations.md flag table): Resolved in Cycle 17 fix pass.
- F-02 (stale `tier` field in /chat endpoint): Resolved — removed.
- F-03 (stale model override in evolve description): Resolved — removed.
- F-04 (/compact missing from rate limit table): Resolved — added.
- F-06/F-07/F-08 (CLAUDE.md count drift): Resolved — CLAUDE.md updated.
- F-09 (diagram line number drift): Accepted — cosmetic, tracked in diagrams.md header.

## Pattern Checks

| Pattern | Result |
|---------|--------|
| P-007 test count drift | Fixed — README updated from ~1725 to ~1721 |
| P-008 new module without docs | PASS — all source modules documented in architecture.md |
| P-011 config-to-doc label consistency | PASS — model strings match between docs and provider files |
| P-020 config-to-default parity | Fixed — lucyd.toml.example now includes all required keys |
| P-021 provider split | PASS — framework vs provider settings correctly separated |
| P-024 HTTP endpoint completeness | PASS — all 10 endpoints documented with full schemas |
| P-031 context budget documentation | Fixed — added context budget comment to lucyd.toml.example |
| P-032 default documentation for tunables | PASS — all parameters have inline comments |

## Cross-Reference Check

| Check | Status |
|-------|--------|
| Tool counts consistent | PASS — README, TOOLS.md, architecture.md all say 19 |
| Tool names consistent | PASS — same 19 names in all files |
| Env vars consistent | PASS — .env.example matches configuration.md |
| Config sections consistent | PASS — lucyd.toml.example matches configuration.md |
| Features documented | PASS — all user-facing features in README |
| Provider examples consistent | PASS — providers.d/*.toml.example matches configuration.md |
| Project structure consistent | PASS — README structure matches actual layout |
| File references valid | PASS — all referenced paths exist |

## Known Gaps

| Gap | Severity | Status |
|-----|----------|--------|
| `_require()` over-strictness for tunable parameters | Medium | CARRIED to Stage 8 |

## Confidence

95% — all discrepancies fixed, cross-references verified consistent, lucyd.toml.example now complete. Test suite passes (1721/1721). Remaining uncertainty: `_require()` design decision carried to remediation.
