# Dependency Chain Audit Report

**Date:** 2026-02-28
**Audit Cycle:** 11
**EXIT STATUS:** PASS

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-006 (dead data pipeline) | PASS | All 25 pipelines have active producers. No consumer without a writer. New pipeline: Telegram quote extraction → LLM text injection (in-flight transform, no new data store). Auto-close system sessions flows through existing `close_session()` → consolidation → archive pipeline. |
| P-012 (auto-populated misclassified) | PASS | `entity_aliases` correctly classified as auto-populated by `consolidation.py:extract_facts()`. Anti-fragmentation directive present at line 148 ("use the shortest common name"). Alias insertion ordering (aliases BEFORE facts at lines 220/230) preserved. `evolution_state` correctly classified as auto-populated by daemon agentic loop via `lucyd-evolve` trigger. |
| P-014 (failure behavior at boundaries) | FINDING | Auto-close at `lucyd.py:1117` calls `close_session()` without try/except. If `f.rename()` in archival (session.py:350) fails due to filesystem error, exception propagates and crashes the daemon on an otherwise-successful system message. All other `close_session()` call sites share this pattern (lines 1280, 1284, 1299), but those are user-initiated resets — auto-close fires automatically on every system message. See Finding #1. |
| P-016 (resource lifecycle / shutdown) | PASS | All resources accounted for. `_memory_conn` created at lucyd.py:596, closed in finally block at 1725-1727. Channel `disconnect()` called at 1721. PID file removed at 1730. FIFO cleaned at 1733. `httpx.AsyncClient` instances at lines 1170/1209/1262 use `async with` context managers. Telegram `_client` closed via `disconnect()` → `aclose()`. |
| P-017 (state persistence ordering) | PASS | Main path: `_save_state()` at line 1006 before delivery/webhook/consolidation/compaction. Compaction: `_save_state()` at line 506 before `append_event()` at 507. Auto-close at 1117 fires after `_save_state()` at 1006 — crash during close leaves resumable state. |
| P-025 (default parameter binding) | PASS (Resolved) | All three functions in `indexer.py` (`embed_batch`, `cache_embeddings`, `index_workspace`) now use `None` sentinel pattern. Grep for `def \w+\(.*:\s*\w+\s*=\s*[A-Z_]{2,}` across all `.py` files returns zero matches. Embedding pipeline fully functional — all 136 chunks have embeddings, 0 empty. |
| P-026 (streaming error path) | PASS | Hotfix in `anthropic_compat.py:223-252` compensates for SDK mid-stream SSE error misclassification. `status_code < 429` guard inspects body for `overloaded_error`/`api_error`, re-raises with synthesized response carrying correct status code. 6 tests in `TestAnthropicMidstreamSSEReRaise`. Canary test `test_sdk_bug_still_exists` monitors SDK fix. OpenAI provider uses `client.chat.completions.create()` (non-streaming) — not affected. |

## Data Flow Matrix

| Consumer | Data Source | Producer | Producer Runs? | Status |
|----------|-----------|----------|---------------|--------|
| `memory.py` search/recall | `main.sqlite` (chunks, chunks_fts) | `bin/lucyd-index` (cron :10) | Yes | HEALTHY |
| `memory.py` embeddings | `main.sqlite` (embedding_cache) | `bin/lucyd-index` (cron :10) | Yes | HEALTHY |
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
| `memory_tools._synth_provider` | provider instance | `lucyd.py` `set_synthesis_provider()` | Yes | HEALTHY |
| `evolution.py` reads | daily logs, facts, episodes, commitments, IDENTITY.md, MEMORY.md, USER.md | Lucy (daily logs), `consolidation.py` (facts/episodes), manual (IDENTITY.md) | Yes | HEALTHY |
| `evolution.py` writes | `evolution_state` table, MEMORY.md, USER.md | Daemon agentic loop via `lucyd-evolve` (cron 4:20) | Yes | HEALTHY (exercised) |
| Telegram quote injection | `reply_to_message` in Telegram update | `telegram.py:_parse_message()` → `InboundMessage.quote` | Yes | HEALTHY (new) |

**New pipeline (Cycle 11):** Telegram quote extraction. Data flow: Telegram `reply_to_message` → `_parse_message()` extracts quote text (with Telegram quote selection preference, media fallbacks) → stored in `InboundMessage.quote` → injected at `lucyd.py:1513-1515` as `[replying to: ...]` prefix → flows into session as part of user message text. No new data store — in-flight text transformation. Truncated at 200 chars.

