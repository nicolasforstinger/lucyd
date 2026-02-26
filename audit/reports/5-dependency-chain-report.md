# Dependency Chain Audit Report

**Date:** 2026-02-26
**Audit Cycle:** 10
**EXIT STATUS:** PARTIAL

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-006 (dead data pipeline) | PASS | All 23 pipelines have active producers. No consumer without a writer. New pipeline: `evolution.py` reads daily logs, facts, episodes, IDENTITY.md → writes MEMORY.md/USER.md, updates `evolution_state`. |
| P-012 (auto-populated misclassified) | PASS | `entity_aliases` correctly classified as auto-populated by `consolidation.py:extract_facts()`. Anti-fragmentation directive present at line 148 ("use the shortest common name"). Alias insertion ordering (aliases BEFORE facts at lines 229/264+) preserved. `evolution_state` correctly classified as auto-populated by `evolution.py:update_evolution_state()`. |

## Data Flow Matrix

| Consumer | Data Source | Producer | Producer Runs? | Status |
|----------|-----------|----------|---------------|--------|
| `memory.py` search/recall | `main.sqlite` (chunks, chunks_fts) | `bin/lucyd-index` (cron :10) | Yes | DEGRADED (Finding #1) |
| `memory.py` embeddings | `main.sqlite` (embedding_cache) | `bin/lucyd-index` (cron :10) | Yes | DEGRADED (Finding #1) |
| `memory_schema.py` | `main.sqlite` (all 11 tables incl. FTS5) | `ensure_schema()` in daemon + indexer | Yes | HEALTHY |
| `session.py` load | `sessions/*.jsonl` + `.state.json` | `session.py` save (daemon) | Yes | HEALTHY |
| `session.py:build_session_info()` | `cost.db` + session state | `agentic.py:_record_cost()` + `session.py` | Yes | HEALTHY |
| `session.py:read_history_events()` | `sessions/*.jsonl` + `.archive/` | `session.py` save (daemon) | Yes | HEALTHY |
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
| `evolution.py` reads | daily logs, facts, episodes, commitments, IDENTITY.md, MEMORY.md, USER.md | Lucy (daily logs), `consolidation.py` (facts/episodes), manual (IDENTITY.md) | Yes | HEALTHY (new) |
| `evolution.py` writes | `evolution_state` table, MEMORY.md, USER.md | `evolution.py:evolve_file()` via `lucyd-consolidate --evolve` (cron 4:20) | Yes | NOT YET EXERCISED (Finding #2) |

## External Process Inventory

| Process | Type | Schedule | Exists? | Enabled? | Last Run | Status |
|---------|------|----------|---------|----------|----------|--------|
| `lucyd.service` | systemd | continuous | Yes | enabled | active (PID 129267) | HEALTHY |
| Workspace auto-commit | cron | :05 hourly | Yes | Yes | Hourly | HEALTHY |
| `lucyd-index` | cron | :10 hourly | Yes | Yes | 2026-02-26 21:10 | DEGRADED (embedding errors) |
| `lucyd-consolidate` | cron | :15 hourly | Yes | Yes | 2026-02-26 21:15 | HEALTHY |
| `lucyd-consolidate --maintain` | cron | 04:05 daily | Yes | Yes | Daily | HEALTHY |
| `lucyd-consolidate --evolve` | cron | 04:20 daily | Yes | Yes | Not yet (new) | PENDING (Finding #2) |
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
04:20 — lucyd-consolidate --evolve (daily, new)
```

Pipeline ordering correct: git commit → index → consolidate → maintain → evolve. 5-minute+ gaps prevent overlap.

## Freshness Checks

| Data Source | Threshold | Last Write | Fresh? | Notes |
|-------------|-----------|-----------|--------|-------|
| Memory chunks | 48h | 2026-02-24 16:10 (~53h) | STALE | Indexer runs but 2 files fail embedding (Finding #1). Changed files not re-embedded. |
| Structured facts | 2h from consolidation | 2026-02-25 21:15 | YES | Consolidation running at :15, 0 new facts (no new conversation content) |
| Consolidation state | Matches sessions | 2026-02-24 00:39 | YES | Last real conversation 2026-02-24. All sessions reset; expected. |
| File hashes | Matches workspace changes | 2026-02-25 21:15 | YES | MEMORY.md and memory/2026-02-25.md last processed |
| Episodes | 48h | 2026-02-24 | YES | Last episode from last real conversation |
| Open commitments | Informational | 2026-02-24 00:39 | OK | 3 open commitments, most recent from last conversation |
| Evolution state | N/A (new) | Never | N/A | Table exists, empty. Cron hasn't run yet. (Finding #2) |
| Cost DB | 24h | 2026-02-26 10:19 (~11h) | YES | |
| Session JSONL | Matches conversation | Sessions reset, sessions.json only | YES | All sessions reset earlier this session |
| Daily memory logs | 72h (conversational) | 2026-02-25 21:59 (~24h) | YES | |
| PID file | Current process | PID 129267 running | YES | |
| Monitor JSON | 5 min (if active) | 2026-02-26 10:19 (~11h) | OK | Daemon idle — no messages since reset. Expected. |
| Indexer lock file | Absent when idle | No lock file | YES | Clean |

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
| Structured: aliases | PARTIAL | test_consolidation.py | `test_alias_resolution_in_same_batch` | Alias extraction + lookup tested, no multi-session accumulation test |
| Session: build_session_info | YES | test_session.py | `TestBuildSessionInfo` (5 tests) | PASS |
| Session: read_history_events | YES | test_session.py | `TestReadHistoryEvents` (6 tests) | PASS |
| Evolution: gather → evolve → write | YES | test_evolution.py | `test_successful_evolution`, `test_processes_configured_files` | PASS (new) |

## Findings

| # | Phase | Severity | Description | Remediation |
|---|-------|----------|-------------|-------------|
| 1 | 3 (Freshness) | **MEDIUM** | **Embedding pipeline broken for changed files.** `indexer.py:293` uses `base_url: str = EMBEDDING_BASE_URL` as default parameter — Python evaluates default at function definition time, capturing the initial `""` before `configure()` sets the real URL. `bin/lucyd-index` calls `index_workspace()` without explicit `base_url`, so re-embedding always fails with `unknown url type: '/embeddings'`. Affects `MEMORY.md` (modified Feb 26, last embedded Feb 20) and `memory/2026-02-25.md` (never embedded). 48 cumulative failures in log. FTS5 indexing unaffected — keyword search works. Vector search degraded for changed files. | Fix: use `None` sentinel as default, resolve to module global at call time: `base_url = base_url if base_url is not None else EMBEDDING_BASE_URL`. Same fix needed for `model` parameter. Also affects `embed_batch()` at line 229 and `cache_embeddings()` at line 271. |
| 2 | 1 (Data Flow) | LOW | **Evolution pipeline not yet exercised.** `evolution_state` table is empty. Cron entry at 4:20 AM exists but hasn't run since being added today. Pipeline verified in tests (25 tests, 77% mutation kill rate). Will exercise on first 4:20 AM run. | No action needed — will self-resolve on first cron execution. |
| 3 | 4 (Round-trip) | LOW | Memory `MemoryInterface.search()` aggregation untested end-to-end. Indexer→FTS round-trip exists but full search interface not exercised. | Carried forward from cycle 5. |
| 4 | — | INFO | Stage 5 methodology SQL queries use stale column names (`session_file` → `session_id`, `title` → `summary`, `description` → `what`, `consolidated_at` → `last_consolidated_at`, `valid` → `invalidated_at IS NULL`). | Update methodology to match production schema. Carried from cycle 8. |
| 5 | — | INFO | CLAUDE.md says "10 tables" in memory schema section — now 11 with `evolution_state`. | Update CLAUDE.md table count. |

## Comparison with Cycle 9

| Metric | Cycle 9 | Cycle 10 | Change |
|--------|---------|----------|--------|
| Pipelines mapped | 22 | 24 | +2 (evolution read, evolution write) |
| Dead pipelines | 0 | 0 | Stable |
| External processes | 8 | 9 | +1 (`lucyd-consolidate --evolve`) |
| Round-trip tests | 11 (10 full + 1 partial) | 12 (10 full + 2 partial) | +1 (evolution) |
| Findings | 3 (2 LOW + INFO) | 5 (1 MEDIUM + 1 LOW + 1 LOW + 2 INFO) | +2, severity upgrade |
| All data fresh | Borderline | STALE (chunks 53h) | Embedding bug confirmed as root cause |

## Confidence

Overall confidence: 90%

- **Data flow mapping: HIGH (97%).** All 24 pipelines traced producer-to-consumer. All producers exist and run. New evolution pipeline correctly wired.
- **External processes: HIGH (96%).** All 9 processes verified. Evolution cron added, heartbeat documented disabled.
- **Freshness: LOW (75%).** Embedding pipeline broken for file re-indexing. Root cause identified (Python default parameter binding). FTS5-only search functional as designed fallback. Vector search degraded for 2 files.
- **Round-trip tests: MEDIUM (90%).** 10 of 12 pipelines have genuine round-trips. Memory search aggregation and alias accumulation partial.
