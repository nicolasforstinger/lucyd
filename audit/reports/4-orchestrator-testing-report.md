# Orchestrator Testing Report

**Date:** 2026-02-23
**Audit Cycle:** 6
**Target:** lucyd.py (1,571 lines)
**EXIT STATUS:** PASS

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-017 (crash-unsafe state) | 1 LOW | `pending_system_warning = ""` at line 711 not persisted until `_save_state()` at line 899. Full agentic loop runs between. Crash would re-inject warning on restart. Benign — worst case is a duplicated context warning, not data loss. |

### P-017 Detail

| Site | Lines | Mutation | Operations Before Persist | Risk |
|------|-------|----------|---------------------------|------|
| Warning consumption | 711 → 899 | `pending_system_warning = ""` | Full agentic loop (async, minutes) | LOW — duplicated warning on crash, no data loss |
| Error path cleanup | 869–871 | `messages.pop()` | None | Safe |
| Success path save | 896–899 | image content restore | None | Safe |
| Warning set | 954–961 | `pending_system_warning`, `warned_about_compaction` | None | Safe |

The compaction state persistence (original P-017 finding from session.py) was fixed in the hardening batch — `_save_state()` now called immediately after state mutation. The lucyd.py warning consumption gap is new and lower severity.

## Phase 1: Architecture Map

Extracted decision functions: 6 (unchanged from Cycle 5)
- `_should_warn_context` (line 126) — context length warning
- `_should_deliver` (line 146) — delivery routing
- `_inject_warning` (line 151) — system warning prepend
- `_is_silent` (line 161) — silent token matching
- `_fit_image` (line 189) — image size/quality reduction
- `_extract_document_text` (line 237) — document text extraction

Inline decisions in `_process_message` (lines 603–1000): 32
Components mocked: provider, channel, session_mgr, tool_registry, config, cost_db, _get_memory_conn

### Changes Since Cycle 5 (Hardening Batch)

| Change | Location | Test Coverage |
|--------|----------|---------------|
| `_memory_conn` close in `finally` block | `run()` | `_get_memory_conn` mocked in 7 orchestrator tests |
| `channel.disconnect()` call in `finally` block | `run()` | 4 tests in test_telegram_channel.py (close, idempotent, clean dir, closed client) |
| `_last_inbound_ts` → `OrderedDict` with eviction | Lines 295, 1322-1325 | 3 basic tests in test_daemon_integration.py. **Gap: no eviction test** |
| Config params `api_retries`, `api_retry_base_delay` | Passed to agentic loop | Tested in test_agentic.py (retry logic) |

## Phase 2: Extractions

| Function | Purpose | Tests | Status |
|----------|---------|-------|--------|
| `_should_warn_context` | Context length warning threshold | TestShouldWarnContext | PASS |
| `_should_deliver` | Delivery routing by source | TestShouldDeliver | PASS |
| `_inject_warning` | System warning injection | TestInjectWarning | PASS |
| `_is_silent` | Silent token suppression | TestIsSilentExtended | PASS |
| `_fit_image` | Image resizing for API limits | TestImageFitting | PASS |
| `_extract_document_text` | PDF/text extraction | TestExtractDocumentText | PASS |

No new extractions needed. No new decision points added.

## Phase 3: Contract Tests

| Category | Tests | Class(es) | Status |
|----------|-------|-----------|--------|
| Basic message flow | 10+ | TestBasicMessageFlow | PASS |
| Error handling | 10+ | TestProviderErrorHandling | PASS |
| Typing indicators | 10+ | TestTypingIndicators | PASS |
| Silent token suppression | 8+ | TestSilentTokenSuppression | PASS |
| Delivery suppression | 15+ | TestDeliverySuppression, TestChannelDeliverySuppression | PASS |
| Warning injection | 8+ | TestWarningInjection | PASS |
| Compaction | 12+ | TestCompactionWarning, TestHardCompaction | PASS |
| HTTP future resolution | 10+ | TestHTTPFutureResolution | PASS |
| Message persistence | 10+ | TestMessagePersistence | PASS |
| Memory v2 wiring | 15+ | TestMemoryV2Wiring, TestConsolidateOnClose | PASS |

Additional coverage: TestResolvePattern, TestResolveIntegration, TestMessageLoopDebounce, TestTranscribeAudio, TestBuildSessions, TestBuildCost, TestFireWebhook, TestFifoAttachmentReconstruction, TestConsecutiveUserMessageMerge, TestErrorRecoveryOrphanedMessages, TestDocumentExtractionIntegration, TestImageFitting, TestInboundTimestampCapture.

## Test Counts

| Type | Count |
|------|-------|
| test_daemon_helpers.py | 15 |
| test_daemon_integration.py | 94 (+1 from Cycle 5) |
| test_orchestrator.py | 90 |
| test_monitor.py | 33 |
| **Total** | **232** |

All 232 pass.

## Known Gaps

| Gap | Severity | Status |
|-----|----------|--------|
| `_last_inbound_ts` eviction at 1000 entries | Low | No dedicated test. Implementation verified in source (lines 1322-1325). |
| `pending_system_warning` consumption persist delay | Low | P-017 finding. Duplicated warning on crash, not data loss. |
| `_message_loop` (debounce, FIFO) | Medium | Open since Cycle 3. |

## Confidence

Overall confidence: 95%

All 10 contract categories covered. All 6 extracted functions tested. Hardening batch changes tested through component tests (disconnect, retry, memory_conn). One new P-017 finding (warning persist delay) is low severity. No new extractions needed.
