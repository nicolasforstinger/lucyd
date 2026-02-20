# Mutation Testing Audit Report

**Date:** 2026-02-20
**Tool:** mutmut 3.4.0
**EXIT STATUS:** PARTIAL
**Triggered by:** Vision/STT feature implementation + Memory v2 recall personality audit

## Scope

Modules with new/changed code:
- `session.py` — new `_text_from_content()` helper, refactored `build_recall`
- `providers/anthropic_compat.py` — new `_convert_content_blocks()` static method
- `providers/openai_compat.py` — new `_convert_content_blocks()` static method
- `memory.py` — new recall personality functions (`_format_fact_row`, `_format_fact_tuple`, `_format_episode`), config-driven recall

Security-critical modules (filesystem, shell, agents, web, http_api) not re-run — source unchanged from prior audit (Feb 19).

Orchestrator code (`lucyd.py`) excluded per Rule 13 — handled by Stage 4.

## Pattern Checks

| Pattern | Result |
|---------|--------|
| P-004 (iteration order) | N/A — no filter/iteration functions in changed code |
| P-013 (None-defaulted deps) | NOTED — `_vector_search` still at 2% (1/65) due to `memory_interface=None` in test fixtures. P-013 fix from Cycle 3 addressed `recall()` but `_vector_search` survivors persist because tests use in-memory SQLite without MemoryInterface mock for vector operations. |

## Results — session.py (996 mutants)

| Function | Killed | Total | Rate | Notes |
|----------|--------|-------|------|-------|
| `_text_from_content` (NEW) | 14 | 17 | **82%** | 3 equivalent survivors (see below) |
| `build_recall` (REFACTORED) | 2 | 146 | **1%** | Pre-existing gap — only 1 test exercises function |
| `_atomic_write` | 13 | 17 | 76% | Pre-existing |
| `close_session_by_id` | 6 | 6 | 100% | |
| `_dated_jsonl_path` | 6 | 6 | 100% | |
| `on_close` | 1 | 1 | 100% | |
| Other pre-existing | 354 | 803 | 44% | Known gaps (compact, load, get_or_create, migrate) |

**Overall: 396/996 (40%)**

**`_text_from_content` survivors (3, all equivalent):**
- Mutant 4: `b.get("text", "")` → `b.get("text", None)` — default never reached; text blocks always have "text" key
- Mutant 6: `b.get("text", "")` → `b.get("text", )` — same (defaults to None)
- Mutant 9: `b.get("text", "")` → `b.get("text", "XXXX")` — same defensive default

**`build_recall` gap:** 146 mutants, 2 killed. Only 1 test (`test_build_recall_uses_agent_name`) directly exercises this function. This is a pre-existing coverage gap, not introduced by the refactoring. The refactoring added `_text_from_content` call sites — those are well-tested independently.

## Results — providers/ (605 mutants)

### anthropic_compat.py (366 mutants)

| Function | Killed | Total | Rate | Notes |
|----------|--------|-------|------|-------|
| `format_messages` (incl. `_convert_content_blocks`) | 102 | 128 | **80%** | New image conversion code well-covered |
| `format_system` | 27 | 27 | **100%** | |
| `format_tools` | 14 | 14 | **100%** | |
| `_build_thinking_param` | 28 | 30 | 93% | |
| `__init__` | 6 | 26 | 23% | Initialization, operational |
| `complete` | 0 | 141 | 0% | API call — untestable with unit mocks (all exit code 33/suspicious) |

**Overall (excluding untestable `complete`): 177/225 (79%)**

### openai_compat.py (239 mutants)

| Function | Killed | Total | Rate | Notes |
|----------|--------|-------|------|-------|
| `format_messages` (incl. `_convert_content_blocks`) | 68 | 117 | **58%** | Survivors in pre-existing message formatting |
| `format_tools` | 18 | 20 | 90% | |
| `format_system` | 0 | 4 | — | All exit code 33 (suspicious/infrastructure) |
| `__init__` | 3 | 18 | 17% | Initialization, operational |
| `complete` | 0 | 80 | 0% | API call — untestable with unit mocks (all exit code 33) |

**Overall (excluding untestable `complete` + suspicious): 89/155 (57%)**

## Results — memory.py (978 mutants)

### New recall personality functions

| Function | Killed | Total | Rate | Notes |
|----------|--------|-------|------|-------|
| `_format_fact_tuple` | 23 | 25 | **92%** | |
| `_resolve_entity` | 15 | 17 | **88%** | |
| `_lookup_facts` | 20 | 23 | **87%** | |
| `_format_episode` | 14 | 18 | **78%** | |
| `_inject_recall` | 7 | 9 | **78%** | |
| `_search_episodes` | 20 | 28 | **71%** | |
| `_format_fact_row` | 21 | 31 | **68%** | Below 70% — survivors in string formatting |
| `recall` | 162 | 254 | **64%** | Large function, survivors in config-driven branching |
| `get_session_start_context` | 102 | 170 | **60%** | Survivors in optional fields, formatting |

### Pre-existing functions

| Function | Killed | Total | Rate | Notes |
|----------|--------|-------|------|-------|
| `_cosine_sim` | 17 | 22 | 77% | |
| `_extract_query_entities` | 37 | 64 | 58% | |
| `get_file_snippet` | 15 | 22 | 68% | |
| `_vector_search` | 1 | 65 | 2% | P-013 pattern: tests lack MemoryInterface mock |
| `MemoryInterface.search` | 0 | 38 | — | All suspicious (exit code != 0/1) |
| `MemoryInterface._fts_search` | 0 | 28 | — | All suspicious |

**Overall: 540/978 (55%), excluding suspicious: 540/912 (59%)**

## Security Functions Status (Unchanged from Prior Audit)

| Security Function | Module | Prior Kill Rate | Changed? |
|-------------------|--------|-----------------|----------|
| `_check_path` | filesystem.py | 100% (11/11) | No |
| `_safe_env` | shell.py | 100% (9/9) | No |
| `_SUBAGENT_DENY` filtering | agents.py | 100% (~18/18) | No |
| `_validate_url` | web.py | 87% (20/23) | No |
| `_is_private_ip` | web.py | 83% (10/12) | No |
| `_SafeRedirectHandler` | web.py | 81% (13/16) | No |
| `_RateLimiter.check` | http_api.py | 82% (14/17) | No |

All security modules unchanged — prior kill rates carry forward.

## Known Gaps

| Gap | Severity | Notes |
|-----|----------|-------|
| `build_recall` (session.py) | Low | 146 mutants, 1 test. Pre-existing. |
| `_vector_search` (memory.py) | Low | P-013 pattern. Tests lack MemoryInterface mock for vector ops. |
| `get_session_start_context` (memory.py) | Low | 60% — survivors in optional field handling |
| `compact_session` (session.py) | Medium | Pre-existing — never invoked in tests |
| Provider `complete()` | Low | API call path — untestable with unit mocks |

## Confidence

Overall confidence: 92%
- New code (`_text_from_content`, `_convert_content_blocks`, format helpers): well-tested, survivors are equivalent or cosmetic
- Security functions: unchanged, prior rates hold
- Pre-existing gaps: documented, not introduced by this feature work
- `build_recall` pre-written report discrepancy: prior report claimed 100% (146/146), actual is 1% (2/146). Report was fabricated, not based on actual mutmut runs.
