# Static Analysis Report

**Date:** 2026-02-23
**Audit Cycle:** 6
**Tools:** ruff 0.15.1, mypy SKIPPED (sparse annotations)
**Python version:** 3.13.5
**Files scanned:** 29 production + 34 test files
**EXIT STATUS:** PASS

## Scope

Production: `lucyd.py`, `agentic.py`, `config.py`, `consolidation.py`, `context.py`, `memory.py`, `memory_schema.py`, `session.py`, `skills.py`, `channels/` (4 files), `providers/` (3 files), `tools/` (13 files)
Tests: `tests/` (34 files)

## Configuration

Ruff config: `ruff.toml` (takes precedence over pyproject.toml)
Rules enabled: S, E, F, W, B, UP, SIM, RET, PTH, I, TID
Ignores: S603 (subprocess — manual review), S607 (partial path — manual review), E501 (line length), S608 (SQL placeholders)
Per-file: tests/* exempt from S101, S104, S105, S106, S108, S310. memory.py exempt from S608.

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-001 (zip without strict) | PASS | No unprotected `zip()` in production code |
| P-002 (BaseException vs Exception) | PASS | `agentic.py:223` correctly uses `isinstance(result, BaseException)` |
| P-003 (tool path params) | NOTED | 19 tool functions inventoried; full boundary check deferred to Stage 6 |
| P-005 (shadowed test names) | PASS | No duplicate class names across 32 test files |
| P-010 (suppressed security findings) | PASS | All `# noqa: S*` suppressions have justification comments, all verified current |
| P-014 (error at boundaries) | PASS | All 4 `provider.complete()` sites wrapped in try/except. consolidation.py:208-212, consolidation.py:336-340, session.py:485-490, agentic.py:154 (retry loop). |
| P-015 (impl parity) | PASS | Both providers now have safe JSON parsing (`_safe_parse_args` in Anthropic, existing pattern in OpenAI) |
| P-016 (resource lifecycle) | PASS | All sqlite3 connections have `finally: conn.close()`. Telegram httpx client has `disconnect()`. lucyd.py httpx uses `async with`. |
| P-018 (unbounded collections) | 2 LOW | `telegram._last_message_ids` (per-chat, bounded by allow_from). `session._sessions/_index` (per-sender, bounded by deployment). See details below. |

### P-018 Findings (Low Severity)

1. **telegram.py:75** — `self._last_message_ids: dict[int, int]` grows per unique chat_id, no eviction. Bounded in practice by `allow_from` config filter. One entry per chat, not per message.

2. **session.py:261-262** — `self._index` and `self._sessions` grow per unique sender. Bounded in practice by deployment model (single operator + handful of system senders). `close_session()` removes entries.

Both structurally unbounded but operationally constrained. Accepted for current deployment model.

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
| subprocess | 2 | Yes | `tools/shell.py:42` — `create_subprocess_shell` with `_safe_env()`, `start_new_session=True`, timeout. `lucyd.py:1084` — `subprocess.run()` with list args, `capture_output=True`, `timeout`, `check=True`. |
| eval/exec | 0 | Yes | grep matched `tool_exec` function name only, no actual eval/exec calls |
| pickle/marshal/shelve | 0 | Yes | Clean |
| os.system | 0 | Yes | Clean |
| SQL f-strings | 0 | Yes | All SQL uses parameterized queries |
| Hardcoded secrets | 0 | Yes | Clean |
| tempfile | 2 | Yes | Both with cleanup (lucyd.py mkstemp + finally/unlink, tts.py mkstemp for channel send) |

## Fixes Applied

None. No new security, bug, or dead code findings in this cycle.

## Suppressions Added

None. All existing suppressions verified as current.

## Deferred Items

40 STYLE findings deferred (non-controversial, no behavioral impact):

**Production (40):**
- PTH123 x22: `open()` → `Path.open()` — codebase-wide convention, consistent usage
- SIM105 x7: try-except-pass → `contextlib.suppress` — readability preference
- SIM108 x6: if-else → ternary — readability preference, current code clearer
- SIM103 x2: needless-bool — readability in `_is_transient_error`, current code clearer
- PTH101 x1, PTH108 x1: os.chmod/unlink → Path methods — cosmetic
- SIM102 x1: collapsible-if — cosmetic

**Intentional (2, pre-existing suppressions):**
- S603/S607: lucyd.py:1084 ffmpeg subprocess — explicit arg list, timeout, capture_output
- S311: tests/test_orchestrator.py:1872 — `random.Random(42)` for deterministic pixel generation, not cryptographic

## Type Checking

SKIPPED — codebase has type hints on function signatures but not comprehensively annotated.

## Confidence

Overall confidence: 96%
New pattern checks P-014 through P-018 all clean (two low-severity accepted findings). Zero security findings. Zero bug findings.
