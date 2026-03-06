# Static Analysis Report

**Date:** 2026-03-06
**Audit Cycle:** 16
**Tools:** ruff 0.15.1, mypy SKIPPED (minimal type annotations)
**Python version:** 3.13.5
**Files scanned:** 34 production, 40 test
**EXIT STATUS:** PASS

## Scope

All `.py` files in project root, `channels/`, `tools/`, `providers/`, `plugins.d/`. Tests in `tests/`. New this cycle: `verification.py`, `tests/test_verification.py`.

## Configuration

ruff.toml: S, E, F, W, B, UP, SIM, RET, PTH, I, TID enabled. S603/S607/E501/S608 ignored. Tests exempt from S101/S104/S105/S106/S108/S310.

## Pattern Checks

| Pattern | Result |
|---------|--------|
| P-001 zip() strict | CLEAN |
| P-005 Duplicate test names | CLEAN |
| P-010 noqa:S suppressions | CLEAN (2 S608 in memory.py — verified parameterized) |
| P-020 Magic numbers | CLEAN |
| P-021 Provider defaults | CLEAN |
| P-022 Channel names | CLEAN |
| P-025 Default parameter binding | CLEAN |
| P-026 HOTFIX tags | Existing canary test still needed |
| P-027 LLM cost tracking | CLEAN |
| P-029 Truncation signaling | CLEAN |
| P-030 Log without trace_id | CLEAN |

## Findings Summary

| Category | Count | Fixed | Suppressed | Deferred |
|----------|-------|-------|------------|----------|
| SECURITY | 0 | 0 | 0 | 0 |
| BUG | 0 | 0 | 0 | 0 |
| DEAD CODE | 3 | 3 | 0 | 0 |
| STYLE | 75 (test only) | 0 | 0 | 75 |
| INTENTIONAL | 2 (test S107, S311) | 0 | 0 | 0 |

## Security Review

| Pattern | Occurrences | All Safe? | Details |
|---------|-------------|-----------|---------|
| subprocess | 1 (tools/shell.py) | Yes | `_safe_env()`, explicit args, timeout |
| eval/exec | 0 | Yes | `tool_exec` name match only |
| pickle | 0 | Yes | |
| os.system | 0 | Yes | |
| SQL f-strings | 2 (memory.py:388,400) | Yes | Parameterized `?` placeholders, suppressed S608 |
| Hardcoded secrets | 0 | Yes | |
| tempfile | 0 | Yes | |

## Fixes Applied

| # | File | Finding | Fix |
|---|------|---------|-----|
| 1 | session.py:539 | I001 unsorted imports | Reordered `_build_deterministic_summary, verify_compaction_summary` |
| 2 | tests/test_verification.py:3 | F401 unused `pytest` import | Removed |
| 3 | tests/test_verification.py:6 | F401 unused `VerificationResult` import | Removed |

## Deferred Items

75 STYLE findings in test code (SIM117 ×25, PTH123 ×16, E701 ×10, I001 ×9, E402 ×3, SIM105 ×3, B007 ×2, RET503 ×2, others). Cosmetic only, test-only, no behavior impact.

## Confidence

98% — production code fully clean, test STYLE cosmetic only.
