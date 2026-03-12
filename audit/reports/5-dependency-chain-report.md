# Dependency Chain Audit Report

**Date:** 2026-03-12
**Audit Cycle:** 18
**EXIT STATUS:** PASS

## External Process Inventory

| Process | Type | Schedule | Status |
|---------|------|----------|--------|
| `lucyd.service` | systemd | continuous | Active (pid 3019670) |
| Workspace auto-commit | cron | `:05` | Running |
| Memory indexer (`lucyd-index`) | cron | `:10` | Running |
| Memory consolidation (`lucyd-consolidate`) | cron | `:15` | Running |
| Memory maintenance (`--maintain`) | cron | `4:05` | Running |
| Forced compact (`lucyd-send --compact`) | cron | `3:50` | Running |
| Memory evolution (`lucyd-send --evolve`) | cron | `4:20` | Running |
| Trash cleanup | cron | `3:05 weekly` | Running |
| DB integrity check | cron | `4:05 weekly` | Running |

## Freshness Checks

| Data Source | Threshold | Last Write | Fresh? |
|-------------|-----------|-----------|--------|
| Session JSONL | 24h | 2026-03-12 03:52 | Yes |
| Memory chunks | 48h | 2026-03-12 (~04:18 epoch) | Yes |
| Cost DB | 24h | 2026-03-12 (~04:18 epoch) | Yes |
| Facts table | 2h | 2026-03-12 04:18 | Yes |
| Consolidation state | Session activity | 2026-03-12 03:29 | Yes |
| Episodes | 48h | 2026-03-12 | Yes |
| Daily memory logs | 72h | 2026-03-12 03:50 | Yes |
| Monitor JSON | 5 min | 2026-03-12 04:29 | Yes |
| PID file | Current | Active, matches process | Yes |

## Round-Trip Test Coverage

| Pipeline | Test File(s) | Status |
|----------|-------------|--------|
| Memory: index → search | test_indexer.py, test_zero_kill_modules.py | Covered |
| Session: save → load | test_session.py | Covered |
| Cost: record → query | test_cost.py | Covered |
| Context: write → build | test_context.py | Covered |
| Structured: consolidate → recall | test_consolidation.py, test_structured_recall.py | Covered |
| Structured: agent write → recall | test_memory_tools_structured.py | Covered |
| Structured: episodes | test_consolidation.py, test_structured_recall.py | Covered |
| Structured: commitments | test_structured_recall.py | Covered |
| Structured: aliases | test_consolidation.py | Covered |

## Pattern Checks

| Pattern | Result |
|---------|--------|
| P-006 dead data pipeline | All consumers have active producers. No orphaned readers. |
| P-012 auto-populated misclassified | `entity_aliases` correctly identified as auto-populated by `consolidation.py`. |
| P-014 failure behavior at edges | All external edges have defined failure behavior (retry, fallback, or graceful error). |
| P-016 resource lifecycle | All `self.*` resources closed in finally/disconnect paths. |
| P-017 state persistence ordering | `_save_state()` at critical junctures. Correct order verified. |
| P-027 cost.db completeness | 5/5 `provider.complete()` sites have matching `_record_cost()` calls. |

## Known Gaps

None.

## Confidence

96% — all pipelines active, all data fresh, all round-trips tested. Cron pipeline verified against live system.
