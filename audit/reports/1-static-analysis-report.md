# Static Analysis Report

**Date:** 2026-02-19
**Tools:** ruff 0.15.1, mypy SKIPPED (minimal type annotations)
**Python version:** 3.13.5
**Files scanned:** 29 production + 3 bin scripts + 32 test files
**EXIT STATUS:** PASS

## Scope

Production: `lucyd.py`, `agentic.py`, `config.py`, `consolidation.py`, `context.py`, `memory.py`, `memory_schema.py`, `session.py`, `skills.py`, `channels/` (4 files), `providers/` (3 files), `tools/` (13 files), `bin/` (3 scripts)
Tests: `tests/` (32 files)

## Configuration

Ruff config: `ruff.toml` (pre-existing from previous audit cycles)
Rules enabled: S, E, F, W, B, UP, SIM, RET, PTH, I, TID
Ignores: S603 (subprocess — manual review), S607 (partial path — manual review), E501 (line length), S608 (SQL placeholders)
Per-file: tests/* exempt from S101, S104, S105, S106, S108, S310; memory.py exempt from S608

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-001 (zip without strict) | CLEAN | No production hits (only mutants/) |
| P-002 (BaseException vs Exception) | CLEAN | `agentic.py:206` correctly uses `isinstance(result, BaseException)` |
| P-003 (tool path params) | NOTED | 19 tool functions inventoried; path-like params in tool_read, tool_write, tool_edit, tool_memory_get, tool_tts, tool_message (attachments), tool_web_fetch (url), tool_exec (command). Full boundary check deferred to Stage 6. |
| P-005 (shadowed test classes) | CLEAN | No duplicate class names. 4 duplicate function names found across different classes (test_blocked_path in TestRead/TestWrite/TestEdit; test_empty_input in TestChunkFile/TestEmbedBatch; test_user_message and test_assistant_with_tool_calls in TestAnthropicFormatMessages/TestOpenAIFormatMessages) — all in different classes, not shadowed. |
| P-010 (suppressed security findings) | CLEAN | 18 `# noqa: S*` suppressions reviewed. All have justification comments. All justifications verified against current code: S108 (/tmp paths — config defaults), S110 (benign cleanup/status), S310 (hardcoded or validated URLs), S311 (timing jitter). No stale justifications found. |

## Findings Summary

| Category | Count | Fixed | Suppressed | Deferred |
|----------|-------|-------|------------|----------|
| SECURITY | 0 | — | — | — |
| BUG | 0 | — | — | — |
| DEAD CODE | 3 | 3 | 0 | 0 |
| STYLE | 33 (prod) | 2 | 0 | 31 |

## Security Review

| Pattern | Occurrences | All Safe? | Details |
|---------|-------------|-----------|---------|
| subprocess | 2 | Yes | `tools/shell.py:44-45` — `asyncio.subprocess.PIPE` with `_safe_env()`, `start_new_session=True`, timeout |
| eval/exec | 0 | Yes | grep matched `tool_exec` function name, not eval/exec call |
| pickle | 0 | Yes | — |
| os.system | 0 | Yes | — |
| SQL f-strings | 0 | Yes | All SQL uses parameterized queries |
| Hardcoded secrets | 0 | Yes | — |
| tempfile | 1 | Yes | `tools/tts.py:74` — `mkstemp()` with `os.close(fd)` immediately; dir is configured `_output_dir` |

## Fixes Applied

1. **test_consolidation.py** — Removed unused import `Path` (F401)
2. **test_consolidation.py** — Removed unused import `FACT_EXTRACTION_PROMPT` (F401)
3. **test_consolidation.py:540** — Removed unused variable `count` assignment (F841)
4. **consolidation.py:264** — Removed superfluous `else` after `continue` (RET507), fixed indentation
5. **lucyd.py:394** — Added blank line between stdlib and local imports (I001)

All fixes verified: 1020 tests pass, finding resolved in re-scan.

## Suppressions Added

None — all existing suppressions verified, no new ones needed.

## Deferred Items

31 STYLE findings deferred (non-controversial, no behavioral impact):
- PTH123 x21: `open()` → `Path.open()` — codebase-wide refactoring preference
- SIM105 x4: try-except-pass → `contextlib.suppress` — readability preference
- SIM108 x4: if-else → ternary — readability preference
- PTH101 x1: `os.chmod()` → `Path.chmod()` — style
- SIM102 x1: collapsible if — readability preference

Test code has ~30 SIM117 (nested with → single with) also deferred — test readability, no functional impact.

## Type Checking

SKIPPED — codebase has minimal type annotations. Function signatures have basic type hints but no comprehensive typing. Recommendation: add type hints to security-critical functions first (filesystem, shell, web tools).

## Recommendations

1. Consider adding PTH123 to ruff ignore list if the project prefers `open()` over `Path.open()` — it's the most frequent finding and entirely a style choice.
2. Type hints on security-critical tool functions would enable mypy to catch boundary mismatches.

## Confidence

Overall confidence: 98%
No areas of uncertainty — all findings clearly categorized, all fixes verified.
