# 0 — Full Audit Suite

**What:** Run every audit in sequence with gates between stages. Each stage must pass before the next begins. Bugs found at any stage trigger the bug fix workflow before proceeding.

**Why:** Individual audits catch individual classes of problems. Running them in sequence with gates ensures that each audit starts from a known-good state. Static analysis catches syntax issues before you waste time running broken tests. Test verification passes before you spend hours on mutation testing. Dependency chain verification catches missing producers before the security audit assumes all pipelines are live. Security review happens after all code quality and structural completeness is verified. Documentation is audited last because every prior stage might change what needs documenting.

**When to run:** Before any release, after major feature work, quarterly as hygiene, or when Nicolas says "audit everything."

---

## Prerequisites

Before starting, verify the environment:

```bash
# Navigate to project root — use wherever the repo lives, not a hardcoded path
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

# Verify we're in the right place
[ -f lucyd.py ] || { echo "ERROR: lucyd.py not found. Wrong directory."; exit 1; }

source .venv/bin/activate

# Python available
python --version

# Pytest available
python -m pytest --version

# mutmut available
mutmut --version

# ruff available (install if not)
ruff --version || pip install ruff --break-system-packages

# Project structure intact
ls lucyd.py channels/ tools/ tests/ docs/
```

**Confidence check:** Can you run all 5 commands above without errors? If any tool is missing, install it before proceeding. Do not start the audit suite with missing tools.

### Pattern Library

Before starting any stage, verify the pattern library exists:

```bash
ls audit/PATTERN.md
```

`PATTERN.md` contains accumulated findings from previous audit cycles — classes of bugs that recur. **Every stage reads its applicable patterns from PATTERN.md and runs the checks before starting its own work.** This is not optional. The pattern checks are baked into each stage methodology file.

If `PATTERN.md` doesn't exist, this is the first audit cycle. Proceed without it. It will be created during the bug fix workflow (Step 6 of `8-BUG-FIX-WORKFLOW.md`).

### Pre-Audit Retrospective

Before starting Stage 1, check for unprocessed production fixes:

```
1. Have any production fixes, hardening batches, or incident responses
   occurred since the last audit cycle?
2. If YES: Run the Retrospective Protocol from PATTERN.md.
   - For each fix: trace which stage should have caught it, create patterns.
   - New patterns will then be checked during this audit cycle.
3. If NO: Proceed directly to Stage 1.
```

This ensures that lessons from production don't wait until the *next* audit cycle to take effect. The retrospective runs once at the start, generates patterns, and those patterns are immediately active for the stages that follow.

```bash
# Quick check: commits since last audit report date
last_audit=$(grep -m1 'Date:' audit/reports/0-full-audit-report.md 2>/dev/null | grep -oP '\d{4}-\d{2}-\d{2}')
[ -n "$last_audit" ] && git log --oneline --since="$last_audit" -- '*.py' | grep -i 'fix\|harden\|patch\|hotfix'
```

If any commits look like production fixes that weren't triggered by the audit pipeline, they need retrospective analysis before proceeding.

---

## Execution Order

```
┌─────────────────────────────────────┐
│  1-STATIC-ANALYSIS.md               │
│  Fast. Catches syntax, types, dead  │
│  code before wasting time on tests. │
└──────────────┬──────────────────────┘
               │ Gate: Zero errors
               ▼
┌─────────────────────────────────────┐
│  2-TEST-SUITE.md                    │
│  Run all tests. Baseline must be    │
│  green before verifying quality.    │
└──────────────┬──────────────────────┘
               │ Gate: All tests pass
               ▼
┌─────────────────────────────────────┐
│  3-MUTATION-TESTING.md              │
│  Verify component tests actually    │
│  kill mutants. Hours, not minutes.  │
└──────────────┬──────────────────────┘
               │ Gate: Security functions
               │ at target kill rates
               ▼
┌─────────────────────────────────────┐
│  4-ORCHESTRATOR-TESTING.md          │
│  Verify daemon wiring via contract  │
│  tests and extracted decisions.     │
└──────────────┬──────────────────────┘
               │ Gate: All contract tests
               │ pass, extractions verified
               ▼
┌─────────────────────────────────────┐
│  5-DEPENDENCY-CHAIN.md              │
│  Map producer→consumer pipelines.   │
│  Verify nothing reads from a source │
│  that nothing writes to.            │
└──────────────┬──────────────────────┘
               │ Gate: All pipelines have
               │ active producers, data fresh
               ▼
┌─────────────────────────────────────┐
│  6-SECURITY-AUDIT.md                │
│  Find NEW attack surfaces. Map      │
│  inputs → capabilities → boundaries.│
└──────────────┬──────────────────────┘
               │ Gate: All security
               │ boundaries verified
               ▼
┌─────────────────────────────────────┐
│  7-DOCUMENTATION-AUDIT.md           │
│  Sync docs to source. Last because  │
│  everything above might change what │
│  needs documenting.                 │
└──────────────┬──────────────────────┘
               │ Gate: All docs match source
               ▼
            ✓ DONE
```

