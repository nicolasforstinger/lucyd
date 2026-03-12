# Remediation Report

**Date:** 2026-03-12
**Audit Cycle:** 18
**EXIT STATUS:** PASS

## Carried Gaps from Cycle 17

No gaps carried forward. All previous gaps resolved or permanently accepted.

## Permanently Accepted (Carried)

| Gap | Justification | Re-verified? |
|-----|---------------|--------------|
| Provider `complete()` mock-boundary | Cannot test without live API credentials + cost. Canary test validates known SDK behavior each run. | Yes — canary still present |
| Alias accumulation multi-session | `INSERT OR IGNORE` + unique constraint prevents duplicate accumulation by construction. | Yes — constraint still enforced |
| `_message_loop` debounce/FIFO | Orchestrator code (Rule 13 prohibits mutmut). 15+ behavioral contract tests cover all observable side effects. | Yes — tests still passing |

All three re-verified against source — justifications still hold.

## New Gaps This Cycle

### `_require()` over-strictness for tunable parameters — ACCEPTED

**Source:** Stage 7 (Documentation Audit) — discovered when auditing lucyd.toml.example completeness.

**Issue:** Stage 1's `_require()` conversion (Cycle 17) changed all config property access from `_deep_get()` (returns default on missing) to `_require()` (raises ConfigError on missing). This is correct for truly required values (agent.name, channel.type, models.primary) but over-strict for optional behavioral tuning parameters (queue_capacity, warning_pct, quote_max_chars, etc.) that have sensible defaults.

**Impact:** A new deployment copying lucyd.toml.example would crash on startup if any required key was missing. This was the root cause of 40+ missing keys in the example file.

**Mitigation applied:** lucyd.toml.example completely rewritten with all required keys uncommented and set to generic framework defaults. The example file is now copy-pasteable for new deployments.

**Why ACCEPTED (not fixed):** Reverting specific `_require()` calls to `_deep_get()` with built-in defaults is a design decision (tradeoffs: explicit-is-better vs convention-over-configuration). The practical impact is fully mitigated by the complete example file. Pattern P-020 catches config-to-example drift at audit time. Low priority for refactoring — no user-facing breakage with current mitigations.

## Findings Resolved This Cycle

All findings from Stages 1–7 were fixed inline during the audit. No deferred items reached Stage 8.

### Stage 3 Fixes
| # | Finding | Fix |
|---|---------|-----|
| 1 | `TestQueueRoutingInvariant` fails under mutmut trampoline | Added `@pytest.mark.skipif(MUTMUT_RUNNING)` — structural invariant test incompatible with AST-based mutation |

### Stage 7 Fixes (Documentation)
| # | Finding | Fix |
|---|---------|-----|
| 1 | README.md:113 — test count "~1725" stale (actual 1721) | Updated to "~1721" |
| 2 | README.md:140 — HTTP API "145 tests" stale (actual 143) | Updated to "143 tests" |
| 3 | README.md:140 — orchestrator "283 tests" stale (actual 297) | Updated to "297 tests" |
| 4 | docs/configuration.md — 12 config keys undocumented | All keys added with descriptions and defaults |
| 5 | lucyd.toml.example — 40+ required keys missing/commented | Complete rewrite with all keys uncommented |
| 6 | docs/diagrams.md — 16 line number references drifted | All references updated to match current source |

## Batch Fixes

- **Cosmetic debt:** None carried. Style findings (~30 in tests) remain cosmetic-only, permanently deferred.
- **Dependency updates:** No security-critical updates. pip-audit clean.
- **Missing tests:** None identified. All mutation survivors are cosmetic/equivalent.

## Final Test Run

```
1721 passed in 33.78s
```

All tests passing after all fixes applied.

## Confidence

97% — all findings resolved, one new gap formally accepted with full mitigation. Zero deployment-blocking debt.
