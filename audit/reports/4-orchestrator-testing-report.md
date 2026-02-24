# Orchestrator Testing Report

**Date:** 2026-02-24
**Audit Cycle:** 7
**Target:** lucyd.py (1,615 lines)
**EXIT STATUS:** PASS

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-017 (crash-unsafe state) | 1 LOW | `pending_system_warning = ""` at line 716 not persisted until `_save_state()` at line 943. Full agentic loop runs between. Crash would re-inject warning on restart. Benign — worst case is a duplicated context warning, not data loss. Unchanged from Cycle 6. |

### P-017 Detail

| Site | Lines | Mutation | Operations Before Persist | Risk |
|------|-------|----------|---------------------------|------|
| Warning consumption | 716 → 943 | `pending_system_warning = ""` | Full agentic loop (async, minutes) | LOW — duplicated warning on crash, no data loss |
| Warning save after clear | 717 | `_save_state()` | None | Safe — immediate persist of cleared warning |
| Error path cleanup | 912–915 | `messages.pop()` + `_save_state()` | None | Safe |
| Success path save | 940–943 | image content restore + `_save_state()` | None | Safe |
| Warning set | 998–1005 | `pending_system_warning`, `warned_about_compaction`, `_save_state()` | None | Safe |

## Phase 1: Architecture Map

Extracted decision functions: 6 (unchanged from Cycle 5)
- `_should_warn_context` (line 126) — context length warning
- `_should_deliver` (line 146) — delivery routing
- `_inject_warning` (line 151) — system warning prepend
- `_is_silent` (line 161) — silent token matching
- `_fit_image` (line 189) — image size/quality reduction
- `_extract_document_text` (line 237) — document text extraction

Inline decisions in `_process_message` (lines 608–1044): 45
Components mocked: provider, channel, session_mgr, tool_registry, config, cost_db, _get_memory_conn

### Changes Since Cycle 6 (Synthesis Wiring)

| Change | Location | Test Coverage |
|--------|----------|---------------|
| Session-start recall synthesis | Lines 776–796 | TestMemoryV2Wiring (structured recall tests), TestSynthesis (synthesis layer tests) |
| Per-message `set_synthesis_provider` | Lines 822–825 | TestToolPathSynthesis (3 tests in test_synthesis.py) |
| `set_synthesis_provider()` API in memory_tools | `tools/memory_tools.py:35` | TestToolPathSynthesis: configured, structured, no-provider |
| Tool-path synthesis in `tool_memory_search` | `tools/memory_tools.py:56–63` | TestToolPathSynthesis: synthesis applied, fallback on failure |
| Cost recording for synthesis | Lines 787–794 | Tested via provider mock in TestSynthesis |
| `_last_inbound_ts` eviction | Lines 1322–1325 | `test_eviction_at_1001_entries`, `test_reaccess_does_not_grow_beyond_limit` (gap closed) |

### Synthesis Wiring Architecture

Two synthesis paths exist, both covered by tests:

**Path 1 — Session start (lines 776–796):** When a fresh session starts (`len(session.messages) <= 1`), recall text is synthesized before injection into the system prompt. Uses the routed provider for this message. Cost is recorded via `_record_cost`. On failure, falls back to raw recall text. Guarded by `config.recall_synthesis_style != "structured"`.

**Path 2 — Tool invocation (lines 822–825 + memory_tools.py:56–63):** Before the agentic loop, `set_synthesis_provider(provider)` wires the current routed provider into `memory_tools._synth_provider`. When the agent calls `memory_search` during the loop, `tool_memory_search` uses this provider for synthesis. Critical design decision: provider is set **per-message** in `_process_message`, not at startup in `_setup_tools`. This ensures the synthesis provider always matches the routed model for the current message (which may differ by source — e.g., vision routing overrides).

**Fallback chain:** Both paths follow the same pattern: try synthesis, on any exception log warning and return raw recall text. The tool path has an additional fallback: if structured recall itself fails, it falls back to direct vector search.

## Phase 2: Extractions

| Function | Purpose | Tests | Status |
|----------|---------|-------|--------|
| `_should_warn_context` | Context length warning threshold | TestShouldWarnContext (7) | PASS |
| `_should_deliver` | Delivery routing by source | TestShouldDeliver (7) | PASS |
| `_inject_warning` | System warning injection | TestInjectWarning (4) | PASS |
| `_is_silent` | Silent token suppression | TestIsSilentExtended (9) | PASS |
| `_fit_image` | Image resizing for API limits | TestImageFitting (5) | PASS |
| `_extract_document_text` | PDF/text extraction | TestExtractDocumentText (11) | PASS |

