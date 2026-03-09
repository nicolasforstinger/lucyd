# Remediation Report

**Date:** 2026-03-09
**Audit Cycle:** 17
**EXIT STATUS:** PASS

## Carried Gaps from Cycle 16

No gaps carried forward. All previous gaps resolved or permanently accepted in Cycle 16.

## Permanently Accepted (Carried)

| Gap | Justification |
|-----|---------------|
| Provider `complete()` mock-boundary | Cannot test without live API credentials + cost. Canary test validates known SDK behavior each run. |
| Alias accumulation multi-session | `INSERT OR IGNORE` + unique constraint prevents duplicate accumulation by construction. |
| `_message_loop` debounce/FIFO | Orchestrator code (Rule 13 prohibits mutmut). 15+ behavioral contract tests cover all observable side effects. |

All three re-verified against source — justifications still hold.

## Findings Resolved This Cycle

All findings from Stages 1–7 were fixed inline during the audit. No deferred items reached Stage 8.

### Stage 1 Fixes
| # | Finding | Fix |
|---|---------|-----|
| 1 | `tests/conftest.py` I001 import ordering | Auto-corrected via `ruff --fix` |
| 2 | `tests/conftest.py` — `os._exit()` kills mutmut subprocess | Added `MUTMUT_RUNNING` env var check to skip `os._exit()` during mutation testing |

### Stage 7 Fixes (Documentation)
| # | Finding | Fix |
|---|---------|-----|
| 1 | `operations.md` — missing `--status` and `--log` flags | Added to flag table |
| 2 | `operations.md` — stale `tier` field in `/chat` table | Replaced with `attachments` |
| 3 | `operations.md` — stale model override in evolve description | Removed `"model": "primary"` reference |
| 4 | `operations.md` — `/compact` missing from rate limit group | Added to Standard group |
| 5 | `CLAUDE.md` — HTTP route inline list missing `/evolve`, `/compact` | Added |
| 6 | `CLAUDE.md` — source module count 35 → 33, lines ~10,434 → ~10,233 | Corrected |
| 7 | `CLAUDE.md` — test-to-source ratio ~2.5:1 → ~2.6:1 | Updated |
| 8 | `docs/diagrams.md` — 17 drifted line number references | All updated to match source |

## New Gaps This Cycle

None. V-1 (HTTP `/compact` queue bypass) was fixed during remediation.

### V-1 Fix Applied

**Issue:** HTTP `POST /api/v1/compact` called `_handle_compact()` → `_process_message()` directly, bypassing the message queue. Could race with concurrent message processing.

**Fix:** Changed `_handle_compact` in `http_api.py` to enqueue a `{"type": "compact", "response_future": future}` item through the message queue and await the future — same pattern as FIFO compact and HTTP reset. Removed the `handle_compact` callback parameter (no longer needed). Updated 4 tests to match queue-routing pattern.

## Batch Fixes

- **Cosmetic debt:** None carried. Style findings (SIM105, E701) are permanently suppressed with justification.
- **Dependency updates:** No security-critical updates. All outdated packages are minor/patch bumps.
- **Missing tests:** None identified. All mutation survivors are cosmetic/equivalent.

## Final Test Run

```
1725 passed in 33.16s
```

All tests passing after all fixes applied.

## Confidence

98% — all findings resolved, no open gaps, zero debt.
