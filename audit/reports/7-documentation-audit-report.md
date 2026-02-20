# Documentation Audit Report

**Date:** 2026-02-20
**EXIT STATUS:** PASS
**Triggered by:** Memory v2 wiring tests (Stage 4) + full 7-stage audit

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-007 (test count drift) | FOUND & FIXED | README had 1075, actual is 1085 (+10 from Memory v2 wiring tests added in Stage 4). Contract tests ~50 → ~60. Orchestrator total 168 → 186. |
| P-008 (new module without docs) | CLEAN | All 17 source modules documented in architecture.md module map. No new undocumented modules. |
| P-011 (config-to-doc label consistency) | CLEAN | Model IDs verified consistent across all docs: `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`, `text-embedding-3-small`. Labels (Haiku, Sonnet) match in operations.md and configuration.md. |

## Source Inventory

Built from source code, not from existing docs.

| Category | Count | Verified Against |
|----------|-------|------------------|
| Tools | 19 (across 11 modules) | `tools/__init__.py` registry + all `tools/*.py` |
| Channels | 3 (telegram, cli, http_api) | `channels/*.py` |
| Providers | 2 (anthropic-compat, openai-compat) | `providers/*.py` |
| Models | 4 (primary, subagent, compaction, embeddings) | `providers.d/*.toml` |
| CLI utilities | 3 user-facing (lucyd-send, lucyd-index, lucyd-consolidate) | `bin/` |
| Config sections | 14 top-level + sub-sections | `config.py` property definitions |
| Environment variables | 6 | `config.py` + `.env.example` |
| Test functions | 1085 | `python -m pytest tests/ -q` (16.35s, all pass) |

## Files Audited

| File | Lines | Issues Found |
|------|-------|--------------|
| README.md | 145 | 3 (test count, contract test count, orchestrator test count) |
| docs/architecture.md | 342 | 0 |
| docs/configuration.md | 397 | 2 (missing recall personality section, wrong priority order) |
| docs/operations.md | 385 | 1 (heartbeat cron minute field) |
| lucyd.toml.example | 169 | 2 (missing vision routing, missing recall personality config) |

## Discrepancies Found & Fixed

| File | Line(s) | Issue | Fix Applied |
|------|---------|-------|-------------|
| README.md | 106 | Test count "1075" — actual is 1085 | Updated to 1085 |
| README.md | 126 | Contract tests listed as ~50 — actual is ~60 (10 Memory v2 wiring tests added in Stage 4) | Updated to ~60 |
| README.md | 133 | Orchestrator tests "168" — actual is 186 | Updated to 186 |
| docs/operations.md | 323 | Heartbeat cron example shows `0 8 * * *` — actual crontab and CLAUDE.md say `5 8 * * *` | Fixed minute field to `5` |
| docs/configuration.md | 315-327 | `[memory.recall]` section missing `max_episodes_at_start` key (exists in config.py with default 3) | Added key with description |
| docs/configuration.md | 327 | Priority order stated as "commitments > facts > episodes > vector" — source defaults are commitments (40) > vector (35) > episodes (25) > facts (15) | Fixed priority order |
| docs/configuration.md | (new) | Missing entire `[memory.recall.personality]` subsection — 7 config keys exist in config.py | Added subsection with all keys and defaults |
| lucyd.toml.example | 61-65 | `[routing]` section missing `vision = "primary"` key | Added with comment |
| lucyd.toml.example | 85-89 | `[memory.recall]` missing `max_episodes_at_start` and `[memory.recall.personality]` | Added key + commented personality section |

**Root cause:** Discrepancies trace to two changes: (1) Memory v2 wiring tests added 10 tests in Stage 4 but README/CLAUDE.md not updated, (2) recall personality config (`[memory.recall.personality]`) added in Memory v2 implementation but docs/example not updated.

## Cross-Reference Check

| Check | Status | Details |
|-------|--------|---------|
| Tool counts (README vs source) | PASS | 19 = 19 |
| Test counts (README vs actual) | PASS | 1085 = 1085 (after fix) |
| Test layer breakdown sums to total | PASS | 845 + 60 + 54 + 48 + 78 = 1085 |
| Env vars (.env.example vs config.py) | PASS | 6 = 6 |
| Config keys (lucyd.toml.example vs configuration.md) | PASS | All sections present after fixes |
| Config defaults (configuration.md vs config.py) | PASS | All recall defaults verified against source |
| Model names (providers.d vs docs) | PASS | All 4 model names consistent |
| Cron schedules (operations.md vs CLAUDE.md) | PASS | All 7 cron jobs match after heartbeat fix |
| Feature list (README vs actual capabilities) | PASS | All 16 listed features exist in source |

## Fixes Applied

**README.md** (3 edits):
- Test count: 1075 → 1085
- Contract tests: ~50 → ~60
- Orchestrator tests: 168 → 186

**docs/operations.md** (1 edit):
- Heartbeat cron: `0 8 * * *` → `5 8 * * *`

**docs/configuration.md** (3 additions):
- Added `max_episodes_at_start = 3` to `[memory.recall]` section
- Fixed recall priority order to: commitments > vector > episodes > facts
- Added `[memory.recall.personality]` subsection with 7 config keys and defaults

**lucyd.toml.example** (3 additions):
- Added `vision = "primary"` to `[routing]` section
- Added `max_episodes_at_start = 3` to `[memory.recall]` section
- Added commented `[memory.recall.personality]` section with all 7 keys

## Verification

All 1085 tests pass after documentation changes. No source code was modified in this stage.

## Confidence

Overall confidence: 97%

- Test counts: verified via `pytest -q` (1085 passed)
- Config keys: every `[memory.recall]` and `[memory.recall.personality]` key traced to `config.py` property definitions (lines 268-301)
- Priority order: verified against `config.py:284-297` defaults (commitments=40, vector=35, episodes=25, facts=15)
- Cron schedules: operations.md verified against CLAUDE.md crontab
- Cross-reference: all 9 consistency checks pass after fixes
- Known limitation: docs/ may have minor drift from CLAUDE.md on non-feature topics (CLAUDE.md is source of truth per Working Principles #14)
