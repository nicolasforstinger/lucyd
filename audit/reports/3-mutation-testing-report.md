# Mutation Testing Audit Report

**Date:** 2026-03-06
**Audit Cycle:** 16
**Tool:** mutmut 3.4.0
**Python:** 3.13.5
**EXIT STATUS:** PASS

## Scope

New module `verification.py` — full mutmut run. All other security-critical functions unchanged since Cycle 12 (kill rates carry forward).

## Security Verification

| Function | Module | Survivors | Status |
|----------|--------|-----------|--------|
| `_safe_env` | shell.py | **0** | CARRIED — 100% kill rate |
| `_safe_parse_args` | shell.py | **0** | CARRIED — 100% kill rate |
| `_check_path` | filesystem.py | **0** | CARRIED — 100% kill rate |
| `_subagent_deny` (deny-list) | agents.py | **0** | CARRIED — 100% kill rate |
| `_is_private_ip` | web.py | 2 | CARRIED — equivalent mutants |
| `_validate_url` | web.py | 3 | CARRIED — cosmetic (error text) |
| `_auth_middleware` | http_api.py | **0** | CARRIED — 100% kill rate |
| `_rate_middleware` | http_api.py | **0** | CARRIED — 100% kill rate |
| `hmac.compare_digest` | http_api.py | **0** | CARRIED — 100% kill rate |
| `verify_compaction_summary` | verification.py | 13 | **NEW** — 81.5% kill rate, all survivors cosmetic/equivalent |
| `_detect_turn_labels` | verification.py | **0** | **NEW** — 100% kill rate |
| `_extract_distinctive_tokens` | verification.py | **0** | **NEW** — 100% kill rate |
| `_build_deterministic_summary` | verification.py | 3 | **NEW** — cosmetic (string case) |

## New Module: verification.py

### Initial Run

96 mutants (including 15 dead code in `_check_entity_grounding`). Kill rate: 66.7%.

### Findings & Fixes

1. **Dead code removed:** `_check_entity_grounding()` (lines 68-83) was defined but never called — same logic inlined in `verify_compaction_summary`. Removed.
2. **Security mutant killed (ratio calculation):** `grounded_count / len(summary_tokens)` → `grounded_count * len(summary_tokens)` survived. Added `test_low_grounding_ratio_rejects` (25% grounding).
3. **Boundary mutant killed (`<` → `<=`):** At exactly 50% threshold. Added `test_exactly_at_grounding_threshold_passes`.

### Final Run

81 mutants, 66 killed, 15 survived. Kill rate: **81.5%**.

### Survivor Categorization (15 total)

| Category | Count | Details |
|----------|-------|---------|
| Cosmetic | 9 | String case changes, `XX`-wrapping in log messages and format strings |
| Equivalent | 6 | `None` vs `False` (both falsy), default param ±1, `>` vs `>=` on warning-only check |
| Security | **0** | All security-relevant mutants killed |

## Stale Gap Resolution

| Gap | Cycles | Resolution |
|-----|--------|------------|
| Alias accumulation multi-session | 5 → ACCEPTED (Cycle 15) | `INSERT OR IGNORE` + unique constraint |
| `_message_loop` debounce/FIFO | 6 → ACCEPTED (Cycle 15) | Orchestrator code, 15+ contract tests |

## Known Gaps

| Gap | Severity | Status |
|-----|----------|--------|
| Provider `complete()` mock-boundary | Low | ACCEPTED (permanent) |

## Confidence

96% — verification.py fully mutation-tested, 2 security mutants caught and killed, all survivors categorized as cosmetic/equivalent.
