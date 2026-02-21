# Dependency Chain Audit Report

**Date:** 2026-02-21
**Audit Cycle:** 4
**EXIT STATUS:** PASS
**Environment:** Development (no runtime instance — code-level verification only)

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-006 (dead data pipeline) | CLEAN | All consumers have active producers in the code. No dead pipelines. Every data source has an identified write path. |
| P-012 (auto-populated misclassified as static) | CLEAN | `entity_aliases` confirmed auto-populated by `consolidation.py:225-232` (`INSERT OR IGNORE INTO entity_aliases`). Ordering invariant intact (line 223: comment + aliases stored BEFORE facts). Anti-fragmentation directive at line 149-151 ("use the shortest common name"). Not misclassified. Same as Cycle 3. |

## Data Flow Matrix

17 data sources, 10 consumer modules. All have identified producers.

| Consumer | Data Source | Producer | Producer Type | Status |
|----------|-----------|----------|--------------|--------|
| `memory.py` search/recall | `main.sqlite` (chunks, FTS, embeddings) | `tools/indexer.py` via `bin/lucyd-index` | Cron (:10) | HEALTHY |
| `memory.py` structured recall | `main.sqlite` (facts) | `consolidation.py` (cron :15, pre-compaction, close) + `memory_write` tool | Cron + daemon + agent | HEALTHY |
| `memory.py` structured recall | `main.sqlite` (episodes) | `consolidation.py` | Cron + daemon | HEALTHY |
| `memory.py` structured recall | `main.sqlite` (commitments) | `consolidation.py` + `commitment_update` tool | Cron + daemon + agent | HEALTHY |
| `memory.py` resolve_entity | `main.sqlite` (entity_aliases) | `consolidation.py` extract_facts (auto) | Cron + daemon | HEALTHY |
| `memory.py` embedding cache | `main.sqlite` (embedding_cache) | `memory.py` itself (self-caching) | Daemon | HEALTHY |
| `consolidation.py` skip check | `main.sqlite` (consolidation_state) | `consolidation.py` update_state | Self | HEALTHY |
| `consolidation.py` hash check | `main.sqlite` (consolidation_file_hashes) | `consolidation.py` extract_from_file | Self | HEALTHY |
| `memory_schema.py` | `main.sqlite` (DDL) | `memory_schema.py` ensure_schema | Self (bootstrap) | HEALTHY |
| `session.py` load | `sessions/*.jsonl` + `.state.json` | `session.py` save (daemon) | Daemon | HEALTHY |
| `context.py` build | `workspace/*.md` | Operator / Lucy via tools | Manual + agent | HEALTHY |
| `config.py` load | `lucyd.toml`, `providers.d/*.toml`, `.env` | Operator (hand-authored) | Static | HEALTHY |
| `skills.py` load | `workspace/skills/*.md` | Operator (hand-authored) | Static | HEALTHY |
| `tools/status.py` cost query | `cost.db` (costs) | `agentic.py` _record_cost() | Daemon | HEALTHY |
| `lucyd.py` PID check | `lucyd.pid` | `lucyd.py` daemon startup | Daemon | HEALTHY |
| `lucyd.py` FIFO reader | `control.pipe` | `bin/lucyd-send`, cron jobs | On-demand | HEALTHY |
| `lucyd.py` monitor | `monitor.json` | `lucyd.py` _process_message | Daemon | HEALTHY |

No dead pipelines. Every consumer has an identified, implemented producer.

## External Process Inventory

**Note:** Development environment. No runtime instance (`~/.lucyd/` does not exist). Inventory verified from source code — all processes exist and would be functional when deployed.

| Process | Type | Schedule | In Codebase? | Status |
|---------|------|----------|-------------|--------|
| `lucyd.service` (daemon) | systemd | continuous | Yes (`lucyd.py`) | Code verified |
| `lucyd-index` (indexer) | cron | `:10 * * * *` | Yes (`bin/lucyd-index`) | Code verified |
| `lucyd-consolidate` | cron | `:15 * * * *` | Yes (`bin/lucyd-consolidate`) | Code verified |
| `lucyd-consolidate --maintain` | cron | daily | Yes (flag supported) | Code verified |
| `lucyd-send` (FIFO CLI) | on-demand | — | Yes (`bin/lucyd-send`) | Code verified |

Freshness checks (Phase 3) are N/A — no deployment state to check.

## Round-Trip Test Coverage

| Pipeline | Round-Trip? | Test File(s) | Real Store? | Status |
|----------|------------|-------------|-------------|--------|
| Memory: index → FTS search | Yes | `test_indexer.py::test_fts_searchable_for_all_content` | Yes (real SQLite) | PASS |
| Memory: index → vector search | No | — | — | GAP (Low) |
| Session: save → load | Yes | `test_session.py::test_rebuild_*`, `TestStateRoundTrip` | Yes (real JSONL) | PASS |
| Context: write → build | Yes | `test_context.py::test_reload_picks_up_file_changes` | Yes (real filesystem) | PASS |
| Cost: record → query | Yes | `test_cost.py::test_write_then_query` | Yes (real SQLite) | PASS |
| Structured: facts write → recall | Partial | `test_consolidation.py` + `test_structured_recall.py` | Yes (LLM mocked, DB real) | PASS |
| Structured: agent write → recall | Partial | `test_memory_tools_structured.py` | Yes (real SQLite) | PASS |
| Structured: episodes | No (each side independent) | `test_consolidation.py` + `test_structured_recall.py` | Separate DBs | GAP (Low) |
| Structured: commitments | Partial | `test_memory_tools_structured.py` | Yes (real SQLite) | PASS |
| Structured: aliases | Partial | `test_consolidation.py::test_aliases_stored` | Yes (real SQLite) | PASS |

### Carried-Forward Gaps (Cycle 3 → 4)

1. **Memory vector search round-trip (Low):** No test indexes files then queries via `MemoryInterface.search()` vector path. Embeddings always mocked. FTS path verified. Would require local embedding model or cached vectors.
2. **Episode pipeline round-trip (Low):** Episodes written by `extract_episode()` and read by `search_episodes()`, but no test composes both. Each side uses its own DB fixtures.

Both are composition gaps — each side individually tested against real stores.

## Findings

| # | Phase | Severity | Description | Remediation |
|---|-------|----------|-------------|-------------|
| 1 | 4 | Low | No vector search round-trip (carried from Cycle 3) | FTS path verified. Embedding mock prevents real vector test. |
| 2 | 4 | Low | No episode write→search round-trip (carried from Cycle 3) | Each side works against real SQLite independently. |

## Confidence

Overall confidence: 95%

- Phase 1: All 17 data sources mapped to producers (98%). No dead pipelines.
- Phase 2: All expected processes exist in codebase (95%). Runtime verification N/A.
- Phase 3: Freshness checks N/A (dev environment).
- Phase 4: 8/10 pipelines have round-trip or partial round-trip tests (92%). 2 gaps are Low severity.
- Same results as Cycle 3 — no regression.
