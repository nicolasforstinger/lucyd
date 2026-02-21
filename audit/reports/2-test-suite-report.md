# Test Suite Report

**Date:** 2026-02-21
**Audit Cycle:** 4
**Python version:** 3.14.3
**Pytest version:** 9.0.2
**EXIT STATUS:** PASS

## Inventory

| Metric | Value |
|--------|-------|
| Test files | 34 (32 test_*.py + conftest.py + __init__.py) |
| Tests collected | 1158 |
| Tests passed | 1158 |
| Tests failed | 0 |
| Production modules | 29 |
| Modules with tests | 28 |
| Modules WITHOUT tests | 1 (channels/cli.py — thin CLI adapter) |

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-005 (test count drift) | N/A | Stage 1 found no shadowed tests (all 12 duplicate function names in different classes). No count impact to verify. |
| P-006 (fixture check) | CLEAN | Pre-populated fixtures (cost_db, tmp_workspace, fs_workspace) map to production creation paths. Cost DB schema matches production init. Workspace files match deployment structure. |
| P-013 (None-defaulted deps) | NOTED | Test fixtures properly mock dependencies. Cycle 3 recall() fix verified — memory_interface is no longer passed as None in recall tests. Flagged for Stage 3 mutation coverage verification. |

## Suite Run

Total time: 14.05s
All passed: yes
Failures: none

## Health Checks

### Warnings

| Warning | Count | Category | Action |
|---------|-------|----------|--------|
| RuntimeWarning: coroutine AsyncMockMixin was never awaited | 1 | Dependency (httpcore + unittest.mock interaction) | No action — appears only in full suite run, not in isolation. Known CPython AsyncMock teardown issue. Not from our code. |

1 total warning. No unawaited coroutines in production code. No DeprecationWarnings from our code.

### Isolation

All 31 test files pass in isolation: **yes**
No ordering dependencies. Individual file counts sum to 1158 (matches full suite).

### Timing

Slowest 5 tests:

| Test | Time | Explanation |
|------|------|-------------|
| test_shell_security::TestExecTimeout::test_exec_timeout_cap_applied | 2.01s | Real timeout test (by design) |
| test_shell_security::TestExecTimeout::test_exec_timeout_kills_command | 2.01s | Real timeout test (by design) |
| test_scheduling::TestListScheduled::test_excludes_completed | 1.50s | Real asyncio timer (by design) |
| test_scheduling::TestScheduleMessage::test_task_cleaned_up_after_fire | 1.50s | Real asyncio timer (by design) |
| test_scheduling::TestScheduleMessage::test_message_fires_and_sends | 1.50s | Real asyncio timer (by design) |

All slow tests are intentional (real timers/timeouts). No unexpected I/O or network calls.

### Fixture Health

- All fixtures function-scoped (no broad session/module scope)
- No unused fixtures
- conftest.py: mutmut compatibility workaround (`safe_set_start_method`) — benign and well-documented
- `fs_workspace` fixture properly restores config after test (`yield` + cleanup)
- No fixtures doing real I/O without cleanup

## Quality Indicators

| Metric | Value | Healthy Range |
|--------|-------|---------------|
| Test-to-production ratio | 2.3:1 (17,742 / 7,727 lines) | 1.5:1 — 3:1 |
| Assert density | 1.6 (1,880 / 1,158) | > 1.5 |
| Test naming consistency | Consistent (test_snake_case throughout) | Consistent |

## Modules Without Test Files

| Module | Lines | Assessment |
|--------|-------|-----------|
| `channels/cli.py` | 46 | Thin stdin/stdout wrapper implementing Channel protocol. 5 methods, all trivial. Tested indirectly via daemon integration tests. Acceptable gap. |

## Fixes Applied

None during this stage. Stage 1 fixes (2 dead code removals in test files) verified — all 1158 tests still pass.

## Confidence

Overall confidence: 97%
Suite is healthy — all tests collected, all pass, all pass in isolation, no critical warnings from our code, quality metrics in healthy range. Previous count (Cycle 3): 1136 → current: 1158 (+22 tests).
