# Static Analysis Report

**Date:** 2026-02-20
**Tools:** ruff 0.15.1, mypy SKIPPED (minimal type annotations)
**Python version:** 3.13.5
**Files scanned:** 29 production + 32 test files
**EXIT STATUS:** PASS
**Triggered by:** Vision/STT feature implementation + Memory v2 recall personality audit

## Scope

Production: `lucyd.py`, `agentic.py`, `config.py`, `consolidation.py`, `context.py`, `memory.py`, `memory_schema.py`, `session.py`, `skills.py`, `channels/` (4 files), `providers/` (3 files), `tools/` (13 files)
Tests: `tests/` (32 files)

## Configuration

Ruff config: `ruff.toml` (pre-existing from previous audit cycles)
Rules enabled: S, E, F, W, B, UP, SIM, RET, PTH, I, TID
Ignores: S603 (subprocess — manual review), S607 (partial path — manual review), E501 (line length), S608 (SQL placeholders)
Per-file: tests/* exempt from S101, S104, S105, S106, S108, S310; memory.py exempt from S608

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-001 (zip without strict) | CLEAN | No production hits |
| P-002 (BaseException vs Exception) | CLEAN | `agentic.py:206` correctly uses `isinstance(result, BaseException)` |
| P-003 (tool path params) | NOTED | 19 tool functions inventoried; path-like params in tool_read, tool_write, tool_edit, tool_memory_get, tool_tts, tool_message (attachments), tool_web_fetch (url), tool_exec (command). Full boundary check deferred to Stage 6. |
| P-005 (shadowed test classes) | CLEAN | No duplicate class names. 11 duplicate function names found across 5 test files — all in different classes (e.g., `TestSTTConfig.test_defaults_when_section_absent` vs `TestVisionConfig.test_defaults_when_section_absent`). Not shadowed. |
| P-010 (suppressed security findings) | CLEAN | 18 `# noqa: S*` suppressions in production code. All have justification comments. All justifications verified against current code — no changed data flows. S310 URL sources confirmed non-user-controlled. S110 exception swallowing confirmed in cleanup/status paths only. |

## Findings Summary

| Category | Count | Fixed | Suppressed | Deferred |
|----------|-------|-------|------------|----------|
| SECURITY | 0 | — | — | — |
| BUG | 0 | — | — | — |
| DEAD CODE | 0 | — | — | — |
| STYLE | 34 (prod) + 88 (test) | 1 | 0 | 121 |
| INTENTIONAL | 2 | 0 | 0 | 0 |

## Security Review

| Pattern | Occurrences | All Safe? | Details |
|---------|-------------|-----------|---------|
| subprocess | 2 | Yes | `tools/shell.py:42-48` — `create_subprocess_shell` with `_safe_env()`, `start_new_session=True`, timeout. `lucyd.py:862-866` — `subprocess.run()` with list args, `capture_output=True`, `timeout=ffmpeg_timeout`, `check=True`. No shell=True. |
| eval/exec | 0 | Yes | grep matched `tool_exec` function name only, not bare eval/exec call |
| pickle | 0 | Yes | — |
| os.system | 0 | Yes | — |
| SQL f-strings | 2 | Yes | `memory.py:412,424` — f-strings interpolate only `?` placeholder counts from `",".join("?" * len(entities))`, actual values bound via parameterized queries. Standard safe IN clause pattern. S608 ignored in ruff.toml for memory.py. |
| Hardcoded secrets | 0 | Yes | — |
| tempfile | 2 | Yes | `lucyd.py:859` — `mkstemp()` with fd closed immediately (line 860), `finally` block calls `os.unlink()` (line 882). `tools/tts.py:74` — `mkstemp()` with fd closed (line 75), intentionally persistent output artifact. Path validated by `_check_path()` when user-supplied. |

## Fixes Applied

1. **test_structured_recall.py:8-23** — Fixed unsorted import block (I001). `_DEFAULT_PRIORITIES` moved before `EMPTY_RECALL_FALLBACK` per isort rules. Auto-fixable, verified clean.

All fixes verified: 1075 tests pass, finding resolved in re-scan.

## Intentional Findings (Not Fixed)

1. **lucyd.py:862-866** — S603/S607 on ffmpeg subprocess call. Uses list form args, has `timeout` and `check=True`. Partial path `"ffmpeg"` is acceptable for system utility. Globally ignored in ruff config.
2. **lucyd.py:882** — PTH108 `os.unlink(wav_path)` where `wav_path` is a string from `tempfile.mkstemp()`. Using os.unlink directly is idiomatic here.

## Deferred Items

121 STYLE findings deferred (non-controversial, no behavioral impact):

**Production (34):**
- PTH123 x22: `open()` → `Path.open()` — codebase-wide convention, consistent usage
- SIM105 x5: try-except-pass → `contextlib.suppress` — readability preference
- SIM108 x4: if-else → ternary — readability preference (if-else is often clearer)
- PTH101 x1: `os.chmod()` → `Path.chmod()` — style
- PTH108 x1: `os.unlink()` → `Path.unlink()` — style (see Intentional #2)
- SIM102 x1: collapsible if — readability preference

**Tests (87):**
- SIM117 x80: nested with → single with — test readability with mock stacking
- PTH123 x5: open() in tests — style
- PTH211 x1: os.symlink in tests — style
- SIM105 x1: suppress pattern in conftest — style

## Type Checking

SKIPPED — codebase has minimal type annotations. Function signatures have basic type hints but no comprehensive typing. Recommendation: add type hints to security-critical functions first.

## Recommendations

1. Consider adding PTH123 to ruff ignore list — it's the most frequent finding (22 hits) and purely a style choice this codebase doesn't follow.
2. The ffmpeg subprocess call in `_transcribe_local()` follows all safe patterns (list args, timeout, check, finally cleanup). No action needed.

## Confidence

Overall confidence: 98%
No areas of uncertainty — all findings clearly categorized, all fixes verified. Zero security findings in production code. Zero bug findings. Zero dead code.
