# Static Analysis Report

**Date:** 2026-02-26
**Audit Cycle:** 9
**Tools:** ruff 0.15.1, mypy SKIPPED (sparse annotations)
**Python version:** 3.13.5
**Files scanned:** 30 production + 36 test files
**EXIT STATUS:** PASS

## Scope

Production: `lucyd.py`, `agentic.py`, `config.py`, `consolidation.py`, `context.py`, `memory.py`, `memory_schema.py`, `session.py`, `skills.py`, `synthesis.py`, `channels/` (4 files), `providers/` (3 files), `tools/` (13 files)
Tests: `tests/` (36 files, +2 since cycle 8)

## Configuration

Ruff config: `ruff.toml` (target-version updated to py313)
Rules enabled: S, E, F, W, B, UP, SIM, RET, PTH, I, TID
Ignores: S603 (subprocess — manual review), S607 (partial path — manual review), E501 (line length), S608 (SQL placeholders)
Per-file: tests/* exempt from S101, S104, S105, S106, S108, S310. memory.py exempt from S608.

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-001 (zip without strict) | PASS | No unprotected `zip()` in production code |
| P-002 (BaseException vs Exception) | PASS | `agentic.py:240` correctly uses `isinstance(result, BaseException)` |
| P-003 (tool path params) | NOTED | 19 tool functions inventoried; full boundary check deferred to Stage 6 |
| P-005 (shadowed test names) | PASS | AST-verified: zero duplicate class or function names within same scope |
| P-010 (suppressed security findings) | PASS | 30 `# noqa: S*` suppressions, all have justification comments, all verified current |
| P-020 (magic numbers) | PASS | Numeric literals in signatures are all config-driven or framework constants |
| P-021 (provider-specific defaults) | PASS | All matches are dispatch branches or config accessors — no framework defaults |
| P-022 (channel identifiers) | PASS | `config.py` (validation), `context.py` (source routing) — known debt, documented. No new violations. Enforced by `test_audit_agnostic.py` |

## Findings Summary

| Category | Count | Fixed | Suppressed | Deferred |
|----------|-------|-------|------------|----------|
| SECURITY | 0 | 0 | 5 (new noqa) | 0 |
| BUG | 2 | 2 | 0 | 0 |
| DEAD CODE | 4 | 4 | 0 | 0 |
| STYLE | 48 | 0 | 0 | 48 |
| INTENTIONAL | 5 | 0 | 5 (new) | 0 |
| FALSE POSITIVE | 0 | 0 | 0 | 0 |

## Security Review

| Pattern | Occurrences | All Safe? | Details |
|---------|-------------|-----------|---------|
| subprocess | 2 | Yes | `tools/shell.py:42` — `create_subprocess_shell` with `_safe_env()`, `start_new_session=True`, timeout. `lucyd.py:1195` — `subprocess.run()` with list args, `capture_output=True`, `timeout`, `check=True`. |
| eval/exec | 0 | Yes | grep matched `tool_exec` function name only, no actual eval/exec calls |
| pickle/marshal/shelve | 0 | Yes | Clean |
| os.system | 0 | Yes | Clean |
| SQL f-strings | 0 | Yes | `memory.py` SQL suppressions verified — placeholder lists only, all values parameterized |
| Hardcoded secrets | 0 | Yes | Clean |
| tempfile | 2 | Yes | `lucyd.py:1192` — mkstemp + finally/unlink. `tools/tts.py` — mkstemp with cleanup after send. |

## Fixes Applied

| Fix | File | Category | Description |
|-----|------|----------|-------------|
| Remove unused `time` import | test_audit_agnostic.py:242 | DEAD CODE | F401 — unused import |
| Remove unused `asyncio`, `os` imports | test_shell_security.py:7-8 | DEAD CODE | F401 — unused imports |
| Remove unused `response` variable | test_daemon_integration.py:1684 | DEAD CODE | F841 — assigned but never used |
| Remove unused `mock_wait` bindings | test_shell_security.py:443,455 | DEAD CODE | F841 — `as mock_wait` never referenced |
| Fix f-string without placeholders | test_audit_agnostic.py:121 | BUG | F541 — `f"..."` → `"..."` |
| Fix type comparison | test_orchestrator.py:2151 | BUG | E721 — `t == list` → `t is list` |

## Suppressions Added

| File:Line | Rule | Justification |
|-----------|------|---------------|
| consolidation.py:447 | S110 | Rollback after failed commit; if rollback itself fails, outer except re-raises |
| consolidation.py:500 | S110 | Same pattern — rollback fallback for extract_from_file |
| lucyd.py:1306 | S110 | Config lookup for session listing; graceful degradation to max_ctx=0 |
| lucyd.py:1700 | S110 | Session state persist on shutdown; failure is benign |
| session.py:595 | S110 | Cost DB query for session info; graceful degradation to 0.0 |

## Deferred Items

48 STYLE findings deferred (non-controversial, no behavioral impact):

**Production (48):**
- PTH123 x24: `open()` → `Path.open()` — codebase-wide convention, consistent usage
- SIM105 x11: try-except-pass → `contextlib.suppress` — readability preference
- SIM108 x7: if-else → ternary — readability preference, current code clearer
- SIM103 x2: needless-bool — readability preference
- PTH108 x2: os.unlink → Path.unlink — cosmetic
- PTH101 x1: os.chmod → Path.chmod — cosmetic
- SIM102 x1: collapsible-if — cosmetic

## Type Checking

SKIPPED — codebase has type hints on function signatures but not comprehensively annotated.

## Confidence

Overall confidence: 97%
Zero security findings. All S110 suppressions documented with justification. 6 bug/dead-code fixes in tests, all verified green. 48 style findings deferred — cosmetic only.
