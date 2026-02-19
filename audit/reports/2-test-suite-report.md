# Test Suite Report

**Date:** 2026-02-19
**Python version:** 3.13.5
**Pytest version:** 9.0.2
**EXIT STATUS:** PASS

## Inventory

| Metric | Value |
|--------|-------|
| Test files | 32 (30 test_*.py + conftest.py + __init__.py) |
| Tests collected | 1020 |
| Tests passed | 1020 |
| Tests failed | 0 |
| Production modules | 29 |
| Modules with tests | 28 |
| Modules WITHOUT tests | 1 (channels/cli.py) |

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-005 (shadowed tests) | CLEAN | No true duplicates — Stage 1 found 4 same-named functions across different classes (not shadows) |
| P-006 (fixture check) | NOTED | Fixtures create test DBs (cost_db, mem_conn in test files). Production producers: cost tracking writes to cost.db (lucyd.py), memory indexer writes to main.sqlite (lucyd-index). Both active. Round-trip coverage exists in test_indexer.py. |
| P-013 (None-defaulted deps) | NOTED | `memory_interface=None` in recall() tests — flagged for Stage 3 (known from Cycle 3) |

## Suite Run

Total time: 15.74s
All passed: Yes
Failures: None

## Health Checks

### Warnings

| Warning | Count | Category | Action |
|---------|-------|----------|--------|
| RuntimeWarning: coroutine AsyncMockMixin._execute_mock_call was never awaited | ~38 | CPython 3.13 AsyncMock known issue | No action — from unittest.mock internals, not our code |
| ResourceWarning: unclosed database | 1 | Mock artifact | No action — from mock object lifecycle |

No critical warnings (no unawaited coroutines in OUR code, no resource leaks in production code).

### Isolation

All 30 test files pass in isolation: **Yes**
No order dependencies detected.

### Timing

| Test | Time | Explanation |
|------|------|-------------|
| test_shell_security::TestExecTimeout (x2) | 2.0s each | Intentional — testing timeout behavior |
| test_scheduling::TestScheduleMessage (x3) | 1.5s each | Intentional — testing delay/scheduling |
| test_scheduling::TestScheduleFireAndCleanup (x3) | 0.3-0.4s each | Intentional — timer tests |

All slow tests are intentional timeout/delay tests. No real I/O, no network calls. Suite total 15.74s for 1020 tests.

### Fixture Health

- All fixtures use `tmp_path` (pytest temp directory)
- No broad-scope fixtures (`session` or `module`)
- `fs_workspace` properly yields and restores state
- `cost_db` properly closes connection
- conftest.py has mutmut compatibility workaround (safe_set_start_method) — benign
- No unused fixtures detected

## Quality Indicators

| Metric | Value | Healthy Range |
|--------|-------|---------------|
| Test-to-production ratio (lines) | 2.2:1 (15,463 / 7,041) | 1.5:1 — 3:1 |
| Assert density | 1.6 (1,618 / 1,020) | > 1.5 |
| Test naming consistency | Consistent (test_snake_case throughout) | Consistent |

## Modules Without Test Files

| Module | Assessment |
|--------|-----------|
| `channels/cli.py` | CLI channel adapter — thin wrapper around lucyd-send. Tested indirectly via `test_lucyd_send.py`. Acceptable gap. |

## Fixes Applied

None — suite was healthy, no fixes needed.

## Confidence

Overall confidence: 97%
Minor note: AsyncMock RuntimeWarnings are noise from CPython 3.13, not actionable. All substantive checks pass.
