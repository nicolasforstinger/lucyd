# Static Analysis Report

**Date:** 2026-03-12
**Cycle:** 18
**Tools:** ruff 0.15.1, mypy SKIPPED (type hints present but not prioritized)
**Python version:** 3.13.5
**Files scanned:** 33 production, 40 test
**EXIT STATUS:** PASS

## Configuration

ruff.toml: S, E, F, W, B, UP, SIM, RET, PTH, I, TID enabled. S603/S607/S608/E501 ignored. Tests exempt from S101/S104/S105/S106/S108/S310.

## Findings Summary

| Category | Count | Fixed | Suppressed | Deferred |
|----------|-------|-------|------------|----------|
| SECURITY | 0 | 0 | 0 | 0 |
| BUG | 0 | 0 | 0 | 0 |
| DEAD CODE | 0 | 0 | 0 | 0 |
| STYLE | ~30 (tests only) | 0 | 0 | 30 |
| INTENTIONAL | 18 | 0 | 18 | 0 |

## Pattern Checks

| Pattern | Result |
|---------|--------|
| P-001 zip strict | Clean |
| P-002 gather BaseException | 1 site (agentic.py:279) — isinstance checks use BaseException correctly |
| P-005 duplicate tests | Duplicate function names across different classes — not actual shadows |
| P-010 noqa:S | 18 suppressions, all with explanations, all verified current |
| P-016 resource lifecycle | All sqlite3.connect calls have cleanup |
| P-018 unbounded data | Session/contacts dicts bounded by config (allow_from, tools) |
| P-020 magic numbers | 4 hardcoded timeouts in CLI tools (consolidation.py:531, indexer.py:264,324,427) |
| P-021 provider defaults | STT has OpenAI defaults in provider-specific function — acceptable |
| P-022 channel names | Clean |
| P-027 cost tracking | All LLM calls have cost tracking downstream |
| P-030 trace_id | Startup/shutdown logs exempt per policy |
| TODO/FIXME/HACK | Clean |

## Security Review

| Pattern | Occurrences | All Safe? | Details |
|---------|-------------|-----------|---------|
| subprocess | 1 (shell.py) | Yes | Safe env, explicit args, timeout, pid group kill |
| eval/exec | 0 | Yes | |
| pickle | 0 | Yes | |
| os.system | 0 | Yes | |
| SQL f-strings | 0 | Yes | |
| Hardcoded secrets | 0 | Yes | |
| tempfile | 1 (tts.py) | Yes | mkstemp in configured dir |

## Known Gaps

| Gap | Severity | Notes |
|-----|----------|-------|
| P-020: 4 hardcoded timeouts in CLI tools | Low | consolidation.py, indexer.py — standalone CLI tools |
| Test style findings (~30) | Low | E701, SIM117, PTH in tests — cosmetic |

## Confidence
Overall: 95%