**Resolved from Cycle 10:** Evolution pipeline now fully exercised. `evolution_state` table contains two entries (MEMORY.md and USER.md, both evolved 2026-02-27 12:14:23). Triggered via HTTP API (not cron — cron has skipped since logs_through matches latest log). Embedding pipeline now fully healthy — P-025 fix resolved the default parameter binding bug. All 136 chunks have embeddings, 0 empty.

## External Process Inventory

| Process | Type | Schedule | Exists? | Enabled? | Last Run | Status |
|---------|------|----------|---------|----------|----------|--------|
| `lucyd.service` | systemd | continuous | Yes | enabled | active (PID 229387, since Feb 27 23:11) | HEALTHY |
| Workspace auto-commit | cron | :05 hourly | Yes | Yes | Hourly | HEALTHY |
| `lucyd-index` | cron | :10 hourly | Yes | Yes | 2026-02-28 17:10 | HEALTHY |
| `lucyd-consolidate` | cron | :15 hourly | Yes | Yes | 2026-02-28 17:15 | HEALTHY |
| `lucyd-consolidate --maintain` | cron | 04:05 daily | Yes | Yes | Daily | HEALTHY |
| `lucyd-evolve` | cron | 04:20 daily | Yes | Yes | 2026-02-28 04:20 (skipped — no new logs) | HEALTHY |
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
04:20 — lucyd-evolve (self-driven evolution, daily)
```

Pipeline ordering correct: git commit → index → consolidate → maintain → evolve. 5-minute+ gaps prevent overlap.

**Note:** Cycle 10 report listed the evolution cron as `lucyd-consolidate --evolve`. The actual implementation uses the separate `lucyd-evolve` script (`bin/lucyd-evolve`). The cron entry correctly invokes `lucyd-evolve`, not `lucyd-consolidate --evolve`.

## Freshness Checks

| Data Source | Threshold | Last Write | Fresh? | Notes |
|-------------|-----------|-----------|--------|-------|
| Memory chunks | 48h | 2026-02-28 01:10 (~16h) | YES | All 136 chunks have embeddings. P-025 fix resolved embedding pipeline. Indexer skipping (all 25 files unchanged). |
| Structured facts | 2h from consolidation | 2026-02-28 00:15 | YES | Consolidation running hourly at :15. 0 new facts (no new file changes). |
| Consolidation state | Matches sessions | 2026-02-27 16:09 | YES | Latest session (Feb 27) processed. Active session (Feb 28) in progress, not yet consolidated. |
| File hashes | Matches workspace changes | 2026-02-28 00:15 | YES | `memory/2026-02-27.md` last processed. |
| Episodes | 48h | Latest from 2026-02-27 | YES | Evolution episode from Feb 27 evolution cycle. |
| Open commitments | Informational | Various | OK | 5 open commitments. Most from recent conversations. |
| Evolution state | Matches daily logs | 2026-02-27 12:14 | YES | Both MEMORY.md and USER.md evolved through 2026-02-27. Cron skips correctly when no new logs. |
| Cost DB | 24h | 2026-02-28 15:30 (~2h) | YES | Active conversation today. |
| Session JSONL | Matches conversation | 2026-02-28 15:30 | YES | Active session for Nicolas (Feb 28). |
| Daily memory logs | 72h (conversational) | 2026-02-27 00:34 (~17h) | YES | Feb 27 log written during late-night session. |
| PID file | Current process | PID 229387 running | YES | Matches systemd Main PID. |
| Monitor JSON | 5 min (if active) | 2026-02-28 15:30 (~2h) | OK | Daemon idle after last message. Expected. |
| Indexer lock file | Absent when idle | No lock file | YES | Clean. |

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
| Evolution: state → check → trigger | YES | test_evolution.py | 7 tests (state read/write, log pre-check, ref file) | PASS |
| Quote: Telegram → InboundMessage → LLM text | YES | test_telegram_channel.py + test_daemon_integration.py | `test_reply_to_text_message_extracts_quote`, `test_quote_injected_into_text`, `test_long_quote_truncated` (6 tests total) | PASS (new) |
| Auto-close: system → close_session | YES | test_orchestrator.py | `TestAutoCloseSystemSessions` (5 tests) | PASS (new) |

## Findings

| # | Phase | Severity | Description | Remediation |
|---|-------|----------|-------------|-------------|
| 1 | Pattern (P-014) | LOW | **Auto-close `close_session()` at lucyd.py:1117 is unguarded.** If `session.py:350 f.rename()` fails during archival (disk full, permission error), the exception propagates through `_process_message()` and crashes the daemon. The message was already processed successfully — only the cleanup failed. Pre-existing pattern: `_reset_session()` at lines 1280/1284/1299 has the same exposure. However, auto-close fires automatically on every system message, increasing the likelihood of hitting a transient failure compared to user-initiated resets. | Wrap auto-close in try/except with log warning. Consider adding try/except to `_reset_session()` as well, though that's a lower priority (user-initiated). |
| 2 | 4 (Round-trip) | LOW | Memory `MemoryInterface.search()` aggregation untested end-to-end. Indexer→FTS round-trip exists but full search interface (FTS + vector fallback + merge + dedup) not exercised as one pipeline. | Carried forward from cycle 5. Individual components tested; aggregation logic is straightforward. |
| 3 | — | INFO | Stage 5 methodology SQL queries use stale column names (`session_file` → `session_id`, `title` → `summary`, `description` → `what`, `consolidated_at` → `last_consolidated_at`, `valid` → `invalidated_at IS NULL`, `created_at` in chunks → `updated_at`, `created_at` in episodes → column doesn't exist, use `rowid`). | Update methodology to match production schema. Carried from cycle 8. |
| 4 | — | INFO | Cycle 10 report listed evolution cron as `lucyd-consolidate --evolve`. Actual implementation uses separate `bin/lucyd-evolve` script. | Corrected in this report. |

## Resolved from Cycle 10

| Finding | Resolution |
|---------|-----------|
| **#1 (MEDIUM): Embedding pipeline broken** | P-025 fix applied. All three functions (`embed_batch`, `cache_embeddings`, `index_workspace`) now use `None` sentinel. All 136 chunks have embeddings. No more embedding failures in log. |
| **#2 (LOW): Evolution pipeline not yet exercised** | Evolution ran successfully 2026-02-27 12:14:23 via HTTP API. Both MEMORY.md and USER.md evolved. `evolution_state` table populated. Cron (`lucyd-evolve`) runs daily at 04:20, correctly skips when no new logs. |
| **#5 (INFO): CLAUDE.md table count** | CLAUDE.md updated to "11 tables" with evolution_state included. |

## Comparison with Cycle 10

| Metric | Cycle 10 | Cycle 11 | Change |
|--------|----------|----------|--------|
| Pipelines mapped | 24 | 25 | +1 (quote injection) |
| Dead pipelines | 0 | 0 | Stable |
| External processes | 9 | 9 | Stable (lucyd-evolve confirmed as separate script) |
| Round-trip tests | 12 (10 full + 2 partial) | 14 (12 full + 2 partial) | +2 (quote injection, auto-close) |
| Findings | 5 (1 MEDIUM + 1 LOW + 1 LOW + 2 INFO) | 4 (1 LOW + 1 LOW + 2 INFO) | -1, severity downgrade |
| All data fresh | STALE (embedding bug) | ALL FRESH | Embedding pipeline fully recovered |
| Test count | ~1472 | 1489 | +17 |

## Confidence

Overall confidence: 95%

- **Data flow mapping: HIGH (98%).** All 25 pipelines traced producer-to-consumer. All producers exist and run. New quote injection pipeline is a text transform, not a persistent store. Auto-close routes through existing `close_session()` pipeline.
- **External processes: HIGH (97%).** All 9 processes verified. Daemon active (PID 229387, 18h uptime). All cron jobs running on schedule. Evolution cron correctly skipping (no new logs since last evolution).
- **Freshness: HIGH (96%).** All data sources within thresholds. Embedding pipeline fully recovered (0 empty chunks). Consolidation, indexing, and evolution all running correctly. Active conversation today (cost DB and sessions fresh).
- **Round-trip tests: HIGH (93%).** 12 of 14 pipelines have genuine round-trips. Memory search aggregation still partial (carried since cycle 5). Alias accumulation partial. Two new pipelines (quote injection, auto-close) have full round-trip coverage.
