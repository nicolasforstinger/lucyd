# Dependency Chain Audit Report

**Date:** 2026-02-25
**Audit Cycle:** 8
**EXIT STATUS:** PASS

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-006 (dead data pipeline) | PASS | All 13 pipelines have active producers. No consumer without a writer. |
| P-012 (auto-populated misclassified) | PASS | `entity_aliases` correctly classified as auto-populated by `consolidation.py:extract_facts()`. Anti-fragmentation directive present in extraction prompt. Ordering invariant (aliases BEFORE facts) preserved. |

## Data Flow Matrix

| Consumer | Data Source | Producer | Producer Runs? | Status |
|----------|-----------|----------|---------------|--------|
| `memory.py` search/recall | `main.sqlite` (chunks, chunks_fts) | `bin/lucyd-index` (cron :10) | Yes | HEALTHY |
| `memory.py` embeddings | `main.sqlite` (embedding_cache) | `bin/lucyd-index` (cron :10) | Yes | HEALTHY |
| `memory_schema.py` | `main.sqlite` (all 10 tables) | `ensure_schema()` in daemon + indexer | Yes | HEALTHY |
| `session.py` load | `sessions/*.jsonl` + `.state.json` | `session.py` save (daemon) | Yes | HEALTHY |
| `context.py` build | `workspace/*.md` | Manual + git auto-commit | N/A (manual) | HEALTHY |
| `config.py` load | `lucyd.toml`, `providers.d/*.toml` | Manual (static) | N/A | HEALTHY |
| `skills.py` load | `workspace/skills/*.md` | Manual (static) | N/A | HEALTHY |
| `tools/status.py` cost | `cost.db` (costs) | `agentic.py:_record_cost()` | Yes | HEALTHY |
| `lucyd.py` PID | `lucyd.pid` | `lucyd.py` startup | Yes | HEALTHY |
| `lucyd.py` FIFO | `control.pipe` | `lucyd-send` / cron | Yes | HEALTHY |
| `lucyd.py` monitor | `monitor.json` | `lucyd.py:_process_message()` | Yes | HEALTHY |
| `memory.py` recall (facts) | `main.sqlite` (facts) | `consolidation.py` + `structured_memory.py` | Yes | HEALTHY |
| `memory.py` recall (episodes) | `main.sqlite` (episodes) | `consolidation.py` | Yes | HEALTHY |
| `memory.py` recall (commitments) | `main.sqlite` (commitments) | `consolidation.py` + `structured_memory.py` | Yes | HEALTHY |
| `memory.py` `resolve_entity()` | `main.sqlite` (entity_aliases) | `consolidation.py:extract_facts()` (auto) | Yes | HEALTHY |
| `consolidation.py` skip check | `main.sqlite` (consolidation_state) | `consolidation.py:update_consolidation_state()` | Yes | HEALTHY |
| `consolidation.py` hash check | `main.sqlite` (consolidation_file_hashes) | `consolidation.py:extract_from_file()` | Yes | HEALTHY |
| `synthesis.py` (session path) | recall_text from `inject_recall()` | `memory.inject_recall()` → `lucyd.py` | Yes | HEALTHY |
| `synthesis.py` (tool path) | recall_text from `inject_recall()` | `memory_tools.tool_memory_search` → `recall()` | Yes | HEALTHY |
| `memory_tools._synth_provider` | provider instance | `lucyd.py:825 set_synthesis_provider()` | Yes | HEALTHY |

## External Process Inventory

| Process | Type | Schedule | Exists? | Enabled? | Last Run | Status |
|---------|------|----------|---------|----------|----------|--------|
| `lucyd.service` | systemd | continuous | Yes | enabled | active (PID 584146) | HEALTHY |
| Workspace auto-commit | cron | :05 hourly | Yes | Yes | Hourly | HEALTHY |
| `lucyd-index` | cron | :10 hourly | Yes | Yes | 2026-02-24 | HEALTHY |
| `lucyd-consolidate` | cron | :15 hourly | Yes | Yes | 2026-02-24 00:39 | HEALTHY |
| `lucyd-consolidate --maintain` | cron | 04:05 daily | Yes | Yes | Scheduled | HEALTHY |
| Trash cleanup | cron | 03:05 daily | Yes | Yes | Daily | HEALTHY |
| DB integrity check | cron | 04:05 weekly (Sun) | Yes | Yes | Weekly | HEALTHY |
| Heartbeat | cron | — | Commented out | Disabled | — | NOTED (documented as disabled) |

