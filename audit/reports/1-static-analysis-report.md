# Static Analysis Report

**Date:** 2026-02-26
**Audit Cycle:** 10
**Tools:** ruff 0.15.1, mypy SKIPPED (sparse annotations)
**Python version:** 3.13.5
**Files scanned:** 31 production + 34 test files
**EXIT STATUS:** PASS

## Scope

Production: `lucyd.py`, `agentic.py`, `config.py`, `consolidation.py`, `context.py`, `evolution.py` (new), `memory.py`, `memory_schema.py`, `session.py`, `skills.py`, `synthesis.py`, `channels/` (4 files), `providers/` (3 files), `tools/` (12 files)
Tests: `tests/` (34 files)

## Configuration

Ruff config: `ruff.toml` (target-version py313)
Rules enabled: S, E, F, W, B, UP, SIM, RET, PTH, I, TID
Ignores: S603 (subprocess — manual review), S607 (partial path — manual review), E501 (line length), S608 (SQL placeholders)
Per-file: tests/* exempt from S101, S104, S105, S106, S108, S310. memory.py exempt from S608.

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-001 (zip without strict) | PASS | No unprotected `zip()` in production code |
| P-002 (BaseException vs Exception) | PASS | `agentic.py:240` correctly uses `isinstance(result, BaseException)` |
| P-003 (tool path params) | NOTED | 19 tool functions inventoried; full boundary check deferred to Stage 6 |
| P-005 (shadowed test names) | PASS | All "duplicates" are methods in different classes (same file, different namespace) |
| P-010 (suppressed security findings) | PASS | 30 `# noqa: S*` suppressions, all have justification comments, all verified current |
| P-020 (magic numbers) | PASS | Numeric literals in signatures are config-driven or framework constants |
| P-021 (provider-specific defaults) | PASS | All matches are dispatch branches or config accessors — no framework defaults |
| P-022 (channel identifiers) | PASS | `config.py` (validation), `context.py` (source routing) — known debt, documented. Enforced by `test_audit_agnostic.py` |

## Findings Summary

| Category | Count | Fixed | Suppressed | Deferred |
|----------|-------|-------|------------|----------|
| SECURITY | 0 | 0 | 0 | 0 |
| BUG | 1 | 1 | 0 | 0 |
| DEAD CODE | 1 | 1 | 0 | 0 |
| STYLE | 50 | 0 | 0 | 50 |
| INTENTIONAL | 0 | 0 | 0 | 0 |
| FALSE POSITIVE | 0 | 0 | 0 | 0 |

## Security Review

| Pattern | Occurrences | All Safe? | Details |
|---------|-------------|-----------|---------|
| subprocess | 2 | Yes | `tools/shell.py:42` — `create_subprocess_shell` with `_safe_env()`, `start_new_session=True`, timeout. `lucyd.py:1195` — `subprocess.run()` with list args, `capture_output=True`, `timeout`, `check=True`. |
| eval/exec | 0 | Yes | Clean |
| pickle/marshal/shelve | 0 | Yes | Clean |
| os.system | 0 | Yes | Clean |
| SQL f-strings | 0 | Yes | `memory.py` SQL suppressions verified — placeholder lists only, all values parameterized |
| Hardcoded secrets | 0 | Yes | Clean |
| tempfile | 2 | Yes | `lucyd.py:1192` — mkstemp + finally/unlink. `tools/tts.py:89` — mkstemp with cleanup after send. |

## Fixes Applied

| Fix | File | Category | Description |
|-----|------|----------|-------------|
| Remove unused `date` import | evolution.py:18 | DEAD CODE | F401 — `from datetime import date` never used |
| Rename unused loop variable | evolution.py:169 | BUG | B007 — `date_str` → `_date_str` (not used in loop body) |

## Suppressions Added

None this cycle. All existing suppressions (30 total) verified current with accurate justification comments.

## Deferred Items

50 STYLE findings deferred (non-controversial, no behavioral impact):

**Production (50):**
- PTH123 x24: `open()` → `Path.open()` — codebase-wide convention, consistent usage
- SIM105 x12: try-except-pass → `contextlib.suppress` — readability preference
- SIM108 x7: if-else → ternary — readability preference, current code clearer
- SIM103 x2: needless-bool — readability preference
- PTH108 x2: os.unlink → Path.unlink — cosmetic
- PTH101 x1: os.chmod → Path.chmod — cosmetic
- PTH105 x1: os.replace → Path.replace — cosmetic (in evolution.py atomic write)
- SIM102 x1: collapsible-if — cosmetic

## Type Checking

SKIPPED — codebase has type hints on function signatures but not comprehensively annotated.

## Confidence

Overall confidence: 98%
Zero security findings. Two fixes in evolution.py (new module from today), both verified green. 50 style findings deferred — cosmetic only. All 30 existing security suppressions verified with current justification comments.
