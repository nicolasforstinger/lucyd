# Dependency Chain Audit Report

**Date:** 2026-02-24
**Audit Cycle:** 7
**EXIT STATUS:** PASS

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-006 (dead pipeline) | CLEAN | All consumers have active producers. New synthesis pipeline (`synthesis.py`) has two active producers: `lucyd.py` (session-start path, line 778-796) and `tools/memory_tools.py` (tool path, line 56-63). Both reach `synthesize_recall()` which calls `provider.complete()`. |
| P-012 (misclassified static) | CLEAN | `entity_aliases` correctly auto-populated by `consolidation.py:extract_facts()` (line 225-232). Ordering invariant preserved (aliases stored BEFORE facts, line 223, with explicit comment). Anti-fragmentation directive present in `FACT_EXTRACTION_PROMPT` (line 149: "use the shortest common name as the canonical entity"). |
| P-014 (failure behavior) | PASS | All `provider.complete()` calls wrapped in try/except: `consolidation.py` (lines 208-212, 336-340), `lucyd.py` recall (753-769), synthesis (779-796), pre-compaction (1010-1031), on-close (1045-1065), `synthesis.py` (113-143), `tools/memory_tools.py` synthesis (58-63). New synthesis paths both log-and-continue — `lucyd.py` falls back to raw recall, `memory_tools.py` falls back to raw recall. |
| P-016 (shutdown path) | PASS | `_memory_conn` closed in `run()` `finally` (line 1564-1568). Telegram httpx `disconnect()` called in `finally` (1558-1562). `cost.db` uses per-call open/close with `finally` — no persistent connection. No new persistent resources from synthesis (provider reused, not newly created). |
| P-017 (persist order) | PASS | Compaction state in `session.py` persists immediately after mutation. Warning consumption in `lucyd.py` persisted via `_save_state()` at line 717 before agentic loop. Synthesis operates on `recall_text` (a local variable), not persisted state — no crash-safety concern. |

## New Pipeline: Memory Synthesis

### Data Flow: Session-Start Path (lucyd.py)

```
config.recall_synthesis_style  (TOML → config.py:337-339)
  → lucyd.py:778 guard (skip if "structured")
  → synthesis.synthesize_recall(recall_text, style, provider)
  → provider.complete() via PROMPTS[style] template
  → recall_text replaced with SynthesisResult.text
  → synth_result.usage → agentic._record_cost() (lines 787-794)
  → recall_text → context_builder.build(extra_dynamic=recall_text)
```

**Provider source:** Same `provider` resolved at line 637 via `self.config.route_model(source)` — the routed model for the current message. No model mismatch.

**Footer preservation:** `synthesis.py:127-129` scans `recall_text` for lines starting with `[Memory loaded:` or `[Dropped` and appends them to synthesized output. Producer: `memory.inject_recall()` (line 567). Consumer: `synthesis.synthesize_recall()` (line 127). Verified by `test_synthesis.py::TestFooterPreservation` (3 tests).

### Data Flow: Tool Path (memory_tools.py)

```
lucyd.py:823-825 sets _synth_provider per-message
  → from tools.memory_tools import set_synthesis_provider
  → set_synthesis_provider(provider)  (same routed provider)
  → memory_tools._synth_provider module global

Agent calls memory_search tool:
  → tool_memory_search(query)
  → recall() + inject_recall() → raw result
  → check _config.recall_synthesis_style != "structured"
  → check _synth_provider is not None
  → synthesis.synthesize_recall(result, style, _synth_provider)
  → result replaced with synth_result.text
```

**Guard conditions:** Three guards prevent synthesis when not configured:
1. `lucyd.py:823` — only calls `set_synthesis_provider()` if style != "structured"
2. `memory_tools.py:57` — checks style != "structured"
3. `memory_tools.py:57` — checks `_synth_provider is not None`

**Provider parity:** Both paths use the same `provider` variable from `self.providers.get(model_name)` at `lucyd.py:637`. The tool path receives it via `set_synthesis_provider(provider)` at line 825, called in the same `_process_message()` scope.

### Config Property Chain

```
lucyd.toml: [memory.recall.personality] synthesis_style = "narrative"
  → config.py:337-339: recall_synthesis_style property
  → _deep_get(self._data, "memory", "recall", "personality", "synthesis_style", default="structured")
  → default "structured" = no LLM call, zero cost, raw passthrough
```

### PROMPTS Registry Validation