No new extractions needed. The synthesis wiring decisions (style check, provider null check) are thin guards — 1–2 line conditionals that don't warrant extraction into separate functions. They are tested through the tool-path integration tests.

## Phase 3: Contract Tests

| Category | Tests | Class(es) | Status |
|----------|-------|-----------|--------|
| Basic message flow | 3 | TestBasicMessageFlow | PASS |
| Error handling | 4 | TestProviderErrorHandling | PASS |
| Typing indicators | 4 | TestTypingIndicators | PASS |
| Silent token suppression | 2 | TestSilentTokenSuppression | PASS |
| Delivery suppression | 12 | TestDeliverySuppression, TestChannelDeliverySuppression | PASS |
| Warning injection | 3 | TestWarningInjection | PASS |
| Compaction | 6 | TestCompactionWarning, TestHardCompaction | PASS |
| HTTP future resolution | 4 | TestHTTPFutureResolution | PASS |
| Message persistence | 3 | TestMessagePersistence | PASS |
| Memory v2 wiring | 10 | TestMemoryV2Wiring, TestConsolidateOnClose | PASS |
| Synthesis wiring | 3 | TestToolPathSynthesis (in test_synthesis.py) | PASS |

Additional coverage: TestResolvePattern, TestResolveIntegration, TestMessageLoopDebounce, TestTranscribeAudio, TestBuildSessions, TestBuildCost, TestFireWebhook, TestFifoAttachmentReconstruction, TestConsecutiveUserMessageMerge, TestErrorRecoveryOrphanedMessages, TestDocumentExtractionIntegration, TestImageFitting, TestInboundTimestampCapture.

### Synthesis Contract Details

| Contract | Test | Verified |
|----------|------|----------|
| Provider passed correctly to `synthesize_recall` at session start | TestSynthesis::test_narrative_calls_provider | Provider's `complete` called with synthesis prompt |
| Provider wired per-message via `set_synthesis_provider` | TestToolPathSynthesis::test_synthesis_applied_when_configured | `synth_provider.complete.assert_awaited_once()` |
| Fallback to raw recall on synthesis failure | TestFallback::test_provider_failure_returns_raw | Exception in provider returns raw text |
| No synthesis when style is "structured" | TestToolPathSynthesis::test_no_synthesis_when_structured | `synth_provider.complete.assert_not_called()` |
| No synthesis when no provider set | TestToolPathSynthesis::test_no_synthesis_without_provider | Returns raw recall without crash |
| Cost recorded for synthesis | TestSynthesis::test_narrative_calls_provider | Usage returned in SynthesisResult |
| Footer preservation through synthesis | TestFooterPreservation (3 tests) | Memory-loaded and dropped footers preserved |

## Test Counts

| File | Count |
|------|-------|
| test_orchestrator.py | 91 |
| test_daemon_integration.py | 100 |
| test_daemon_helpers.py | 15 |
| test_monitor.py | 33 |
| test_synthesis.py | 23 (3 tool-path, 20 synthesis layer) |
| **Orchestrator total** | **262** |

All 262 pass. Full suite: 1,299 tests, all passing.

## _process_message Metrics

| Metric | Value |
|--------|-------|
| Lines (method body) | 437 (lines 608–1044) |
| Inline decisions | 45 |
| Extracted decisions | 6 functions |
| lucyd.py total | 1,615 lines |

## Known Gaps

| Gap | Severity | Status |
|-----|----------|--------|
| `pending_system_warning` consumption persist delay | Low | P-017 finding. Unchanged. Duplicated warning on crash, not data loss. |
| `_message_loop` (debounce, FIFO) | Medium | Open since Cycle 3. Partially covered by TestMessageLoopDebounce (11 tests). |
| Session-start synthesis cost recording not tested end-to-end | Low | Cost recording logic tested in test_agentic.py. Synthesis path calls `_record_cost` which is tested separately. |

### Gaps Closed Since Cycle 6

| Gap | Resolution |
|-----|-----------|
| `_last_inbound_ts` eviction at 1000 entries | Now tested: `test_eviction_at_1001_entries` and `test_reaccess_does_not_grow_beyond_limit` in test_daemon_integration.py |

## Confidence

Overall confidence: 95%

All 10 original contract categories plus the new synthesis wiring category covered. All 6 extracted functions tested. Synthesis wiring tested through both the session-start path (test_synthesis.py) and the per-message tool path (TestToolPathSynthesis). The per-message provider wiring pattern (line 822–825) is architecturally correct — it ensures model routing consistency when vision or source-based routing overrides the default provider. One eviction gap from Cycle 6 is now closed. No new extractions needed. No regressions.
