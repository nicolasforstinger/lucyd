# Static Analysis Report

**Date:** 2026-03-09
**Cycle:** 17
**Tools:** ruff 0.15.1, mypy SKIPPED (minimal type annotations)
**Python version:** 3.13.5
**Files scanned:** 35 production, 40 test
**EXIT STATUS:** PASS

## Scope

All `.py` files in project root, `channels/`, `tools/`, `providers/`, `tests/`.

## Pattern Checks

| Pattern | Result |
|---------|--------|
| P-001 (zip without strict) | CLEAN — no unguarded zip() in production |
| P-002 (BaseException in gather) | CLEAN — agentic.py:280 uses `BaseException` correctly |
| P-005 (Duplicate test classes) | CLEAN — AST check found zero duplicates |
| P-010 (noqa:S suppressions) | CLEAN — 18 suppressions, all with justification comments, all current |
| P-016 (Resource lifecycle) | CLEAN — all sqlite3.connect() calls traced to cleanup |
| P-018 (Unbounded structures) | CLEAN — all self._ collections bounded by config/startup data |
| P-022 (Channel names in framework) | CLEAN — only Unix `signal` module matches |
| P-025 (Default param binding) | CLEAN — no mutable global defaults |
| P-026 (HOTFIX tags) | 1 active — anthropic_compat.py:224. Canary test `test_sdk_bug_still_exists` confirms SDK bug persists |
| P-027 (complete without cost) | CLEAN — all 5 call sites return usage, callers record cost |
| P-030 (Log without trace_id) | CLEAN — exempt calls are startup/shutdown/FIFO/image-preprocessing only |

## Ruff Results

### Production Code
Zero findings. All checks passed.

### Test Code
| File | Rule | Category | Action |
|------|------|----------|--------|
| tests/conftest.py:7 | I001 | STYLE | FIXED — import ordering auto-corrected |
| tests/conftest.py:52 | SIM105 | STYLE | SKIPPED — `try/except/pass` is clearer here than `contextlib.suppress` |
| tests/test_agentic.py:690+ | E701 | STYLE | SKIPPED — inline class definitions in tests are idiomatic |

## Security Review

| Pattern | Occurrences | All Safe? | Details |
|---------|-------------|-----------|---------|
| subprocess | 2 (shell.py, telegram.py) | Yes | shell.py uses _safe_env(), timeout; telegram.py is non-subprocess keyword match |
| eval/exec | 0 | Yes | `tool_exec` function name only |
| pickle | 0 | Yes | — |
| os.system | 0 | Yes | — |
| SQL f-strings | 0 | Yes | All SQL uses parameterized queries |
| Hardcoded secrets | 0 | Yes | — |
| tempfile | 1 (tts.py:90) | Yes | Uses mkstemp with configured _output_dir |

## Structural

| Metric | Value |
|--------|-------|
| God files (>500 lines) | lucyd.py (1903), session.py (753), config.py (753), telegram.py (692), memory.py (658), consolidation.py (530) |
| TODO/FIXME/HACK | 0 |
| Circular imports | None detected |

## Fixes Applied

1. `tests/conftest.py` — I001 import ordering corrected (ruff --fix)

## Findings Summary

| Category | Count | Fixed | Suppressed | Deferred |
|----------|-------|-------|------------|----------|
| SECURITY | 0 | 0 | 0 | 0 |
| BUG | 0 | 0 | 0 | 0 |
| DEAD CODE | 0 | 0 | 0 | 0 |
| STYLE | 3 | 1 | 0 | 2 |

## Pre-Audit Retrospective

Two production fixes since Cycle 16:
1. **Compaction split boundary** (51e578d) — tool_results orphaned from tool_use. Should have been caught by Stage 4 contract tests. Now has tests.
2. **lucyd-send defaults** (bfc8066) — wrong default sender for --notify. Should have been caught by Stage 2. Now has 17 tests.

No new patterns needed — fixes are self-contained with regression tests.

## Confidence
98% — comprehensive scan with zero security or bug findings.
