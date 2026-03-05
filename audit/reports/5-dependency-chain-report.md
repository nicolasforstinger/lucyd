# Dependency Chain Audit Report

**Date:** 2026-03-04
**Audit Cycle:** 15
**EXIT STATUS:** PASS

## Changes Since Cycle 14

1. **New cron job:** `lucyd-send --compact` at 3:50 AM (forced diary + compaction)
2. **New HTTP endpoint:** `POST /api/v1/compact` → routes through `_handle_compact()`
3. **New FIFO message type:** `"type": "compact"` → routes through message queue

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-006 (dead pipeline) | CLEAN | New compact pipeline has producer (cron :50) and consumer (daemon `_handle_compact`). All existing pipelines unchanged. |
| P-012 (auto-populated misclassified) | CLEAN | Alias ordering invariant still intact. |
| P-014 (failure at dependency edges) | CLEAN | `_handle_compact` has try/except with logging and HTTP 500 return. |
| P-016 (resource lifecycle) | CLEAN | No new resources requiring cleanup. |
| P-017 (state persistence ordering) | CLEAN | Compact routes through `_process_message` — same persistence path. |
| P-026 (streaming error path) | CLEAN | No provider changes. |
| P-027 (cost DB completeness) | CLEAN | Compact uses `_process_message` which records cost via agentic loop. |

## Data Flow Matrix

| Consumer | Data Source | Producer | Producer Runs? | Status |
|----------|-----------|----------|---------------|--------|
| `memory.py` search/recall | `memory/main.sqlite` (chunks) | `lucyd-index` cron :10 | Yes | HEALTHY |
| `memory.py` embedding_cache | `memory/main.sqlite` | `memory.py:_embed()` + `lucyd-index` | Yes | HEALTHY |
| `session.py` load | `sessions/*.jsonl` + `.state.json` | `session.py` save (daemon) | Yes | HEALTHY |
| `context.py` build | `workspace/*.md` | Lucy via tools / manual | N/A (conversational) | HEALTHY |
| `config.py` load | `lucyd.toml`, `providers.d/*.toml` | Manual (static) | N/A | HEALTHY |
| `skills.py` load | `workspace/skills/*.md` | Manual (static) | N/A | HEALTHY |
| `agentic.py` cost query | `cost.db` (costs table) | `agentic.py:_record_cost()` | Yes | HEALTHY |
| `lucyd.py` PID check | `lucyd.pid` | daemon startup | Yes | HEALTHY |
| `lucyd.py` FIFO reader | `control.pipe` | `lucyd-send` + cron | Yes | HEALTHY |
| `lucyd.py` monitor | `monitor.json` | `_process_message()` | Yes | HEALTHY |
| `memory.py` → `lookup_facts()` | `facts` table | `consolidation.py` cron :15 + tool | Yes | HEALTHY |
| `memory.py` → `search_episodes()` | `episodes` table | `consolidation.py` cron :15 | Yes | HEALTHY |
| `memory.py` → `get_open_commitments()` | `commitments` table | `consolidation.py` + tool | Yes | HEALTHY |
| `memory.py` → `resolve_entity()` | `entity_aliases` table | `consolidation.py:extract_facts()` | Yes | HEALTHY |
| `consolidation.py` skip check | `consolidation_state` table | `consolidation.py` | Yes | HEALTHY |
| `consolidation.py` hash check | `consolidation_file_hashes` table | `consolidation.py` | Yes | HEALTHY |
| `evolution.py` pre-check | daily logs | Lucy via `write` tool | N/A (conversational) | HEALTHY |
| `evolution.py` state | `evolution_state` table | `evolution.py` via daemon | Yes | HEALTHY |
| **NEW:** `lucyd.py` `_handle_compact()` | primary session | daemon message processing | Yes | HEALTHY |

## External Process Inventory

| Process | Type | Schedule | Exists? | Enabled? | Status |
|---------|------|----------|---------|----------|--------|
| `lucyd.service` | systemd | continuous | Yes | Yes | HEALTHY (PID 660248) |
| Workspace auto-commit | cron | `5 * * * *` | Yes | Yes | HEALTHY |
| Memory indexer | cron | `10 * * * *` | Yes | Yes | HEALTHY |
| Memory consolidation | cron | `15 * * * *` | Yes | Yes | HEALTHY |
| Memory maintenance | cron | `5 4 * * *` | Yes | Yes | HEALTHY |
| **NEW:** Forced compact | cron | `50 3 * * *` | Yes | Yes | HEALTHY (new) |
| Memory evolution | cron | `20 4 * * *` | Yes | Yes | HEALTHY |
| Trash cleanup | cron | `5 3 * * 0` | Yes | Yes | HEALTHY |
| DB integrity check | cron | `5 4 * * 0` | Yes | Yes | HEALTHY |

## Freshness Checks

| Data Source | Threshold | Last Write | Fresh? |
|-------------|-----------|-----------|--------|
| Memory SQLite (chunks) | 48h | 2026-03-04 | Yes |
| Cost SQLite (costs) | 24h | 2026-03-04 | Yes |
| Session JSONL | Matches conversation | 2026-03-04 22:44 | Yes |
| Daily memory logs | 72h | 2026-03-04 18:50 | Yes |
| PID file | Current process | PID 660248 running | Yes |
| Structured facts | 2h | 2026-03-04 21:43 | Yes |

## Dependency Hygiene

### Outdated Packages

| Package | Installed | Latest | Severity |
|---------|-----------|--------|----------|
| `certifi` | 2025.1.31 | 2026.2.25 | **Medium** (CA certs — reverted from 2026.2.25?) |
| Various others | — | — | Low (SDK patches, non-security) |

## Findings

| # | Phase | Severity | Description | Remediation |
|---|-------|----------|-------------|-------------|
| 1 | 4b | Medium | `certifi` at 2025.1.31, latest 2026.2.25 (CA cert bundle, >1 year old) | Update in Stage 8 |

## Confidence

Overall: 97% — 19 pipelines mapped (18 existing + 1 new compact), all producers active, all data fresh.
