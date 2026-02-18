# Documentation Audit Report

**Date:** 2026-02-18
**EXIT STATUS:** PASS

## Pattern Checks

- **P-007:** Test count in README ("916 tests") matches actual (`916 collected`). Layer breakdown (~750 + ~50 + ~35 + ~80 = ~915) consistent with approximate totals.
- **P-008:** All 26 source modules documented in `docs/architecture.md` module map. No undocumented modules found. Cron jobs match `docs/operations.md` table.
- **P-011:** Model names in docs now match config. Fixed 3 stale `claude-sonnet-4-5-20250929` references (2 in configuration.md, 1 in operations.md) to `claude-sonnet-4-6`. Also fixed stale `thinking_mode` example (budgeted → adaptive) and `max_tokens` (16384 → 65536) in configuration.md.

## Source Inventory

| Category | Count |
|----------|-------|
| Tools | 16 |
| Channels | 3 (telegram, http_api, cli) |
| Providers | 2 (anthropic_compat, openai_compat) |
| Provider configs | 2 (anthropic.toml, openai.toml) + 2 examples |
| Config sections | 12 (agent, agent.context, agent.context.tiers, agent.skills, channel, channel.telegram, http, providers, routing, memory, tools, behavior, paths) |
| Environment variables | 6 (LUCYD_ANTHROPIC_KEY, LUCYD_TELEGRAM_TOKEN, LUCYD_OPENAI_KEY, LUCYD_BRAVE_KEY, LUCYD_ELEVENLABS_KEY, LUCYD_HTTP_TOKEN) |
| CLI utilities | 3 (lucyd-send, lucyd-index, audit-deps) |
| Workspace example files | 8 (SOUL.md, USER.md, MEMORY.md, IDENTITY.md, AGENTS.md, HEARTBEAT.md, TOOLS.md, skills/example-skill/SKILL.md) |

## Files Audited

| File | Lines | Status |
|------|-------|--------|
| README.md | 143 | PASS |
| docs/architecture.md | 311 | PASS |
| docs/configuration.md | 319 | FIXED (3 discrepancies) |
| docs/operations.md | 383 | FIXED (1 discrepancy) |
| lucyd.toml.example | 133 | PASS |
| .env.example | 10 | PASS |
| lucyd.service.example | 38 | PASS |
| providers.d/anthropic.toml.example | 26 | PASS |
| providers.d/openai.toml.example | 9 | PASS |
| workspace.example/TOOLS.md | 26 | PASS |

## Discrepancies Found

| File | Line | Issue | Fix Applied |
|------|------|-------|-------------|
| docs/configuration.md | 122 | Model `claude-sonnet-4-5-20250929` → actual is `claude-sonnet-4-6` | Updated to `claude-sonnet-4-6` |
| docs/configuration.md | 125 | `max_tokens = 16384` → actual is `65536` | Updated to `65536` |
| docs/configuration.md | 128-129 | `thinking_mode = "budgeted"` + `thinking_budget = 10000` → actual is `thinking_mode = "adaptive"` (no budget) | Updated to `adaptive`, removed budget line |
| docs/configuration.md | 162 | Model example `claude-sonnet-4-5-20250929` | Updated to `claude-sonnet-4-6` |
| docs/operations.md | 107 | Monitor example `Model: claude-sonnet-4-5-20250929` | Updated to `claude-sonnet-4-6` |

## Cross-Reference Check

| Check | Status |
|-------|--------|
| Tool counts consistent (README = TOOLS.md = configuration.md = source) | PASS (16 everywhere) |
| Tool names consistent across files | PASS |
| Env vars consistent (.env.example = configuration.md) | PASS (6 vars) |
| Config sections consistent (lucyd.toml.example = configuration.md) | PASS |
| Features documented (README features = actual) | PASS |
| Provider examples consistent (providers.d/*.example = configuration.md) | PASS (after fix) |
| Project structure consistent (README = actual layout) | PASS |
| File path references valid | PASS |
| Model names consistent (config = docs = examples) | PASS (after fix) |

## Fixes Applied

**docs/configuration.md (3 fixes):**
- Updated primary model example from `claude-sonnet-4-5-20250929` to `claude-sonnet-4-6`
- Updated `max_tokens` from `16384` to `65536`
- Changed `thinking_mode` from `"budgeted"` (with budget 10000) to `"adaptive"` (no budget)
- Updated model example in options table from `claude-sonnet-4-5-20250929` to `claude-sonnet-4-6`

**docs/operations.md (1 fix):**
- Updated monitor example model from `claude-sonnet-4-5-20250929` to `claude-sonnet-4-6`

## Missing Documentation

None. All 16 tools documented. All channels documented. All CLI utilities documented. All config sections documented. All env vars documented.

## Confidence

Overall confidence: 95%

All doc files audited line-by-line against source. All discrepancies fixed. Cross-references verified. Test suite passes (916/916) after doc edits confirming no accidental source changes.
