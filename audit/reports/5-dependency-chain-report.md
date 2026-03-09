# Dependency Chain Audit Report

**Date:** 2026-03-09
**Audit Cycle:** 17
**EXIT STATUS:** PASS

## Changes Since Cycle 16

1. **Passive telemetry buffer** (`_telemetry_buffer`) — in-memory dict, no new persistent store. Buffer keyed by notification `ref`, drained into next real message as `[telemetry: ...]`. No pipeline impact.
2. **Primary sender routing** (`primary_sender`) — redirects notification session keys. No new data store, just session key routing change.
3. **lucyd-send overhaul** — `--status`, `--log` flags added. Read-only consumers of existing stores (PID file, log file). No new producers.
4. **Test count:** 1725 (up from ~1684 in cycle 16). All passing.

## Pattern Checks

| Pattern | Result |
|---------|--------|
| P-006 (dead pipeline) | CLEAN — no new data stores. Telemetry buffer is in-memory only, no persistence gap. All 19 pipelines verified with active producers. |
| P-012 (auto-populated misclassified) | CLEAN — `entity_aliases` correctly identified as auto-populated by `consolidation.py:extract_facts()`. Anti-fragmentation directive present: "use the shortest common name as the canonical entity." Alias ordering invariant (aliases stored BEFORE facts) verified in `extract_facts()` at line 220–229. |
| P-014 (failure at dependency edges) | CLEAN — SQLite edges have `OperationalError` handling in `memory.py` and `indexer.py`. Consolidation uses atomic commit/rollback. Provider streaming has SDK bug workaround for misclassified status codes. |
| P-016 (resource lifecycle) | CLEAN — `finally` block in `run()` (line 1843) closes: sessions (`_save_state`), channel (`disconnect`), memory DB (`.close()`), PID file (remove), FIFO (unlink). All wrapped in `contextlib.suppress(Exception)`. |
| P-017 (state persistence ordering) | CLEAN — session state saved before cleanup in shutdown path. Consolidation commits atomically after all extractions, cost recording follows commit (non-critical). |
| P-026 (streaming error path) | CLEAN — Anthropic provider has explicit SDK bug workaround (line 223–252) for mid-stream SSE error misclassification. Overloaded (529) and API errors (500) re-raised with correct status codes. |
| P-027 (cost DB completeness) | CLEAN — all 6 `provider.complete()` call sites have corresponding `_record_cost()`: agentic loop (agentic.py:217), compaction (session.py:539), consolidation facts+episodes (consolidation.py:456,462), file extraction (consolidation.py:525), synthesis (lucyd.py:914). |

## Data Flow Matrix

| # | Consumer | Data Source | Producer | Producer Runs? | Status |
|---|----------|------------|----------|---------------|--------|
| 1 | `memory.py` search/recall | `memory/main.sqlite` (chunks, chunks_fts, embedding_cache) | `tools/indexer.py` via `bin/lucyd-index` cron (:10) | Yes | OK |
| 2 | `session.py` load | `sessions/*.YYYY-MM-DD.jsonl` | `session.py` save (daemon) | Yes | OK |
| 3 | `context.py` build | `workspace/*.md` | Lucy via tools / manual | N/A (agent-driven) | OK |
| 4 | `config.py` load | `lucyd.toml`, `providers.d/*.toml` | Manual / Claudio (static) | N/A (static) | OK |
| 5 | `skills.py` load | `workspace/skills/*.md` | Manual (static) | N/A (static) | OK |
| 6 | `tools/status.py` cost query | `cost.db` (costs table) | `agentic.py:_record_cost()` | Yes | OK |
| 7 | `lucyd.py` PID check | `~/.lucyd/lucyd.pid` | `lucyd.py` daemon startup | Yes | OK |
| 8 | `lucyd.py` FIFO reader | `~/.lucyd/control.pipe` | `bin/lucyd-send` and cron jobs | Yes | OK |
| 9 | Daily memory logs | `workspace/memory/YYYY-MM-DD.md` | Lucy via `write` tool (conversational) | N/A (non-deterministic) | OK |
| 10 | `lucyd.py` monitor reader | `~/.lucyd/monitor.json` | `lucyd.py:_write_monitor()` (daemon) | Yes | OK |
| 11 | `memory.py` `lookup_facts()` | `memory/main.sqlite` (facts) | `consolidation.py:extract_facts()` (cron :15) + `tools/structured_memory.py:handle_memory_write()` | Yes | OK |
| 12 | `memory.py` `search_episodes()` | `memory/main.sqlite` (episodes) | `consolidation.py:extract_episode()` (cron :15) | Yes | OK |
| 13 | `memory.py` `get_open_commitments()` | `memory/main.sqlite` (commitments) | `consolidation.py:extract_episode()` + `tools/structured_memory.py:handle_commitment_update()` | Yes | OK |
| 14 | `memory.py` `resolve_entity()` | `memory/main.sqlite` (entity_aliases) | `consolidation.py:extract_facts()` — auto-populated (P-012) | Yes | OK |
| 15 | `consolidation.py` skip check | `memory/main.sqlite` (consolidation_state) | `consolidation.py:update_consolidation_state()` | Yes | OK |
| 16 | `consolidation.py` hash check | `memory/main.sqlite` (consolidation_file_hashes) | `consolidation.py:extract_from_file()` | Yes | OK |
| 17 | `evolution.py` pre-check | `workspace/memory/YYYY-MM-DD.md` | Lucy via `write` tool (conversational) | N/A (non-deterministic) | OK |
| 18 | `evolution.py` state tracking | `memory/main.sqlite` (evolution_state) | Daemon agentic loop via evolution skill Step 8 (`exec` tool) | Yes | OK |
| 19 | Evolution output | `workspace/MEMORY.md`, `workspace/USER.md` | Daemon agentic loop (triggered by `lucyd-send --evolve` cron) | Yes | OK |

