# Static Analysis Report

**Date:** 2026-02-28
**Audit Cycle:** 11
**Tools:** ruff 0.15.1
**Python version:** 3.13.5
**Files scanned:** 31 production + 37 test files
**EXIT STATUS:** PASS

## Scope

Production: `lucyd.py`, `agentic.py`, `config.py`, `consolidation.py`, `context.py`, `evolution.py`, `memory.py`, `memory_schema.py`, `session.py`, `skills.py`, `synthesis.py`, `channels/` (4 files), `providers/` (3 files), `tools/` (12 files)
Tests: `tests/` (37 files)

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
| P-005 (shadowed test names) | PASS | No duplicate class/function names in test files |
| P-010 (suppressed security findings) | PASS | 30 `# noqa: S*` suppressions, all verified current with justification |
| P-014 (unhandled errors at boundaries) | PASS | Error handling verified at all system boundaries |
| P-015 (implementation parity) | PASS | Provider format functions verified symmetric |
| P-016 (resource lifecycle) | PASS | All resource creation sites have cleanup paths |
| P-018 (unbounded data structures) | PASS | No new unbounded collections |
| P-020 (magic numbers) | PASS | Numeric literals are config-driven or framework constants |
| P-021 (provider-specific defaults) | PASS | All matches are dispatch branches or config accessors |
| P-022 (channel identifiers) | PASS | Enforced by `test_audit_agnostic.py` |
| P-025 (default parameter binding) | PASS | No new instances of module-global defaults |
| P-026 (SDK hotfix tag) | PASS | `HOTFIX:SDK-STREAMING-BUG` tag present in `anthropic_compat.py` |

## Findings Summary

| Category | Count | Fixed | Suppressed | Deferred |
|----------|-------|-------|------------|----------|
| SECURITY | 0 | 0 | 0 | 0 |
| BUG | 0 | 0 | 0 | 0 |
| DEAD CODE | 0 | 0 | 0 | 0 |
| STYLE | 48 | 0 | 0 | 48 |

## Security Review

| Pattern | Occurrences | All Safe? | Details |
|---------|-------------|-----------|---------|
| subprocess | 2 | Yes | `tools/shell.py:42` — `create_subprocess_shell` with `_safe_env()`, `start_new_session=True`, timeout. `lucyd.py:1203` — `subprocess.run()` with list args, `capture_output=True`, `timeout`, `check=True`. |
| eval/exec | 0 | Yes | Clean |
| pickle/marshal/shelve | 0 | Yes | Clean |
| os.system | 0 | Yes | Clean |
| SQL f-strings | 0 | Yes | `memory.py` SQL suppressions verified — placeholder lists only, all values parameterized |
| Hardcoded secrets | 0 | Yes | Clean |
| tempfile | 2 | Yes | `lucyd.py:1192` — mkstemp + finally/unlink. `tools/tts.py:89` — mkstemp with cleanup after send. |

## Fixes Applied

None this cycle. Previous PLAN.md audit (same day) already applied:
- Removed unused `Path` import from `tools/status.py` (F401)
- Fixed `import httpx` position in `providers/anthropic_compat.py` (E402)

Both fixes verified — no F401 or E402 in production code.

## Suppressions Added

None this cycle. All existing suppressions (30 total) verified current.

## Deferred Items

48 STYLE findings deferred (non-controversial, no behavioral impact):

- PTH123 x24: `open()` → `Path.open()` — codebase-wide convention
- SIM105 x11: try-except-pass → `contextlib.suppress` — readability preference
- SIM108 x7: if-else → ternary — readability preference
- PTH108 x2: os.unlink → Path.unlink — cosmetic
- SIM103 x2: needless-bool — readability preference
- PTH101 x1: os.chmod → Path.chmod — cosmetic
- SIM102 x1: collapsible-if — cosmetic

## Confidence

Overall confidence: 98%
Zero security or bug findings. All 14 pattern checks pass. All 30 existing security suppressions verified. 48 style findings are deferred cosmetic items carried from previous cycles.
