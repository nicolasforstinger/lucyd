# Static Analysis Report

**Date:** 2026-02-24
**Audit Cycle:** 7 (post-synthesis feature)
**Tools:** ruff 0.15.1, mypy SKIPPED (sparse annotations)
**Python version:** 3.13.5
**Files scanned:** 30 production + 34 test files
**EXIT STATUS:** PASS

## Scope

Production: `lucyd.py`, `agentic.py`, `config.py`, `consolidation.py`, `context.py`, `memory.py`, `memory_schema.py`, `session.py`, `skills.py`, `synthesis.py` (NEW), `channels/` (4 files), `providers/` (3 files), `tools/` (13 files)
Tests: `tests/` (34 files including `test_synthesis.py` NEW)

## Configuration

Ruff config: `ruff.toml` (takes precedence over pyproject.toml)
Rules enabled: S, E, F, W, B, UP, SIM, RET, PTH, I, TID
Ignores: S603 (subprocess — manual review), S607 (partial path — manual review), E501 (line length), S608 (SQL placeholders)
Per-file: tests/* exempt from S101, S104, S105, S106, S108, S310. memory.py exempt from S608.

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-001 (zip without strict) | PASS | No unprotected `zip()` in production code |
| P-002 (BaseException vs Exception) | PASS | `agentic.py:232` correctly uses `isinstance(result, BaseException)` |
| P-003 (tool path params) | NOTED | 19 tool functions inventoried; full boundary check deferred to Stage 6 |
| P-005 (shadowed test names) | PASS | All "duplicate" function names are in different classes (e.g. `test_blocked_path` in TestRead, TestWrite, TestEdit) — no actual shadowing |
| P-010 (suppressed security findings) | PASS | All `# noqa: S*` suppressions have justification comments, all verified current |
| P-014 (error at boundaries) | PASS | All `provider.complete()` sites wrapped in try/except. synthesis.py:118 wrapped in try/except with raw recall fallback. |
| P-015 (impl parity) | PASS | Both providers have safe JSON parsing |
| P-016 (resource lifecycle) | PASS | All sqlite3 connections have `finally: conn.close()`. synthesis.py has no resource ownership. |
| P-018 (unbounded collections) | 2 LOW | Same as prior cycle — telegram._last_message_ids, session._sessions/_index. Operationally constrained. |

## New Module: synthesis.py

`synthesis.py` (144 lines) scanned with zero findings. No security anti-patterns, no dead code, no bug patterns. Provider calls wrapped in try/except with fallback to raw recall. No subprocess, no eval, no SQL, no tempfile, no pickle.

## Findings Summary

| Category | Count | Fixed | Suppressed | Deferred |
|----------|-------|-------|------------|----------|
| SECURITY | 0 | 0 | 0 | 0 |
| BUG | 0 | 0 | 0 | 0 |
| DEAD CODE | 0 | 0 | 0 | 0 |
| STYLE | 40 | 0 | 0 | 40 |
| INTENTIONAL | 2 | 0 | 2 (pre-existing) | 0 |
| FALSE POSITIVE | 1 | 0 | 0 | 0 |

## Security Review

| Pattern | Occurrences | All Safe? | Details |
|---------|-------------|-----------|---------|
| subprocess | 2 | Yes | `tools/shell.py:42` — `create_subprocess_shell` with `_safe_env()`, `start_new_session=True`, timeout. `lucyd.py:1128` — `subprocess.run()` with list args, `capture_output=True`, `timeout`, `check=True`. |
| eval/exec | 0 | Yes | grep matched `tool_exec` function name only, no actual eval/exec calls |
| pickle/marshal/shelve | 0 | Yes | Clean |
| os.system | 0 | Yes | Clean |
| SQL f-strings | 2 | Yes | `memory.py:388,400` — f-strings build `?,?,?` placeholder lists only, all values parameterized. Verified. |
| Hardcoded secrets | 0 | Yes | Clean |
| tempfile | 2 | Yes | Both with cleanup (lucyd.py mkstemp + finally/unlink, tts.py mkstemp for channel send) |

## Fixes Applied

None. Zero new security, bug, or dead code findings.

## Suppressions Added

None. All existing suppressions verified as current.

## Deferred Items

40 STYLE findings deferred (non-controversial, no behavioral impact):

**Production (40):**
- PTH123 x22: `open()` → `Path.open()` — codebase-wide convention, consistent usage
- SIM105 x7: try-except-pass → `contextlib.suppress` — readability preference
- SIM108 x6: if-else → ternary — readability preference, current code clearer
- SIM103 x2: needless-bool — readability preference
- PTH101 x1, PTH108 x1: os.chmod/unlink → Path methods — cosmetic
- SIM102 x1: collapsible-if — cosmetic

**Intentional (2, pre-existing suppressions):**
- S603/S607: lucyd.py:1128 ffmpeg subprocess — explicit arg list, timeout, capture_output
- S311: tests/test_orchestrator.py:1902 — `random.Random(42)` for deterministic pixel generation, not cryptographic

## Type Checking

SKIPPED — codebase has type hints on function signatures but not comprehensively annotated.

## Confidence

Overall confidence: 97%
synthesis.py introduced zero findings. All pattern checks clean. Zero security findings. Zero bug findings.