## External Process Inventory

| Process | Type | Schedule | Expected Output | Exists? | Enabled? | Last Run | Status |
|---------|------|----------|----------------|---------|----------|----------|--------|
| `lucyd.service` | systemd | continuous | daemon | Yes | Yes (enabled) | Running (PID 2350418, 28+ min) | ACTIVE |
| Workspace auto-commit | cron | `5 * * * *` | git commits | Yes | Yes | Active | ACTIVE |
| Memory indexer (`lucyd-index`) | cron | `10 * * * *` | `memory/main.sqlite` | Yes | Yes | 2026-03-09 21:10:01 | ACTIVE |
| Memory consolidation (`lucyd-consolidate`) | cron | `15 * * * *` | facts, episodes, commitments, aliases | Yes | Yes | 2026-03-09 21:15:59 | ACTIVE |
| Memory maintenance (`lucyd-consolidate --maintain`) | cron | `5 4 * * *` | dedup, decay, cleanup | Yes | Yes | Active | ACTIVE |
| Forced compact (`lucyd-send --compact`) | cron | `50 3 * * *` | diary + compaction | Yes | Yes | Active | ACTIVE |
| Memory evolution (`lucyd-send --evolve`) | cron | `20 4 * * *` | evolution_state + MEMORY.md/USER.md | Yes | Yes | Active | ACTIVE |
| Trash cleanup | cron | `5 3 * * 0` (weekly) | removes 30+ day trash | Yes | Yes | Active | ACTIVE |
| DB integrity check | cron | `5 4 * * 0` (weekly) | PRAGMA integrity_check | Yes | Yes | Active | ACTIVE |

**Cron pipeline ordering verified:**
`:05` git auto-commit -> `:10` lucyd-index -> `:15` lucyd-consolidate -> `3:50` forced compact -> `4:05` consolidate --maintain -> `4:20` lucyd-send --evolve

5-minute gaps maintained. No collisions. Note: CLAUDE.md documents the cron pipeline as `:05` -> `:10` -> `:15` -> `4:05` -> `4:20`, which is correct but omits the `3:50` forced compact entry. Documented separately in the compaction section.

## Freshness Checks

| Data Source | Threshold | Last Write | Fresh? |
|-------------|-----------|-----------|--------|
| Memory SQLite (chunks) | 48h | 2026-03-09 21:10 (epoch 1773087003942) | Yes |
| Cost SQLite (costs) | 24h | 2026-03-09 21:15:59 | Yes |
| Session JSONL | Matches conversation | 2026-03-09 21:15 | Yes |
| Daily memory logs | 72h | 2026-03-09 21:06 | Yes |
| PID file | Current process | PID 2350418 running | Yes |
| Monitor JSON | 5 min | 2026-03-09 21:15 | Yes |
| Structured facts | 2h | 2026-03-09 20:15:59 | Yes |
| Structured episodes | 48h | 2026-03-09 | Yes |
| Structured consolidation_state | Matches session activity | 2026-03-09 20:08:49 | Yes |
| Structured consolidation_file_hashes | Matches workspace changes | 2026-03-09 20:15:59 | Yes |
| Evolution state | Matches last evolution | 2026-03-09 16:37:19, logs through 2026-03-09 | Yes |
| Open commitments | Informational | 2026-03-09 12:32:30 | N/A (informational) |
| Indexer lock file | Should not exist when idle | Not present | OK |

## Round-Trip Test Coverage