| VALID_STYLES | PROMPTS keys | Match? |
|--------------|-------------|--------|
| structured | (not in PROMPTS — passthrough) | Correct — no prompt needed |
| narrative | PROMPTS["narrative"] | Present, contains `{recall_text}` placeholder |
| factual | PROMPTS["factual"] | Present, contains `{recall_text}` placeholder |

`VALID_STYLES - set(PROMPTS.keys()) = {"structured"}` — exactly the passthrough style. Verified by `test_synthesis.py::TestPromptRegistry` (5 tests).

## Data Flow Matrix

| Consumer | Data Source | Producer | Producer Runs? | Status |
|----------|-----------|----------|---------------|--------|
| memory.py search | main.sqlite (chunks, chunks_fts) | tools/indexer.py via cron :10 | Yes | HEALTHY |
| memory.py embeddings | main.sqlite (embedding_cache) | tools/indexer.py via cron :10 | Yes | HEALTHY |
| memory_schema.py | main.sqlite (all 10 tables) | ensure_schema() in daemon + indexer | Yes | HEALTHY |
| session.py load | sessions/*.jsonl | session.py save (daemon) | Yes | HEALTHY |
| context.py build | workspace/*.md | Lucy + tools (non-deterministic) | N/A | HEALTHY |
| config.py load | lucyd.toml, providers.d/*.toml | Manual (static) | N/A | HEALTHY |
| skills.py load | workspace/skills/*.md | Manual (static) | N/A | HEALTHY |
| tools/status.py cost | cost.db (costs) | agentic.py _record_cost() | Yes | HEALTHY |
| lucyd.py PID | lucyd.pid | lucyd.py startup | Yes | HEALTHY |
| lucyd.py FIFO | control.pipe | lucyd-send / cron | Yes | HEALTHY |
| lucyd.py monitor | monitor.json | lucyd.py _process_message | Yes | HEALTHY |
| memory.py recall (facts) | main.sqlite (facts) | consolidation.py + structured_memory.py | Yes | HEALTHY |
| memory.py recall (episodes) | main.sqlite (episodes) | consolidation.py | Yes | HEALTHY |
| memory.py recall (commitments) | main.sqlite (commitments) | consolidation.py + structured_memory.py | Yes | HEALTHY |
| memory.py resolve_entity | main.sqlite (entity_aliases) | consolidation.py extract_facts() (auto) | Yes | HEALTHY |
| consolidation.py skip | main.sqlite (consolidation_state) | consolidation.py | Yes | HEALTHY |
| consolidation.py hash | main.sqlite (consolidation_file_hashes) | consolidation.py | Yes | HEALTHY |
| **synthesis.py (session path)** | **recall_text from inject_recall()** | **memory.inject_recall() → lucyd.py:778** | **Yes** | **HEALTHY (new)** |
| **synthesis.py (tool path)** | **recall_text from inject_recall()** | **memory_tools.tool_memory_search → recall()** | **Yes** | **HEALTHY (new)** |
| **memory_tools._synth_provider** | **provider instance** | **lucyd.py:825 set_synthesis_provider()** | **Yes** | **HEALTHY (new)** |
| **agentic._record_cost (synthesis)** | **synth_result.usage** | **synthesis.synthesize_recall()** | **Yes** | **HEALTHY (new)** |

## External Process Inventory

| Process | Type | Schedule | Exists? | Enabled? | Status |
|---------|------|----------|---------|----------|--------|
| lucyd.service | systemd | continuous | Yes | enabled+active | HEALTHY (PID 188447 active) |
| Workspace auto-commit | cron | :05 hourly | Yes | Yes | HEALTHY |
| lucyd-index | cron | :10 hourly | Yes | Yes | HEALTHY |
| lucyd-consolidate | cron | :15 hourly | Yes | Yes | HEALTHY |
| lucyd-consolidate --maintain | cron | 04:05 daily | Yes | Yes | HEALTHY |
| Trash cleanup | cron | 03:05 daily | Yes | Yes | HEALTHY |
| DB integrity check | cron | 04:05 weekly | Yes | Yes | HEALTHY |
| Heartbeat | cron | disabled | Documented | N/A | NOTED |

## Freshness Checks

| Data Source | Threshold | Last Write | Fresh? |
|-------------|-----------|-----------|--------|
| Memory chunks | 48h | 2026-02-24 02:10 | Yes |
| Cost DB | 24h | 2026-02-24 02:25 | Yes |
| Structured facts | 2h | 2026-02-24 01:15 | Yes |
| Consolidation state | Match sessions | 2026-02-24 00:39 | Yes |
| Episodes | 48h | 2026-02-24 | Yes |
| Session JSONL | Match conversation | 2026-02-24 02:25 | Yes |
| PID file | Current | Process running (PID 188447) | Yes |

## Round-Trip Test Coverage

| Pipeline | True Round-Trip? | Test File | Notes |
|----------|-----------------|-----------|-------|
| Memory: index → FTS search | Yes (partial) | test_indexer.py | FTS round-trip real. Vector search path not round-tripped. |
| Session: save → load | Yes | test_session.py | TestStateRoundTrip — real files, no mocks |
| Cost: record → query | Yes | test_cost.py | TestCostDBRoundTrip — real SQLite |
| Context: workspace → prompt | Yes | test_context.py | Real files on disk, includes mid-test mutation |
| extract_facts → lookup_facts | **Yes** | test_consolidation.py | **TestExtractThenLookupRoundTrip::test_facts_round_trip — real SQLite, extract writes + lookup reads** |
| extract_episode → search_episodes | **Yes** | test_consolidation.py | **TestExtractThenLookupRoundTrip::test_episodes_round_trip** |
| commitments → get_open_commitments | **Yes** | test_consolidation.py | **TestExtractThenLookupRoundTrip::test_commitments_round_trip** |
| memory_write tool → recall | No | test_memory_tools_structured.py + test_structured_recall.py | Halves tested separately, single schema source mitigates. |
| extract_facts → resolve_entity (aliases) | No | test_consolidation.py + test_structured_recall.py | Alias insertion verified by raw SQL; resolve_entity pre-seeded separately. |
| **Synthesis: recall → synthesize → output** | **Yes** | **test_synthesis.py** | **TestSynthesis (4 tests): mock provider, real inject_recall input, real footer preservation. TestToolPathSynthesis (3 tests): real SQLite + memory schema, real structured recall pipeline, mock provider for LLM.** |
| Telegram reaction round-trip | Yes | test_telegram_channel.py | TestReactionRoundTrip — timestamp encoding survives daemon conversion |

### Round-Trip Gap Resolution

**Resolved from Cycle 6:** Structured memory round-trips (facts, episodes, commitments) now have true cross-function tests in `TestExtractThenLookupRoundTrip`. Extract functions write to real SQLite; query functions read them back. Finding #1 from Cycle 6 is RESOLVED.

### Remaining Gaps

Two structured memory pipelines still lack true cross-function round-trips:
1. `memory_write` tool → `lookup_facts()` (halves tested separately)
2. `extract_facts()` aliases → `resolve_entity()` (halves tested separately)

Both are mitigated by single-source schema (`memory_schema.py`) and the new round-trip tests for the core extraction path. Risk: low.

## Findings

| # | Phase | Severity | Description | Status |
|---|-------|----------|-------------|--------|
| 1 | 4 | Low | Structured memory: no cross-function round-trip tests (extract → query) | **RESOLVED** — `TestExtractThenLookupRoundTrip` added (facts, episodes, commitments) |
| 2 | 4 | Low | Vector search path (`_search_vector`) has no round-trip test | CARRIED FORWARD from Cycle 5 |
| 3 | 4 | Low | `memory_write` tool → recall has no cross-function round-trip | CARRIED FORWARD (mitigated by shared schema) |
| 4 | 4 | Low | `extract_facts` aliases → `resolve_entity` has no cross-function round-trip | CARRIED FORWARD (mitigated by shared schema) |

No new findings from synthesis pipeline analysis. All synthesis data flows are:
- Connected (producers and consumers identified and verified)
- Error-handled (try/except at both call sites, fallback to raw recall)
- Tested (23 tests in `test_synthesis.py`, including tool path integration)
- Cost-tracked (session-start path records synthesis usage via `_record_cost`)

## Confidence

Overall confidence: 96%

All data pipelines have active producers. All external processes exist, are enabled, and running. All data sources within freshness thresholds. New synthesis pipelines verified end-to-end: config property chain traced, both call paths confirmed to use the same routed provider, footer preservation verified, error handling confirmed at both sites. Structured memory round-trip gap partially resolved (3 of 5 pipelines now have true round-trips). Remaining gaps are low-severity with schema-level mitigation.

Test suite: 1299 tests, all passing. Synthesis-specific: 23 tests (passthrough, fallback, synthesis, footer preservation, prompt registry, tool path integration).
