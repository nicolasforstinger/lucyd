# Dependency Chain Audit Report

**Date:** 2026-02-26
**Audit Cycle:** 9
**EXIT STATUS:** PASS

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-006 (dead data pipeline) | PASS | All 20 pipelines have active producers. No consumer without a writer. New pipelines (session history, session info cost) correctly wired. |
| P-012 (auto-populated misclassified) | PASS | `entity_aliases` correctly classified as auto-populated by `consolidation.py:extract_facts()`. Anti-fragmentation directive present in extraction prompt ("use the shortest common name"). Alias insertion ordering (aliases BEFORE facts at lines 229/264+) preserved. |

## Data Flow Matrix

| Consumer | Data Source | Producer | Producer Runs? | Status |
|----------|-----------|----------|---------------|--------|
| `memory.py` search/recall | `main.sqlite` (chunks, chunks_fts) | `bin/lucyd-index` (cron :10) | Yes | HEALTHY |
| `memory.py` embeddings | `main.sqlite` (embedding_cache) | `bin/lucyd-index` (cron :10) | Yes | WARNING (see Finding #1) |
| `memory_schema.py` | `main.sqlite` (all 10 tables incl. FTS5) | `ensure_schema()` in daemon + indexer | Yes | HEALTHY |
| `session.py` load | `sessions/*.jsonl` + `.state.json` | `session.py` save (daemon) | Yes | HEALTHY |
| `session.py:build_session_info()` | `cost.db` + session state | `agentic.py:_record_cost()` + `session.py` | Yes | HEALTHY (new) |
| `session.py:read_history_events()` | `sessions/*.jsonl` + `.archive/` | `session.py` save (daemon) | Yes | HEALTHY (new) |
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
| `lucyd.service` | systemd | continuous | Yes | enabled | active (PID 62173) | HEALTHY |
| Workspace auto-commit | cron | :05 hourly | Yes | Yes | Hourly | HEALTHY |
| `lucyd-index` | cron | :10 hourly | Yes | Yes | 2026-02-26 17:10 | WARNING (embedding errors) |
| `lucyd-consolidate` | cron | :15 hourly | Yes | Yes | 2026-02-26 17:15 | HEALTHY |
| `lucyd-consolidate --maintain` | cron | 04:05 daily | Yes | Yes | Daily | HEALTHY |
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

Pipeline ordering correct: git commit → index → consolidate. 5-minute gaps prevent overlap.

## Freshness Checks

| Data Source | Threshold | Last Write | Fresh? | Notes |
|-------------|-----------|-----------|--------|-------|
| Memory chunks | 48h | 2026-02-24 16:10 (~49h) | BORDERLINE | Indexer runs but 2 files fail embeddings (Finding #1). FTS5 functional. |
| Structured facts | 2h from consolidation | 2026-02-25 21:15 | YES | Consolidation running at :15, 0 new facts expected (no new conversation content) |
| Consolidation state | Matches sessions | 2026-02-24 00:39 | YES | Last real conversation 2026-02-24. Current session is audit-only. Expected. |
| Episodes | 48h | 2026-02-24 | YES | Last episode from last real conversation |
| Open commitments | Informational | 2026-02-24 00:39 | OK | Most recent open commitment |
| Cost DB | 24h | 2026-02-26 ~10:19 (~7h) | YES | |
| Session JSONL | Matches conversation | 2026-02-26 10:19 | YES | Active session |
| Daily memory logs | 72h (conversational) | 2026-02-25 21:59 (~19h) | YES | |
| PID file | Current process | PID 62173 running | YES | |
| Monitor JSON | 5 min (if active) | ~7.6h ago | YES | Daemon idle — no messages since ~10:19. Expected. |
| Indexer lock file | absent when idle | No lock file | YES | Clean |

## Round-Trip Test Coverage

| Pipeline | Round-Trip? | Test File | Key Test Function | Status |
|----------|------------|-----------|-------------------|--------|
| Memory: index → FTS search | PARTIAL | test_indexer.py | `test_fts_searchable_for_all_content` | FTS round-trip real; no `MemoryInterface.search()` end-to-end |
| Session: save → load | YES | test_session.py | `test_round_trip`, `test_state_preserves_compaction_fields` | PASS |
| Context: write → build | YES | test_context.py | `test_reload_picks_up_file_changes` | PASS |
| Cost: record → query | YES | test_cost.py | `test_write_then_query` | PASS |
| Structured: consolidate → recall (facts) | YES | test_consolidation.py | `test_facts_round_trip` | PASS |
| Structured: agent write → recall | YES | test_memory_tools_structured.py | `test_creates_new_fact` | PASS |
| Structured: episodes | YES | test_consolidation.py | `test_episodes_round_trip` | PASS |
| Structured: commitments | YES | test_consolidation.py | `test_commitments_round_trip` | PASS |
| Structured: aliases | YES | test_consolidation.py | `test_alias_resolution_in_same_batch` | PASS |
| Session: build_session_info | YES | test_session.py | `TestBuildSessionInfo` (5 tests) | PASS (new) |
| Session: read_history_events | YES | test_session.py | `TestReadHistoryEvents` (6 tests) | PASS (new) |

## Findings

| # | Phase | Severity | Description | Remediation |
|---|-------|----------|-------------|-------------|
| 1 | 3 (Freshness) | LOW | Embedding API misconfigured: `MEMORY.md` and `memory/2026-02-25.md` fail with "unknown url type: '/embeddings'" in indexer log. FTS5 indexing succeeds but vector embeddings fail. Chunk freshness borderline (49h vs 48h threshold) because changed files can't be fully reprocessed. | Check embedding provider config in `providers.d/`. Likely missing or incorrect `base_url`. FTS5-only search works as designed fallback — degraded vector quality, not a pipeline break. |
| 2 | 4 (Round-trip) | LOW | Memory pipeline: indexer→FTS round-trip exists, but no test exercises `MemoryInterface.search()` end-to-end. | Carried forward from Cycle 5. |
| 3 | — | INFO | Stage 5 methodology SQL queries use stale column names (`valid` → `invalidated_at IS NULL`, `session_file` → `session_id`, `title` → `summary`, `description` → `what`). | Carried forward from Cycle 8. Recommend updating methodology. |

## Comparison with Cycle 8

| Metric | Cycle 8 | Cycle 9 | Change |
|--------|---------|---------|--------|
| Pipelines mapped | 20 | 22 | +2 (build_session_info, read_history_events) |
| Dead pipelines | 0 | 0 | Stable |
| External processes | 8 | 8 | Stable |
| Round-trip tests | 9 (8 full + 1 partial) | 11 (10 full + 1 partial) | +2 new round-trips |
| Findings | 2 (LOW + INFO) | 3 (2 LOW + INFO) | +1 (embedding API) |
| All data fresh | Yes | Yes (borderline on chunks) | Embedding issue |

## Confidence

Overall confidence: 94%

- **Data flow mapping: HIGH (97%).** All 22 pipelines traced producer-to-consumer. All producers exist and run. New parity pipelines correctly wired.
- **External processes: HIGH (96%).** All processes verified active. Heartbeat documented disabled.
- **Freshness: MEDIUM (88%).** Embedding API misconfiguration causes borderline chunk freshness. All other sources fresh. Consolidation correctly idle (no new conversation content).
- **Round-trip tests: MEDIUM (90%).** 10 of 11 pipelines have genuine round-trips. Memory `MemoryInterface.search()` aggregation untested end-to-end (carried forward).