### Cron Schedule Verification

```
:05 — workspace git auto-commit
:10 — lucyd-index (memory indexer)
:15 — lucyd-consolidate (structured extraction)
03:05 — trash cleanup (daily)
04:05 — consolidation maintenance (daily) + DB integrity check (weekly Sun)
```

Pipeline ordering correct: git commit → index → consolidate. 5-minute gaps prevent overlap. Maintenance jobs scheduled at low-activity hours.

## Freshness Checks

| Data Source | Threshold | Last Write | Fresh? |
|-------------|-----------|-----------|--------|
| Memory chunks | 48h | 2026-02-24 | YES |
| Structured facts | 2h | 2026-02-24 22:28 | YES |
| Consolidation state | Matches sessions | 2026-02-24 00:39 | YES |
| Episodes | 48h | 2026-02-24 | YES |
| Open commitments | Informational | 3 open (most recent: 2026-02-24) | OK |
| Cost DB | 24h | 2026-02-25 ~00:17 | YES |
| Session JSONL | Matches conversation | 2026-02-25 00:17 | YES |
| Daily memory logs | 72h | 2026-02-24 15:40 | YES |
| PID file | Current process | PID 584146 running | YES |
| Monitor JSON | 5 min (if active) | 2026-02-25 00:17 | YES |

All data sources within freshness thresholds.

## Round-Trip Test Coverage

| Pipeline | Round-Trip? | Test File | Key Test Function | Status |
|----------|------------|-----------|-------------------|--------|
| Memory: index → FTS search | PARTIAL | test_indexer.py | `test_fts_searchable_for_all_content` | FTS round-trip real; no `MemoryInterface.search()` end-to-end |
| Session: save → load | YES | test_session.py | `test_round_trip`, `test_add_user_message_persists` | PASS |
| Context: write → build | YES | test_context.py | `test_loads_stable_and_semi_stable`, `test_reload_picks_up_file_changes` | PASS |
| Cost: record → query | YES | test_cost.py, test_daemon_integration.py | `test_write_then_query`, `test_today_cost_from_db` | PASS |
| Structured: consolidate → recall (facts) | YES | test_consolidation.py | `test_facts_round_trip` | PASS |
| Structured: agent write → recall | YES | test_memory_tools_structured.py | `test_creates_new_fact`, `test_updates_changed_value` | PASS |
| Structured: episodes | YES | test_consolidation.py | `test_episodes_round_trip` | PASS |
| Structured: commitments | YES | test_consolidation.py, test_memory_tools_structured.py | `test_commitments_round_trip`, `test_changes_status` | PASS |
| Structured: aliases | YES | test_consolidation.py, test_structured_recall.py | `test_aliases_stored`, `test_alias_resolution_in_same_batch` | PASS |

## Findings

| # | Phase | Severity | Description | Status |
|---|-------|----------|-------------|--------|
| 1 | Round-trip | LOW | Memory pipeline: indexer→FTS round-trip exists, but no test exercises `MemoryInterface.search()` end-to-end (tool-level tests mock `_memory`) | Carried forward from Cycle 5 |
| 2 | Freshness | INFO | Stage 5 methodology SQL queries use stale column names (`valid` → `invalidated_at`, `session_file` → `session_id`, `title` → `summary`, `description` → `what`) | Recommend updating methodology templates |

## Comparison with Cycle 7

| Metric | Cycle 7 | Cycle 8 |
|--------|---------|---------|
| Pipelines mapped | 20 (incl. synthesis) | 20 |
| Dead pipelines | 0 | 0 |
| External processes | 8 | 8 |
| Round-trip tests | 8/9 | 8/9 (same gap) |
| All data fresh | Yes | Yes |

## Confidence

Overall confidence: 95%

- **Data flow mapping: HIGH (97%).** All pipelines traced producer-to-consumer. All producers exist and run.
- **External processes: HIGH (96%).** All processes verified. Heartbeat correctly documented as disabled.
- **Freshness: HIGH (95%).** All data sources within thresholds. Daemon active, cron running correctly.
- **Round-trip tests: MEDIUM (88%).** 8 of 9 pipelines have genuine round-trips. Memory pipeline gap is LOW — FTS layer tested, only `MemoryInterface.search()` aggregation untested end-to-end.
