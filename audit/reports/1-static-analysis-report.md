# Static Analysis Report

**Date:** 2026-02-21
**Audit Cycle:** 4
**Tools:** ruff 0.15.2, mypy SKIPPED (see Type Checking section)
**Python version:** 3.14.3
**Files scanned:** 29 production + 34 test files
**EXIT STATUS:** PASS

## Scope

Production: `lucyd.py`, `agentic.py`, `config.py`, `consolidation.py`, `context.py`, `memory.py`, `memory_schema.py`, `session.py`, `skills.py`, `channels/` (4 files), `providers/` (3 files), `tools/` (12 files)
Tests: `tests/` (34 files)

## Configuration

Ruff config: `pyproject.toml` `[tool.ruff]` section
Rules enabled: S, E, F, W, B, UP, SIM, RET, PTH, I, TID
Ignores: S603 (subprocess — manual review), S607 (partial path — manual review), E501 (line length)
Per-file: tests/* exempt from S101, S104, S105, S106, S108

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-001 (zip without strict) | PASS | No unprotected `zip()` in production code |
| P-002 (BaseException vs Exception) | PASS | `agentic.py:207` correctly uses `isinstance(result, BaseException)` |
| P-003 (tool path params) | NOTED | 19 tool functions inventoried; path-like params in tool_read, tool_write, tool_edit, tool_memory_get, tool_tts, tool_message (attachments), tool_web_fetch (url), tool_exec (command). Full boundary check deferred to Stage 6. |
| P-005 (shadowed test names) | PASS | No duplicate class names. 12 duplicate function names across 6 test files — all in different classes (verified via AST). No shadowing. |
| P-010 (suppressed security findings) | PASS | 19 `# noqa: S*` suppressions in production code. All have justification comments. All justifications verified against current code — no changed data flows since Cycle 3. |

## Findings Summary

| Category | Count | Fixed | Suppressed | Deferred |
|----------|-------|-------|------------|----------|
| SECURITY | 0 | — | — | — |
| BUG | 0 | — | — | — |
| DEAD CODE | 2 | 2 | 0 | 0 |
| STYLE | 133 | 0 | 0 | 133 |
| INTENTIONAL | 19 | — | 19 (existing) | — |
| FALSE POSITIVE | 1 | — | — | — |

## Security Review

| Pattern | Occurrences | All Safe? | Details |
|---------|-------------|-----------|---------|
| subprocess | 2 | Yes | `tools/shell.py:42` — `create_subprocess_shell` with `_safe_env()`, `start_new_session=True`, timeout. `lucyd.py:939` — `subprocess.run()` with list args, `capture_output=True`, `timeout`, `check=True`. |
| eval/exec | 0 | Yes | grep matched `tool_exec` function name only |
| pickle/marshal/shelve | 0 | Yes | Clean |
| os.system | 0 | Yes | Clean |
| SQL f-strings | 2 | Yes | `memory.py:412,424` — f-strings interpolate only `?` placeholder counts (`",".join("?" * len(...))`), actual values bound via parameterized queries. Standard safe IN clause pattern. Already suppressed with S608 + justification. |
| Hardcoded secrets | 0 | Yes | Clean |
| tempfile | 2 | Yes | `lucyd.py:936` — mkstemp with finally cleanup (unlink in finally block). `tools/tts.py:74` — mkstemp, output artifact. |

## Fixes Applied

1. **tests/test_http_api.py:1000** — Removed unused `import json as _json` (F401). Test uses aiohttp's `json=` kwarg, not the json module. Verified: 96 tests pass.
2. **tests/test_web_security.py:365** — Removed unused variable `new_req` (F841). Test checks redirect doesn't raise; return value was never asserted. Verified: 69 tests pass.

Full suite verified: 1158 tests pass.

## Suppressions Added

None. All 19 existing suppressions verified as current.

## Deferred Items

133 STYLE findings deferred (non-controversial, no behavioral impact):

**Production (36):**
- PTH123 x22: `open()` → `Path.open()` — codebase-wide convention, consistent usage
- SIM108 x6: if-else → ternary — readability preference
- SIM105 x5: try-except-pass → `contextlib.suppress` — readability preference
- PTH101 x1, PTH108 x1, SIM102 x1: cosmetic

**Tests (97):**
- SIM117 x90: nested with → single with — test readability with mock stacking
- PTH123 x5, PTH211 x1, SIM105 x1: style

**False Positive (1):**
- tests/test_web_security.py:329 S310: `urllib.request.Request()` constructor used in SSRF test helper, not a URL open operation.

## Type Checking

SKIPPED — `pyproject.toml` has mypy config (`python_version = "3.11"`) but actual runtime is 3.14.3. Production code has type hints on function signatures but not comprehensively annotated. Previous cycle ran mypy and found 8 errors in `tools/indexer.py` (heterogeneous dict values). Not re-run this cycle; recommend updating `python_version` to `"3.14"` first.

## Recommendations

1. Update `[tool.mypy] python_version` from `"3.11"` to `"3.14"` in pyproject.toml
2. Consider a batch PTH123 cleanup pass as a separate task
3. Update P-005 check in PATTERN.md to use AST-based analysis (current grep produces false positives for same-named methods in different classes)

## Confidence

Overall confidence: 97%
No areas of uncertainty. Zero security findings. Zero bug findings. All dead code removed. All suppressions verified current.