**8-BUG-FIX-WORKFLOW.md** is not in the chain. It's triggered when ANY stage finds a bug that needs fixing. After the fix, control returns to where it left off.

---

## How to Run Each Stage

### Stage 1: Static Analysis

```
Read audit/1-STATIC-ANALYSIS.md fully. Execute all phases.
Write report to audit/reports/1-static-analysis-report.md.
```

**Gate check:** Does the report say EXIT STATUS: PASS?
- PASS → Proceed to Stage 2.
- FAIL → Read the findings. Are they fixable now?
  - Yes → Fix them. Re-run Stage 1. Verify PASS.
  - No (e.g., requires architectural change) → Document in report, proceed with PARTIAL. Nicolas decides later.

### Stage 2: Test Suite

```
Read audit/2-TEST-SUITE.md fully. Execute all phases.
Write report to audit/reports/2-test-suite-report.md.
```

**Gate check:** Does the report say EXIT STATUS: PASS?
- PASS → Proceed to Stage 3.
- FAIL → Tests are broken. This blocks everything.
  - Read audit/8-BUG-FIX-WORKFLOW.md.
  - Fix each failing test using the workflow.
  - Re-run Stage 2. Must PASS before proceeding.

### Stage 3: Mutation Testing

```
Read audit/3-MUTATION-TESTING.md fully. Execute all phases.
Write report to audit/reports/3-mutation-testing-report.md.
```

**Gate check:** Does the report show security functions at target kill rates?
- PASS → Proceed to Stage 4.
- PARTIAL (security verified, some behavioral gaps) → Acceptable. Proceed.
- FAIL (security functions have unverified mutants) → Fix using Stage 3's remediation process. Re-verify. Security functions must pass before proceeding.

### Stage 4: Orchestrator Testing

```
Read audit/4-ORCHESTRATOR-TESTING.md fully. Execute all phases.
Write report to audit/reports/4-orchestrator-testing-report.md.
```

**Gate check:** All contract tests pass, extracted functions mutation-verified?
- PASS → Proceed to Stage 5.
- FAIL → Fix contracts or extractions. Re-run. Must PASS.

### Stage 5: Dependency Chain

```
Read audit/5-DEPENDENCY-CHAIN.md fully. Execute all phases.
Write report to audit/reports/5-dependency-chain-report.md.
```

**Gate check:** All data pipelines have active producers? All data sources fresh? Round-trip tests exist?
- PASS → Proceed to Stage 6.
- PARTIAL (non-critical pipeline gap, non-deterministic source slightly stale) → Acceptable. Document and proceed.
- FAIL → Critical pipeline has no producer, or security-relevant pipeline lacks round-trip coverage.
  - Fix the pipeline (add missing producer, write missing test).
  - Re-run Stage 2 (verify new tests pass).
  - Re-run Stage 5 to confirm fix.

### Stage 6: Security Audit

```
Read audit/6-SECURITY-AUDIT.md fully. Execute all phases.
Write report to audit/reports/6-security-audit-report.md.
```

**Gate check:** All input→capability→boundary chains verified?
- PASS → Proceed to Stage 7.
- FAIL → New vulnerabilities found.
  - Read audit/8-BUG-FIX-WORKFLOW.md.
  - Fix each vulnerability using the workflow.
  - Re-run relevant tests (Stage 2).
  - Re-verify affected mutations (Stage 3).
  - Re-run Stage 6 to confirm fix.

### Stage 7: Documentation Audit

```
Read audit/7-DOCUMENTATION-AUDIT.md fully. Execute all phases.
Write report to audit/reports/7-documentation-audit-report.md.
```

**Gate check:** All docs match source?
- PASS → Done.
- FAIL → Fix the docs. Re-run Phase 5 (cross-reference check). Must PASS.

---

## Bug Fix Trigger

At ANY point during any stage, if a bug is found that needs fixing:

```
1. STOP the current stage
2. Read audit/8-BUG-FIX-WORKFLOW.md
3. Follow the complete workflow for the bug
4. Return to the stage where the bug was found
5. Re-run from the beginning of that stage (not from where you stopped)
```

Re-running from the beginning is non-negotiable. The bug fix may have changed behavior that earlier checks in the same stage depend on.

---

## Aggregate Report

After all stages complete, run the post-audit steps, then write the aggregate report.

### Post-Audit: Pattern Sweep

Review all bugs found and fixed during this audit cycle. The bug fix workflow (Step 6) creates patterns per-bug, but an aggregate sweep catches patterns that only emerge when looking at multiple findings together:

```
1. List all bugs fixed during this cycle (from stage reports).
2. Group by root cause class. Do any share a common class not yet in PATTERN.md?
3. For each new class: create a pattern entry using the Retrospective Protocol.
4. Review the Pattern Index — are new patterns indexed to the right stages?
```

### Post-Audit: Known Gaps Review