| Pipeline | Test Exists? | Test File | Status |
|----------|-------------|-----------|--------|
| Memory: index -> FTS search | Yes | `test_indexer.py::TestIndexWorkspace::test_fts_searchable_for_all_content` | OK |
| Memory: cache write -> memory.py lookup | Yes | `test_indexer.py::TestCacheEmbeddings::test_cache_lookup_compatible_with_memory_py` | OK |
| Session: save -> load | Yes | `test_session.py::TestStateRoundTrip` (5 tests) | OK |
| Session: write JSONL -> rebuild | Yes | `test_session.py::test_rebuild_orders_chunks_correctly`, `test_rebuild_handles_compaction_event` | OK |
| Context: write file -> build includes it | Yes | `test_context.py::test_loads_stable_and_semi_stable`, `test_reload_picks_up_file_changes` | OK |
| Cost: record -> query | Yes | `test_cost.py::TestCostDBRoundTrip::test_write_then_query` | OK |
| Structured memory: write fact -> lookup | Yes | `test_structured_recall.py::TestLookupFacts` (populated_conn fixture writes, tests read via `lookup_facts()`) | OK |
| Structured memory: agent write -> read | Yes | `test_memory_tools_structured.py::TestMemoryWrite::test_creates_new_fact` (writes via `handle_memory_write()`, reads via SQL) | OK |
| Structured memory: episodes write -> search | Yes | `test_structured_recall.py::TestSearchEpisodes` (populated_conn writes, tests read via `search_episodes()`) | OK |
| Structured memory: commitments -> open query | Yes | `test_structured_recall.py::TestGetOpenCommitments` (populated_conn writes, tests read via `get_open_commitments()`) | OK |
| Structured memory: aliases -> resolve | Yes | `test_structured_recall.py::TestResolveEntity::test_alias_resolves`, `test_consolidation.py::test_resolve_entity_with_alias` | OK |
| Consolidation state: write -> skip check | Yes | `test_consolidation.py::TestConsolidationState` (3 tests) | OK |
| Evolution state: write -> read | Yes | `test_evolution.py` (uses `_insert_evolution_state` helper, reads via `get_evolution_state()`) | OK |

## Dependency Hygiene (Phase 4b)

### Installed But Never Imported
All production dependencies are used. Apparent mismatches resolved:
- `anthropic` -> `import anthropic` (providers/anthropic_compat.py, conditional import)
- `openai` -> `import openai` (providers/openai_compat.py, conditional import)
- `pillow` -> `from PIL import Image` (lucyd.py)
- `pypdf` -> `from pypdf import PdfReader` (lucyd.py)
- `httpx` -> `import httpx` (telegram.py, stt.py, lucyd.py, anthropic_compat.py)
- `aiohttp` -> HTTP API server (channels/http_api.py)
- `requests` -> transitive dep only (CacheControl, pip_audit). NOT in requirements.txt. Clean.

No orphaned direct dependencies found.

### HTTP Client Justification
- `httpx`: Sync HTTP for Telegram Bot API, STT, Anthropic SDK (their dependency)
- `aiohttp`: Async HTTP server for REST API channel
- `urllib.request`: Stdlib for embedding API calls, web fetch, TTS (no external dep needed)
- Each serves a distinct purpose. No duplication.

### Outdated Packages
| Package | Current | Latest | Type | Risk |
|---------|---------|--------|------|------|
| anthropic | 0.81.0 | 0.84.0 | minor | Low — SDK updates, review changelog |
| openai | 2.21.0 | 2.26.0 | minor | Low — SDK updates |
| pypdf | 6.7.5 | 6.8.0 | patch | None |
| ruff | 0.15.1 | 0.15.5 | patch | None (dev tool) |
| mutmut | 3.4.0 | 3.5.0 | minor | None (dev tool) |
| rich | 14.3.2 | 14.3.3 | patch | None (dev tool) |
| uc-micro-py | 1.0.3 | 2.0.0 | major | Low — transitive dep of linkify-it-py |

No security-critical updates. All outdated packages are minor/patch bumps or dev tools.

## Findings

No findings. All 19 pipelines have active producers, all external processes exist and run, all data sources within freshness thresholds, all round-trip tests exist.

## Confidence

| Phase | Confidence |
|-------|-----------|
| Phase 1 (Data Flow) | 97% — 19 pipelines mapped and verified. Evolution state producer is agent-driven (skill Step 8 exec), not framework code — inherently dependent on skill correctness. |
| Phase 2 (External Processes) | 98% — all 9 external processes verified via crontab, systemd, and log output. |
| Phase 3 (Freshness) | 98% — all stores within thresholds. Most recent writes within 30 minutes of audit. |
| Phase 4 (Round-Trip Tests) | 95% — all 13 critical pipelines have round-trip tests. Some use fixture-populated data rather than production function writes (e.g., structured recall tests use direct SQL INSERTs in fixtures rather than calling `extract_facts()`), which is acceptable for testing the read path but leaves a thin gap for write-path integration. |
| Phase 4b (Dependency Hygiene) | 97% — no orphaned packages, HTTP client usage justified, no security-critical updates. |
| **Overall** | **97%** — 19 pipelines healthy, all producers active, all data fresh, all round-trip tests present. No new data stores since cycle 16. |
