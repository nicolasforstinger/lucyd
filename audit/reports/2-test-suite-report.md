# Test Suite Report

**Date:** 2026-03-12
**Cycle:** 18
**Python version:** 3.13.5
**Pytest version:** 8.x (pytest-asyncio enabled)
**EXIT STATUS:** PASS

## Inventory

| Metric | Value |
|--------|-------|
| Test files | 38 (+ conftest.py, __init__.py) |
| Tests collected | 1721 |
| Tests passed | 1721 |
| Tests failed | 0 |
| Production modules | 34 |
| Modules with tests | 33 |
| Modules WITHOUT tests | 1 (plugins.d/devices.py — deployment-specific, gitignored) |
| Collection errors | 0 |

## Pattern Checks

| Pattern | Result |
|---------|--------|
| P-005 shadowed test classes | Not applicable — no fixes from Stage 1 |
| P-006 dead data pipeline | No pre-populated fixtures without production write paths found |
| P-013 None-defaulted dependency | All `=None` patterns are test helper defaults or early-exit guard tests — no hidden untested branches |
| P-016 ResourceWarning | 3 warnings found — 2 sqlite3 (mock teardown), 1 unclosed file (test_session.py:1590). See Warnings section |

## Suite Run

Total time: 33.78s
All passed: Yes
Failures: None

## Health Checks

### Warnings

93 total warnings. Breakdown:

| Warning | Count | Category | Action |
|---------|-------|----------|--------|
| `RuntimeWarning: coroutine 'AsyncMockMixin._execute_mock_call' was never awaited` | 13 | CPython 3.13 stdlib (unittest.mock) | Not actionable — known AsyncMock behavior |
| `ResourceWarning: unclosed database` (sqlite3.Connection) | 2 | Test teardown (mock paths) | Low — mock cleanup, not production |
| `ResourceWarning: unclosed file` (test_session.py:1590) | 1 | Test code — bare `open()` without `with` | Low — should use context manager |
| Various pytest/asyncio internal warnings | ~77 | Framework infrastructure | Not actionable |

**Critical warnings:** None. All "coroutine never awaited" warnings trace to `unittest.mock.AsyncMockMixin` internals (Python 3.13), not to our async code. ResourceWarnings are in test teardown, not production paths.

### Isolation

All files pass in isolation: **Yes**
38/38 test files pass independently. Sum of individual file counts = 1721 (matches batch run).

### Reverse Order

Pass in reverse order: **Yes** (1721 passed, 33.02s)

### Timing

| Test | Time | Notes |
|------|------|-------|
| test_orchestrator::TestImageFitting::test_jpeg_quality_reduction | 2.29s | Real JPEG encoding (PIL) |
| test_lucyd_send::TestNotifyFlag::test_notify_invalid_data_exits | 2.05s | Subprocess timeout |
| test_shell_security::TestExecTimeout::test_exec_timeout_kills_command | 2.00s | Intentional timeout test |
| test_shell_security::TestExecTimeout::test_exec_timeout_cap_applied | 2.00s | Intentional timeout test |
| test_scheduling (3 tests) | 1.50s each | Real asyncio.sleep timers |
| test_shell_security::TestExecTimeoutProcessEnforcement (3 tests) | 1.00s each | Real process kill tests |

All slow tests are intentional (timeout enforcement, real I/O operations, scheduling timers). No unexpected latency. Full suite completes in ~33s for 1721 tests.

### Fixture Health

- No unused fixtures (false positives from grep: `autouse=True` fixtures, dataclass `__post_init__`, decorator helpers)
- Broad-scope fixtures: None (all fixtures are function-scope)
- conftest.py: `pytest_unconfigure` hook with `os._exit()` — required to prevent asyncio thread hangs. Documented and intentional.
- 3 conditional `pytest.skip()` calls — all with valid reasons (missing tools/ directory, symlink capability)

### Test Rot Detection

**Tests without assertions:** 17 functions detected. All verified as "doesn't crash" tests — they verify that error paths, cleanup paths, or edge cases complete without raising exceptions. Examples: `test_init_idempotent`, `test_remove_missing_no_error`, `test_stop_without_start`, `test_disconnect_idempotent`. These are valid negative tests (verify resilience), not dead tests.

**Permanently skipped tests:** 3 conditional skips — all environment-gated (`tools/` not found, symlink creation impossible). Valid.

**Stale imports:** None found — all test imports resolve to existing production code.

## Quality Indicators

| Metric | Value | Healthy Range |
|--------|-------|---------------|
| Test-to-production ratio | 2.64:1 | 1.5:1 — 3:1 |
| Assert density | 1.62 | > 1.5 |
| Test naming consistency | Consistent | Consistent / Mixed |
| Production lines | 10,464 | — |
| Test lines | 27,644 | — |

## Modules Without Test Files

| Module | Assessment |
|--------|------------|
| `plugins.d/devices.py` | Deployment-specific plugin (gitignored). Lucy's BLE device control. Not framework code. |

## Known Gaps

| Gap | Severity | Notes |
|-----|----------|-------|
| ResourceWarning: unclosed file in test_session.py:1590 | Low | Bare `open()` should use `with` block |
| 2 unclosed sqlite3 connections in test teardown | Low | Mock paths, not production |
| 17 assertion-free tests | Low | All verified as valid "doesn't crash" negative tests |

## Fixes Applied

None required. All tests pass clean.

## Confidence

Overall: **95%**

All 4 confidence gates met:
1. Every test file collected — verified (1721 collected, 0 errors)
2. Tests pass in isolation — verified (38/38 files pass independently)
3. No warnings indicate real problems — verified (all AsyncMock/CPython internals)
4. Test count matches expectations — verified (1721 vs ~1725 documented — 4 tests removed in prior config refactoring, accounted for)
