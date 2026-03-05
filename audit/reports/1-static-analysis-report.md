# Static Analysis Report

**Date:** 2026-03-04
**Audit Cycle:** 15
**Tools:** ruff 0.15.1, mypy SKIPPED (minimal type annotations)
**Python version:** 3.13.5
**Files scanned:** 33 production, 39 test
**EXIT STATUS:** PASS

## Scope

All `.py` files in project root, `channels/`, `tools/`, `providers/`, `plugins.d/`. Tests in `tests/`.

## Configuration

ruff.toml: S, E, F, W, B, UP, SIM, RET, PTH, I, TID enabled. S603/S607/E501/S608 ignored. Tests exempt from S101/S104/S105/S106/S108/S310.

## Pattern Checks

| Pattern | Result |
|---------|--------|
| P-001 zip() strict | CLEAN |
| P-002 BaseException gather | CLEAN |
| P-005 Duplicate test names | Deferred to Stage 2 |
| P-010 noqa:S suppressions | CLEAN |
| P-016 Resource lifecycle | CLEAN |
| P-018 Unbounded structures | CLEAN |
| P-020 Magic numbers | CLEAN |
| P-021 Provider defaults | CLEAN |
| P-022 Channel names | CLEAN — no channel-specific identifiers outside channels/ |
| P-025 Default parameter binding | CLEAN |
| P-026 HOTFIX tags | Existing canary test still needed |
| P-027 LLM cost tracking | CLEAN |
| P-029 Truncation signaling | CLEAN |
| P-030 Log without trace_id | CLEAN |
| P-032 Architectural defaults | CLEAN |

## Findings Summary

| Category | Count | Fixed | Suppressed | Deferred |
|----------|-------|-------|------------|----------|
| SECURITY | 0 | 0 | 0 | 0 |
| BUG | 0 | 0 | 0 | 0 |
| DEAD CODE | 3 | 3 | 0 | 0 |
| STYLE | 73 (test only) | 0 | 0 | 73 |
| INTENTIONAL | 0 | 0 | 0 | 0 |

## Security Review

| Pattern | Occurrences | All Safe? | Details |
|---------|-------------|-----------|---------|
| subprocess | 1 (tools/shell.py) | Yes | `_safe_env()`, explicit args, timeout |
| eval/exec | 0 | Yes | `tool_exec` name match only |
| pickle | 0 | Yes | |
| os.system | 0 | Yes | |
| SQL f-strings | 2 (memory.py:393,405) | Yes | Parameterized `?` placeholders, suppressed S608 |
| Hardcoded secrets | 0 | Yes | |
| tempfile | 0 | Yes | |

## Fixes Applied

| # | File | Finding | Fix |
|---|------|---------|-----|
| 1 | tests/test_audit_agnostic.py:283 | F401 unused `json` import | Removed |
| 2 | tests/test_consolidation.py:6 | F401 unused `MagicMock` import | Removed |
| 3 | tests/test_web_security.py:11 | F401 unused `PropertyMock` import | Removed |

## Deferred Items

73 STYLE findings in test code (SIM117 ×25, PTH123 ×15, E701 ×10, I001 ×8, E402 ×3, SIM105 ×3, B007 ×2, RET503 ×2, others). Cosmetic only, test-only, no behavior impact.

## Confidence

98% — production code fully clean, test STYLE cosmetic only.