```
1. Collect all "Known Gaps" from all 7 stage reports.
2. Compare against the Known Gaps from the previous cycle's aggregate report.
3. For each carried-forward gap, VERIFY it is still open:
   - Read the relevant source code or tests.
   - If the gap has been fixed (code changed, tests added, config updated)
     since it was first reported → status: Resolved (stale finding).
   - Do NOT carry forward a gap without checking the code. Stale findings
     erode trust in the audit.
4. For each verified gap:
   - New this cycle → status: Open
   - Carried from previous cycle, now fixed → status: Resolved
   - Carried from previous cycle, mitigated by pattern/test → status: Mitigated
   - Carried 2+ cycles without action → status: Escalated
5. Record the final gaps table in the aggregate report.
```

### Post-Audit: Remediation Plan

After the Known Gaps table, produce a remediation plan for all Open and Escalated gaps. This is the actionable output of the audit — the "fix these before next cycle" deliverable.

For each gap:
- **What:** One-sentence description of the fix
- **Where:** File path(s) and function/line if known
- **Scope:** Estimated lines of change (1-line, ~10 lines, ~50 lines, etc.)
- **Priority:** severity × cycles_open (Escalated gaps are always Priority 1)

```
| # | Gap | Priority | What | Where | Scope |
|---|-----|----------|------|-------|-------|
```

Sort by priority (highest first). This table goes in the aggregate report AND gets presented to the operator after audit completion as the next work items.

### Aggregate Report

Write the aggregate report to `audit/reports/0-full-audit-report.md`:

```markdown
# Full Audit Report

**Date:** [date]
**Total duration:** [time]
**Triggered by:** [release prep / quarterly / feature completion / manual request]

## Stage Results

| Stage | Status | Duration | Findings | Fixes |
|-------|--------|----------|----------|-------|
| 1. Static Analysis | PASS/FAIL/PARTIAL | | | |
| 2. Test Suite | PASS/FAIL | | | |
| 3. Mutation Testing | PASS/PARTIAL | | | |
| 4. Orchestrator Testing | PASS/FAIL | | | |
| 5. Dependency Chain | PASS/PARTIAL/FAIL | | | |
| 6. Security Audit | PASS/FAIL | | | |
| 7. Documentation Audit | PASS/FAIL | | | |

## Bug Fixes Applied
[List each bug found, which stage found it, root cause, fix, verification]

## Overall Assessment
EXIT STATUS: PASS / PARTIAL / FAIL

### PASS means:
- Zero static analysis errors
- All tests green
- Security mutation kill rates at target
- All contract tests passing
- All data pipelines have active producers
- No unmitigated security vulnerabilities
- All docs match source

### PARTIAL means:
- All security requirements met
- Some non-security items deferred with documentation
- No blockers for deployment

### FAIL means:
- Security vulnerability found and not yet fixed
- Tests failing
- Critical data pipeline has no producer
- Critical doc mismatch that could mislead users

## Patterns
### Pre-audit retrospective
[Production fixes analyzed, patterns created: list]

### Patterns created during this cycle
[From bug fixes and aggregate sweep: list]

### Pattern index changes
[New stage assignments, retired patterns]

## Known Gaps
| Gap | Source | Status | Cycles Open | Action |
|-----|--------|--------|-------------|--------|

Status: Open / Mitigated / Resolved / Accepted / Escalated
Gaps open 2+ cycles without action are escalated.
Carried-forward gaps MUST be verified against current code before re-listing.

## Remediation Plan

| # | Gap | Priority | What | Where | Scope |
|---|-----|----------|------|-------|-------|

Sort by priority (highest first). Escalated gaps are always Priority 1.
These items should be addressed before the next audit cycle.

## Deferred Items
[Anything marked PARTIAL with justification for deferral]

## Recommendations
[What should be done before next audit]
```

---

## What This File Does NOT Contain

This file is a sequencer. It contains no methodology, no testing techniques, no security analysis, no documentation rules. All methodology lives in the individual audit files (1-8). If you need to understand HOW to do something, read the numbered file. This file only tells you WHAT ORDER to do it in and WHEN TO STOP.

---

## Rules

1. **Read each audit file fully before executing it.** Not skimming. Fully.
2. **One stage at a time.** Do not parallelize. Gate checks exist for a reason.
3. **Re-run from stage start after any bug fix.** No resuming mid-stage.
4. **Reports are mandatory.** Every stage produces a report. No exceptions.
5. **FAIL means stop.** Unless explicitly noted as proceeding with PARTIAL.
6. **The aggregate report is the final deliverable.** Nicolas reads this, not the individual reports.
7. **If confidence in any step drops below 90%, stop and reassess.** State what you're unsure about. Don't proceed on assumptions.
8. **NEVER explain away a finding.** If a test fails, fix the code — don't rewrite the test to accept broken behavior. If a security function doesn't handle an input class, fix the function — don't document it as "by design" or "defense is at another layer." The moment you feel the urge to categorize a security gap as acceptable, STOP. That urge is the most dangerous failure mode in this entire audit suite. Ask: "If an attacker knew about this gap, could they exploit it?" If the answer is anything other than a confident, evidence-backed "no," fix the code.
