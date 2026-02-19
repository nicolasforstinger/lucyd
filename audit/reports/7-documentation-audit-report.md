# Documentation Audit Report

**Date:** 2026-02-19
**EXIT STATUS:** PASS

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-007 (test count drift) | CLEAN | README claims 1020 tests; actual count 1020. Subtotals sum correctly: 770 + 50 + 54 + 48 + 98 = 1020. |
| P-008 (new module without docs) | FIXED | 3 modules missing from architecture.md Module Map: `memory_schema.py`, `consolidation.py`, `tools/structured_memory.py`. 2 CLI utilities missing: `bin/lucyd-index`, `bin/lucyd-consolidate`. All added. |
| P-011 (config-to-doc label consistency) | CLEAN | Model IDs verified consistent across all docs: `claude-sonnet-4-6` (primary), `claude-haiku-4-5-20251001` (subagent), `text-embedding-3-small` (embeddings). No stale aliases. |

## Source Inventory

| Category | Count |
|----------|-------|
| Tools | 19 |
| Channels | 3 (Telegram, HTTP API, CLI) |
| Providers | 2 (Anthropic, OpenAI-compatible) |
| Config sections | 14 |
| Environment variables | 6 |
| CLI utilities | 4 (lucyd-send, lucyd-index, lucyd-consolidate, audit-deps) |
| Production modules | 29 |
| Test functions | 1020 |

## Files Audited

| File | Lines | Issues |
|------|-------|--------|
| README.md | 144 | 1 (tool count + module list) |
| docs/architecture.md | 310 | 3 (missing modules, missing tables, missing CLIs) |
| docs/configuration.md | 318 | 2 (missing tools in enabled list, missing consolidation config) |
| docs/operations.md | 383 | 1 (missing cron jobs) |
| lucyd.toml.example | 133 | 2 (missing tools, missing consolidation config) |
| workspace.example/TOOLS.md | 26 | 1 (missing Memory v2 tools) |
| .env.example | 10 | 0 |
| lucyd.service.example | 38 | 0 |
| providers.d/anthropic.toml.example | 26 | 0 |
| providers.d/openai.toml.example | 8 | 0 |
| workspace.example/*.md (7 files) | 65 | 0 |

## Discrepancies Found

| File | Line | Issue | Fix Applied |
|------|------|-------|-------------|
| README.md | 102 | "16 agent tools" — actual count is 19 | Changed to "19 agent tools"; added `memory_schema.py`, `consolidation.py` to module list; added HTTP API to channels |
| docs/architecture.md | Module Map | Missing `memory_schema.py`, `consolidation.py`, `tools/structured_memory.py` | Added 3 module entries |
| docs/architecture.md | Module Map | Missing `bin/lucyd-index`, `bin/lucyd-consolidate` | Added 2 CLI utility entries |
| docs/architecture.md | 194–198 | Tables section only lists v1 tables (chunks, chunks_fts, embedding_cache); missing 6 v2 tables | Added structured memory tables section |
| docs/configuration.md | 210–217 | `[tools] enabled` list missing `memory_write`, `memory_forget`, `commitment_update` | Added 3 tools |
| docs/configuration.md | — | No `[memory.consolidation]` or `[memory.maintenance]` sections | Added both sections with config examples |
| docs/operations.md | 298–308 | Cron table missing `lucyd-consolidate` (hourly :15) and `lucyd-consolidate --maintain` (daily 04:00) | Added 2 cron entries |
| lucyd.toml.example | 76–85 | `[tools] enabled` list missing Memory v2 tools | Added `memory_write`, `memory_forget`, `commitment_update` |
| lucyd.toml.example | 70–71 | No `[memory.consolidation]` section | Added consolidation and maintenance config |
| workspace.example/TOOLS.md | — | Listed 16 tools; missing `memory_write`, `memory_forget`, `commitment_update` | Added "Memory Management" section with 3 tools |

**Root cause:** All 10 discrepancies trace to Memory v2 (structured memory) being added on Feb 19, 2026. Source code and CLAUDE.md were updated; `docs/` directory and example files were not.

## Cross-Reference Check

| Check | Status |
|-------|--------|
| Tool counts consistent (README = TOOLS.md = config = source) | PASS (all 19) |
| Env vars consistent (.env.example = configuration.md) | PASS (6 vars) |
| Config keys consistent (lucyd.toml.example = configuration.md) | PASS |
| Features documented (README features = actual capabilities) | PASS |
| File references valid (all mentioned paths exist) | PASS |
| Model names consistent (provider files = docs) | PASS |
| Test count consistent (README = actual) | PASS (1020) |

## Fixes Applied

1. **README.md** — Updated tool count from 16 to 19; added `memory_schema.py` and `consolidation.py` to module list; added HTTP API to channels list.

2. **docs/architecture.md** — Added 3 missing modules to Module Map (`memory_schema.py`, `consolidation.py`, `tools/structured_memory.py`). Added 2 missing CLI utilities (`bin/lucyd-index`, `bin/lucyd-consolidate`). Added Memory v2 structured tables to Memory section.

3. **docs/configuration.md** — Added `memory_write`, `memory_forget`, `commitment_update` to `[tools] enabled`. Added `[memory.consolidation]` and `[memory.maintenance]` config sections.

4. **docs/operations.md** — Added `lucyd-consolidate` (hourly at :15) and `lucyd-consolidate --maintain` (daily at 04:00) to cron table.

5. **lucyd.toml.example** — Added Memory v2 tools to enabled list. Added `[memory.consolidation]` config section.

6. **workspace.example/TOOLS.md** — Added "Memory Management" section with `memory_write`, `memory_forget`, `commitment_update`.

## Missing Documentation

None — all user-facing features now documented.

## Verification

All 1020 tests pass after documentation changes (no source code modified in this stage).

## Confidence

Overall confidence: 97%
- All factual claims verified against source code
- Cross-references consistent across all 11 audited files
- Memory v2 documentation gap was the only systematic issue; now fully addressed
- Minor uncertainty: some `docs/operations.md` cron entries (trash cleanup, DB integrity) are recommendations, not verified live cron entries. Acceptable — labeled as "Recommended cron jobs."
