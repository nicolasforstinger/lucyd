# 0 — Full Audit Suite

**What:** Run every audit in sequence with gates between stages. Each stage must pass before the next begins. Bugs found at any stage trigger the bug fix workflow before proceeding.

**Why:** Individual audits catch individual classes of problems. Running them in sequence with gates ensures that each audit starts from a known-good state. Static analysis catches syntax issues before you waste time running broken tests. Test verification passes before you spend hours on mutation testing. Dependency chain verification catches missing producers before the security audit assumes all pipelines are live. Security review happens after all code quality and structural completeness is verified. Documentation is audited last because every prior stage might change what needs documenting.

**When to run:** Before any release, after major feature work, quarterly as hygiene, or when Nicolas says "audit everything."

---

## Execution Modes

This audit suite operates in two modes. Read both, pick the right one.

### Full Audit

Run all 8 stages sequentially with gates. Produces comprehensive reports and `LAST_AUDIT.md`. Use for: first audit, quarterly cycles, post-major-feature, release prep, or when Nicolas says "audit everything."

### Verification Pass

Lightweight follow-up that validates a previous full audit's findings without rediscovering the entire codebase. Use when: a fix batch just landed, a previous audit left FLAGGED items, or you need a quick confidence check.

**Verification Pass Protocol:**

```
1. READ audit/LAST_AUDIT.md from the previous full audit.
   If it doesn't exist, you must run a Full Audit instead.

2. VERIFY FIXED items:
   For each row with Status = FIXED:
   - Is the fix still present in source? (not reverted, not overwritten)
   - Did the fix introduce any regressions?
   - Run targeted tests for the affected module.

3. RE-EVALUATE FLAGGED items:
   For each row with Status = FLAGGED:
   - Is the ambiguity resolved? (code changed, decision made)
   - If resolved → fix it, update status.
   - If still ambiguous → carry forward with updated reasoning.

4. CHECK for fix-induced problems:
   - Did any fix remove code that was actually needed?
   - Any new import errors, broken references, cascading failures?
   - Any new deprecation warnings?

5. RUN full test suite:
   python -m pytest tests/ -q --tb=short

6. RUN audit chain spot-check:
   - Stage 1: ruff check (zero errors?)
   - Stage 5: freshness checks on critical data stores
   - Stage 7: cross-reference check (counts still match?)

7. SCAN for anything the previous pass missed:
   - New code added since last audit?
   - New modules, tools, channels, config keys?
   - Any of these undocumented or untested?

8. OUTPUT:
   - If clean → "AUDIT CLEAN" + date + verification summary
   - If not clean → produce delta table (new findings only):
     | # | Phase | File | Finding | Fix Applied | Status |
     Then fix the new findings.
   - Update LAST_AUDIT.md with verification results.
```

A verification pass is NOT a substitute for a full audit. It validates previous work and catches regressions. If the previous full audit is older than one quarter, or if major architectural changes happened since, run a full audit instead.

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

If `PATTERN.md` doesn't exist, this is the first audit cycle. Proceed without it. It will be created during the bug fix workflow (Step 6 of `9-BUG-FIX-WORKFLOW.md`).

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

### Pre-Audit: Audit Suite Self-Check

Before trusting the audit pipeline, verify the pipeline itself is sound. If the audit files contain stale references, outdated assumptions, or missing coverage, every subsequent stage inherits those blind spots.

**Run this before Stage 1. It takes 10 minutes. Skipping it risks an entire audit cycle built on wrong assumptions.**

