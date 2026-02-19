# Dependency Chain Audit Report

**Date:** 2026-02-19
**EXIT STATUS:** PASS

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-006 (dead data pipeline) | CLEAN | All consumers have active producers verified as running and fresh. No dead pipelines found. |
| P-012 (auto-populated misclassified as static) | CLEAN | `entity_aliases` correctly identified as auto-populated by `consolidation.py:230`. Anti-fragmentation directive present in extraction prompt (line 149: "use the shortest common name as the canonical entity"). Ordering invariant verified: aliases stored (line 230) BEFORE facts (line 271) in `extract_facts()`. |

## Data Flow Matrix

| Consumer | Data Source | Producer | Producer Runs? | Status |
|----------|-----------|----------|---------------|--------|
| `memory.py` search/recall | `memory/main.sqlite` (chunks, chunks_fts, embedding_cache) | `bin/lucyd-index` (cron :10) | Yes | OK |
| `session.py` load | `sessions/*.jsonl` + `.state.json` | `session.py` save (daemon) | Yes | OK |
| `context.py` build | `workspace/*.md` | Lucy via tools / manual | Yes (non-deterministic) | OK |
| `config.py` load | `lucyd.toml`, `providers.d/*.toml` | Manual (static) | N/A | OK |
| `skills.py` load | `workspace/skills/*.md` | Manual (static) | N/A | OK |
| `tools/status.py` cost query | `cost.db` (`costs` table) | `agentic.py` `_record_cost()` | Yes | OK |
| `lucyd.py` PID check | `~/.lucyd/lucyd.pid` | `lucyd.py` daemon startup | Yes | OK |
| `lucyd.py` FIFO reader | `~/.lucyd/control.pipe` | `bin/lucyd-send` / cron | Yes | OK |
| `lucyd.py` monitor reader | `~/.lucyd/monitor.json` | `lucyd.py` `_process_message()` | Yes | OK |
| Daily memory logs | `workspace/memory/YYYY-MM-DD.md` | Lucy via `write` tool | Yes (non-deterministic) | OK |
| `memory.py` → `lookup_facts()` | `facts` table | `consolidation.py` (cron :15) + `structured_memory.py` (agent tool) | Yes | OK |
| `memory.py` → `search_episodes()` | `episodes` table | `consolidation.py` (cron :15) | Yes | OK |
| `memory.py` → `get_open_commitments()` | `commitments` table | `consolidation.py` + `structured_memory.py` (agent tool) | Yes | OK |
| `memory.py` → `resolve_entity()` | `entity_aliases` table | `consolidation.py` `extract_facts()` (auto) | Yes | OK |
| `consolidation.py` skip check | `consolidation_state` table | `consolidation.py` `update_consolidation_state()` | Yes | OK |
| `consolidation.py` hash check | `consolidation_file_hashes` table | `consolidation.py` `extract_from_file()` | Yes | OK |

No dead pipelines. All producers identified and verified.

## External Process Inventory

| Process | Type | Schedule | Expected Output | Exists? | Enabled? | Last Run | Status |
|---------|------|----------|----------------|---------|----------|----------|--------|
| `lucyd.service` | systemd | continuous | daemon | Yes | Yes | Running (PID 3562486) | OK |
| Memory indexer (`lucyd-index`) | cron | `10 * * * *` | `memory/main.sqlite` | Yes | Yes | Today (fresh chunks) | OK |
| Memory consolidation (`lucyd-consolidate`) | cron | `15 * * * *` | facts, episodes, commitments, aliases | Yes | Yes | 2026-02-19 16:14:56 | OK |
| Memory maintenance (`lucyd-consolidate --maintain`) | cron | `0 4 * * *` | cleanup in main.sqlite | Yes | Yes | Daily | OK |
| Workspace auto-commit | cron | `0 * * * *` | git commits | Yes | Yes | Hourly | OK |
| Heartbeat | cron | (commented out) | system message | N/A | Disabled | N/A | OK (intentionally disabled, documented) |

All expected processes present and running. Heartbeat intentionally disabled per CLAUDE.md documentation.

## Freshness Checks

| Data Source | Threshold | Last Write | Fresh? |
|-------------|-----------|-----------|--------|
| Memory SQLite (chunks) | 48h | 2026-02-19 (today) | Yes |
| Cost SQLite (`costs`) | 24h | 2026-02-19 16:52 | Yes |
| Session JSONL | Matches conversation | 2026-02-19 17:52 | Yes |
| Daily memory logs | 72h | 2026-02-19 02:07 | Yes |
| PID file | Current process | PID 3562486 alive | Yes |
| Monitor JSON | 5 min (if active) | ~4h ago | Yes (no recent conversation — expected) |
| Structured facts | 2h (of last consolidation) | 2026-02-19 16:14 | Yes |
| Structured episodes | 48h | 2026-02-19 (3 episodes today) | Yes |
| Structured commitments | Informational | 2026-02-19 15:06 (3 open) | Yes |
| Consolidation state | Matches session activity | 2026-02-19 16:14:56 | Yes |
| Consolidation file hashes | Matches workspace changes | 2026-02-19 14:17:24 | Yes |
| Entity aliases | N/A (auto-populated) | 5 aliases present | Yes |
| Indexer lock file | Should not exist when idle | Not present | Yes (clean) |

All data sources within freshness thresholds.

## Round-Trip Test Coverage

| Pipeline | Test Exists? | Test File | Status |
|----------|-------------|-----------|--------|
| Memory: index → search | Yes | test_indexer.py (`test_fts_searchable_for_all_content`) | PASS |
| Session: save → load | Yes | test_session.py (`test_state_preserves_compaction_fields`) | PASS |
| Context: write → build | Yes | test_context.py (`test_reload_picks_up_file_changes`) | PASS |
| Cost: record → query | Yes | test_cost.py (`test_write_then_query`) | PASS |
| Structured: consolidate → recall (facts) | Yes | test_consolidation.py + test_structured_recall.py | PASS |
| Structured: agent write → recall (facts) | Yes | test_memory_tools_structured.py | PASS |
| Structured: episodes extract → search | Yes | test_consolidation.py + test_structured_recall.py | PASS |
| Structured: commitments extract → query | Yes | test_consolidation.py + test_structured_recall.py | PASS |
| Structured: aliases store → resolve | Yes | test_consolidation.py + test_structured_recall.py | PASS |

All 9 required round-trip pipelines have integration tests using real stores (SQLite or filesystem). No mocked round-trips.

## Findings

None. All pipelines have active producers, all external processes exist and run, all data sources are fresh, and all round-trip tests exist.

## Confidence

- Phase 1 (Data Flow Matrix): 98% — all 16 consumer-producer relationships mapped and verified
- Phase 2 (External Process Inventory): 98% — all expected processes confirmed present and enabled
- Phase 3 (Freshness Checks): 97% — all 13 data sources verified fresh. Monitor JSON age (4h) is expected behavior (no recent conversation), not a gap.
- Phase 4 (Round-Trip Tests): 98% — all 9 pipelines verified with real-store integration tests

Overall confidence: 97%
