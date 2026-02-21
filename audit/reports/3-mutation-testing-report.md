# Mutation Testing Audit Report

**Date:** 2026-02-21
**Audit Cycle:** 4
**Tool:** mutmut 3.4.0
**Python:** 3.14.3
**EXIT STATUS:** PARTIAL

## Scope

**Target:** `tools/` directory (13 modules, 1905 total mutants)
**Test files:** 13 scoped test files (per Rule 9)
**Excluded:** `lucyd.py` (orchestrator — Rule 13, handled by Stage 4), `channels/`, `providers/`, root-level modules (covered by prior reports)

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-004 (iteration order) | CLEAN | Shell security env var tests use `monkeypatch.setenv` with both matching and non-matching entries. Non-matching entries appear after matching in insertion order — `continue→break` mutations caught. |
| P-013 (None-defaulted deps) | CLEAN | Agents.py deny-list `tools is not None` guard has both branches tested (14/14 killed). All `_config is None` guards tested via error-when-not-initialized tests. |

## Configuration

```toml
[tool.mutmut]
paths_to_mutate = ["tools/"]
tests_dir = [13 scoped test files]
pythonpath = ["."]
```

## Results Summary

| Metric | Cycle 3 | Cycle 4 | Delta |
|--------|---------|---------|-------|
| Total mutants | 1818 | 1905 | +87 |
| Killed | 1018 | 1054 | +36 |
| Timeout | 9 | 9 | 0 |
| Survived | 679 | 677 | -2 |
| No tests | 112 | 165 | +53 |
| Kill rate | 56.0% | 55.3% | -0.7% |
| Effective rate | 59.7% | 61.1% | +1.4% |

Mutant count increased by 87 — new code in tools modules since Cycle 3. Effective kill rate improved slightly.

## Security Verification

### Security Function Kill Rates

| Function | Module | Total | Killed | Survived | Rate | Status |
|----------|--------|-------|--------|----------|------|--------|
| `_safe_env` | shell.py | 8 | 8 | 0 | **100%** | VERIFIED |
| `_check_path` | filesystem.py | 10 | 10 | 0 | **100%** | VERIFIED |
| deny-list filtering | agents.py | 14 | 14 | 0 | **100%** | VERIFIED |
| `_is_private_ip` | web.py | 11 | 9 | 2 | **81.8%** | VERIFIED (2 equivalent) |
| `_validate_url` | web.py | 22 | 19 | 3 | **86.4%** | VERIFIED (3 cosmetic) |
| `_SafeRedirectHandler` | web.py | ~20 | 16 | 4 | **80.0%** | VERIFIED (4 equivalent) |

### Security Survivor Analysis

**`_is_private_ip` — 2 survivors (EQUIVALENT):**
Same as Cycle 3. `or`→`and` mutations on boolean chain. `ip.is_private` is superset of later terms on Python 3.14.

**`_validate_url` — 3 survivors (COSMETIC):**
Same as Cycle 3. Error string mutations — function still returns truthy error, URL still blocked.

**`_SafeRedirectHandler.redirect_request` — 4 survivors (EQUIVALENT):**
- mutmut_6: `fp`→`None` in `super().redirect_request()` — passthrough param unused in success path
- mutmut_8: `msg`→`None` — same pattern
- mutmut_9: `headers`→`None` — same pattern
- mutmut_17: `and`→`or` on IP-pinning line — either behavior unchanged (new_req not None) or crashes fail-safe (setattr on None). Security check is on prior line.

### Security Verdict

All security-critical mutations (deny-list bypass, path traversal bypass, SSRF bypass, env filtering bypass) are killed. Survivors in security functions are all equivalent or cosmetic. No test gaps in security enforcement.

## Known Gaps (Carried Forward)

| Gap | Severity | Module | Notes |
|-----|----------|--------|-------|
| `_HTMLToText` | Low | web.py | HTML→text conversion. Not security-critical. |
| `tool_web_search` / `tool_web_fetch` | Low | web.py | Functions make real HTTP calls. Security boundary tested. |
| `tool_tts` | Low | tts.py | External ffmpeg subprocess. |
| `tool_exec` body | Medium | shell.py | Process timeout interactions. `_safe_env` verified. |
| `indexer.py` overall | Low | indexer.py | Complex file operations, no security functions. |

## Equivalent Mutants Documented

| Module | Count | Description |
|--------|-------|-------------|
| web.py (`_is_private_ip`) | 2 | `or`→`and` on boolean chain — `is_private` superset on Py 3.14 |
| web.py (`_SafeRedirectHandler`) | 4 | Passthrough params unused in success path + fail-safe crash |
| **Total** | **6** | |

## Cosmetic Mutants Documented

| Module | Count | Description |
|--------|-------|-------------|
| web.py (`_validate_url`) | 3 | Error message string mutations — still returns truthy error |
| **Total** | **3** | |

## Fixes Applied

None during this stage. No security gaps found requiring new tests.

## Confidence

Overall confidence: 94%

- **Security functions: HIGH (98%).** All security-critical mutations killed. All survivors proven equivalent or cosmetic with justification. Same results as Cycle 3.
- **Non-security modules: MEDIUM (85%).** Kill rates below target in several modules, but survivors categorized and no behavioral bugs found.
- **Limitation:** `_HTMLToText` and HTTP-calling functions have low coverage. Functional, not security-critical.