```
1. STALE REFERENCES
   For each audit file (1-7 + PATTERN.md), check for references to:
   - Functions, classes, or modules that no longer exist in source
   - File paths that no longer exist
   - Config keys that were renamed or removed
   - Tool names or counts that changed

   Quick check:
   ```bash
   # Extract all backtick-quoted identifiers from audit files
   grep -ohP '`[a-zA-Z_./()\[\]]+`' audit/*.md | tr -d '`' | sort -u > /tmp/audit_refs.txt
   # Spot-check against source (focus on function/module names)
   while read ref; do
     grep -rq "$ref" *.py tools/ channels/ providers/ 2>/dev/null || echo "MISSING: $ref"
   done < /tmp/audit_refs.txt
   ```
   Not every reference will be a code identifier (some are prose), but any
   MISSING result for a function or module name is a stale reference.

2. OUTDATED ASSUMPTIONS
   Read each stage's "How to Think" section. Does it still describe the
   current architecture? Specifically:
   - Does Stage 4 still describe the correct _process_message flow?
   - Does Stage 5's pipeline inventory match current cron jobs?
   - Does Stage 6's threat model reflect current input sources?
   - Do the contract test categories in Stage 4 cover all current behaviors?

3. MISSING COVERAGE
   Compare the audit suite's scope against current source:
   ```bash
   # New source files since last audit
   last_audit=$(grep -m1 'Date:' audit/reports/0-full-audit-report.md 2>/dev/null | grep -oP '\d{4}-\d{2}-\d{2}')
   [ -n "$last_audit" ] && git log --diff-filter=A --name-only --since="$last_audit" -- '*.py' | sort
   ```
   For each new file: is it covered by at least one stage's scope? New tools
   need Stage 3 + Stage 6. New channels need Stage 6. New config modules need
   Stage 7.

4. CHAIN INTEGRITY
   Verify the execution order still makes sense:
   - Does each stage's output feed the next stage's input?
   - Are there any circular dependencies between stages?
   - Does the bug fix workflow (Stage 8) still reference correct stage names?
   - Are report file paths consistent across all files?

5. ANTI-PATTERNS IN THE AUDITORS
   Check for problems in the audit files themselves:
   - Redundant checks across multiple stages (same grep in Stage 1 and Stage 6
     without cross-reference)
   - Contradictory rules between stages
   - Overly broad pattern matches that would flag correct code
   - Overly narrow checks that miss obvious variants
   - Pattern checks that reference patterns not in PATTERN.md

6. DRIFT FROM CODEBASE REALITY
   Compare what the audit suite assumes about the codebase against reality:
   - Directory structure in examples vs actual layout
   - Naming conventions referenced vs actual conventions
   - Tool/channel/provider counts assumed vs actual
   ```bash
   # Quick reality check
   echo "Source files:"; find . -name "*.py" -not -path "./.venv/*" -not -path "./tests/*" -not -path "./mutants/*" -not -path "./__pycache__/*" | wc -l
   echo "Test files:"; find tests/ -name "*.py" | wc -l
   echo "Tool modules:"; ls tools/*.py | grep -v __init__ | wc -l
   echo "Channel modules:"; ls channels/*.py | grep -v __init__ | wc -l
   echo "Provider modules:"; ls providers/*.py | grep -v __init__ | wc -l
   ```
```

If the self-check finds issues, fix them NOW — before starting Stage 1. Update the audit files, commit the fixes, and proceed with a clean pipeline. Do not start an audit cycle with known-broken audit files.

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
┌─────────────────────────────────────┐
│  8-REMEDIATION (this file, below)   │
│  Resolve ALL carried gaps. Write    │
│  missing tests, fix stale debt,     │
│  update deps. The audit is not a    │
│  report — it's a repair cycle.      │
└──────────────┬──────────────────────┘
               │ Gate: No gap older than
               │ 3 cycles remains unresolved
               ▼
            ✓ DONE
```

**9-BUG-FIX-WORKFLOW.md** is not in the chain. It's triggered when ANY stage finds a bug that needs fixing. After the fix, control returns to where it left off.

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
  - Read audit/9-BUG-FIX-WORKFLOW.md.
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
  - Read audit/9-BUG-FIX-WORKFLOW.md.
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
- PASS → Proceed to Stage 8.
- FAIL → Fix the docs. Re-run Phase 5 (cross-reference check). Must PASS.

### Stage 8: Remediation

This is not a reporting stage — it is a **fix stage**. The audit is a repair cycle, not a diagnostic report. Stages 1–7 find problems. Stage 8 fixes every problem that wasn't fixed inline during those stages.

```
1. COLLECT all gaps from:
   - All 7 stage reports (this cycle)
   - audit/LAST_AUDIT.md (carried from previous cycles)

2. CLASSIFY each gap by age:
   - New (1st cycle): just discovered
   - Active (2–3 cycles): known, not yet resolved
   - Stale (4+ cycles): must be resolved THIS cycle

3. RESOLVE stale gaps (4+ cycles). For each one, pick exactly ONE:
   a. FIX IT — write the test, fix the code, update the config.
      Re-run affected tests to verify.
   b. ACCEPT IT — write a justification explaining why this gap
      is permanently acceptable. Must include:
      - Why the gap cannot or should not be fixed
      - What the actual risk is (not "low" — be specific)
      - What compensating controls exist
      Once accepted, the gap stops appearing in future audits.
   There is no option (c). "Carry forward" is not available for stale gaps.

4. RESOLVE active gaps (2–3 cycles). Same options as above,
   but deferral to the next cycle is allowed ONE more time
   with a written reason. If deferred, it becomes stale next cycle.

5. RESOLVE new gaps. Fix if possible. Defer with reason if not.

6. BATCH FIXES:
   - Cosmetic debt (ruff STYLE findings carried multiple cycles):
     fix them all in one pass. They're small. Stop carrying them.
   - Dependency updates (outdated packages with security relevance):
     update them. Run tests.
   - Missing tests identified by mutation testing:
     write them. This is the actual value of the audit.

7. RE-RUN full test suite after all fixes:
   python -m pytest tests/ -q --tb=short
   All tests must pass. If a fix broke something, fix the regression.

8. UPDATE stage reports if fixes changed their findings.
```

**Gate check:** No gap older than 3 cycles remains unresolved (fixed or accepted)?
- PASS → Proceed to aggregate report.
- PARTIAL → Stale gaps remain because they require architectural decisions.
  Document them as FLAGGED for Nicolas. The audit exits PARTIAL.
- FAIL → Stale gaps remain with no justification. Not acceptable.

---

## Bug Fix Trigger

At ANY point during any stage, if a bug is found that needs fixing:

```
1. STOP the current stage
2. Read audit/9-BUG-FIX-WORKFLOW.md
3. Follow the complete workflow for the bug
4. Return to the stage where the bug was found
5. Re-run from the beginning of that stage (not from where you stopped)
```

Re-running from the beginning is non-negotiable. The bug fix may have changed behavior that earlier checks in the same stage depend on.

---

## Findings Pause Valve

If the cumulative finding count across Stages 1–3 exceeds **50 findings**, pause before proceeding to Stage 4. Produce the summary table for all findings so far and present it to Nicolas for review. Reasons:

- 50+ findings suggest systemic issues, not isolated bugs. Fixing them one-by-one may be the wrong approach.
- Stages 4–7 build on the assumption that basic code quality is sound. If it's not, their results are unreliable.
- Nicolas may want to prioritize, batch, or defer some findings before committing hours to mutation testing and security analysis.

Resume after approval. If Nicolas says "fix all and continue," proceed through Stage 8 for each finding, then continue the pipeline.

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

### Post-Audit: Known Gaps Review (P-019)

**This review happens AFTER Stage 8 (Remediation).** Stage 8 should have already resolved all stale gaps. This review verifies that and documents the final state.

```
1. Collect all "Known Gaps" from all 8 stage reports.
2. Compare against the Known Gaps from the previous cycle's LAST_AUDIT.md.
3. For each carried-forward gap, VERIFY it is still open:
   - Read the relevant source code or tests.
   - If the gap has been fixed (code changed, tests added, config updated)
     since it was first reported → status: Resolved.
   - Do NOT carry forward a gap without checking the code.
4. For each verified gap:
   - New this cycle → status: Open (max 1 cycle carry)
   - Carried 2-3 cycles → status: Active (must resolve next cycle)
   - Carried 4+ cycles → MUST have been resolved in Stage 8.
     If still here, the audit exits PARTIAL.
   - Formally justified as permanent → status: Accepted (stops appearing)
5. Record the final gaps table in the aggregate report.

STALENESS RULE: A gap that has been "Open" or "Active" for 4+
cycles without being fixed or formally accepted is a failure of
the audit process, not of the codebase. The audit exists to fix
things, not to document the same problems forever.
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
| 8. Remediation | PASS/PARTIAL | | | |

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
- **No gap older than 3 cycles remains unresolved**
- All cosmetic debt resolved or formally accepted

### PARTIAL means:
- All security requirements met
- Some non-security items deferred with documentation
- Stale gaps remain only because they require Nicolas's architectural decision
- No blockers for deployment

### FAIL means:
- Security vulnerability found and not yet fixed
- Tests failing
- Critical data pipeline has no producer
- Critical doc mismatch that could mislead users
- **Stale gaps (4+ cycles) remain without fix or formal acceptance**

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

## LAST_AUDIT.md
[This section is auto-generated — see Post-Audit: Write LAST_AUDIT.md below]

## Deferred Items
[Anything marked PARTIAL with justification for deferral]

## Recommendations
[What should be done before next audit]
```

### Post-Audit: Write LAST_AUDIT.md

After the aggregate report is complete, write a persistent summary to `audit/LAST_AUDIT.md`. This file is the handoff to verification passes and future audit cycles. It must be self-contained — a future agent with zero context reads this file and knows exactly what happened.

```markdown
# Last Audit Summary

**Date:** [date]
**Mode:** Full Audit
**EXIT STATUS:** [PASS / PARTIAL / FAIL]
**Test count:** [count] passing
**Source modules:** [count]

## Findings

| # | Stage | File | Finding | Fix Applied | Status |
|---|-------|------|---------|-------------|--------|

Status: FIXED / FLAGGED (needs human decision) / BLOCKED (could not fix without breakage)

## Known Gaps Carried Forward

Gaps use staleness classification:
- **Open** (1 cycle): New this cycle. Will be resolved next cycle.
- **Active** (2-3 cycles): Known, deferred with reason. MUST resolve next cycle or becomes stale.
- **Accepted**: Formally justified as permanent. Written justification on record. Stops appearing.

Gaps that would be 4+ cycles old MUST have been resolved in Stage 8 (Remediation).
If any remain, the audit exits PARTIAL.

| # | Gap | Status | Cycles | Justification (if Active) |
|---|-----|--------|--------|---------------------------|

## Resolved This Cycle
[Gaps fixed in Stage 8 that were previously carried forward]

## Accepted This Cycle
[Gaps formally accepted with written justification]

## Patterns Created This Cycle
[P-NNN entries added during this audit]
```

**Rules for LAST_AUDIT.md:**
- Overwrite on every full audit (not append — the file represents the LATEST state)
- Verification passes append a section, not overwrite
- Every row in the findings table must have a Status — no blanks
- FLAGGED items must include the reason for ambiguity
- BLOCKED items must include what would unblock them

### Post-Audit: Fix-Everything Mandate

The audit's job is to find AND fix. Findings without fixes are documentation, not remediation.

**Default policy: fix every finding.** Do not skip, defer, or deprioritize unless:
1. The fix requires an architectural decision only Nicolas can make → Status: FLAGGED
2. The fix would break a running production system with no safe rollback → Status: BLOCKED
3. Nicolas explicitly approves deferral

Every other finding gets fixed in this cycle. A finding deferred "because it's low severity" is a finding that will be deferred every cycle forever. Fix it now or accept it formally.

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
