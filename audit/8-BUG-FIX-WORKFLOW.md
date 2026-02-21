# 8 — Bug Fix Workflow

**What:** Structured process for fixing bugs found by any audit stage. Takes a finding from any audit (static analysis, test suite, mutation testing, orchestrator testing, security audit, documentation audit) and produces: a fix, a verified test, and an updated report.

**Why:** Bugs found during audits need consistent handling. Without a structured process, fixes are ad-hoc — the bug gets patched but no test is written, or a test is written but it doesn't actually verify the fix, or the fix introduces a regression that nobody catches.

The `expanduser()` bug is the template. Found during manual testing of the monitor feature. Root cause: `Path("~/.lucyd/monitor.json").expanduser()` hardcoded the path instead of using `self.config.state_dir / "monitor.json"`. The old tests PASSED on the broken code because they mocked `expanduser()`, papering over the bug. The fix and the test rewrite were coupled correctly — new tests fail on old code, pass on new code.

This workflow ensures every bug gets the same treatment: reproduce → root cause → failing test → fix → verify the fix → verify no regressions → document.

**When to run:** Triggered by any audit stage that finds a bug. Not part of the sequential chain — it's the escape hatch. After the fix, control returns to the audit stage that triggered it.

---

## How to Think

You are fixing a specific bug, not improving the codebase. Scope is narrow: one bug, one fix, one test, one verification. Resist the urge to refactor adjacent code, fix nearby style issues, or improve test coverage beyond what's needed for this bug.

**Root cause, not symptoms.** The bug might manifest as a test failure, a mutation survivor, a security gap, or a doc discrepancy. The fix must address the ROOT CAUSE, not the symptom. If the symptom is "mutation survived," the root cause might be "test mocks the function under test" or "assertion is too weak" or "the security check is actually missing."

**Every fix gets a test.** No exceptions. If you can't write a test that fails on the old code and passes on the new code, you haven't verified the fix. If the bug is in untestable code (like the orchestrator), write a contract test.

**Every fix gets a regression check.** Run the full test suite after every fix. If the fix breaks something else, the fix is wrong — don't patch the other thing, fix the fix.

**Never write a test that asserts broken behavior is correct.** When you write the failing test (Step 3), you decide what "correct" means. If the function doesn't handle an input and you write `assert result is False` — you've just enshrined the bug as a feature. The test should assert what the function SHOULD do, not what it currently does. If `_is_private_ip("0177.0.0.1")` returns `False`, the correct test is `assert _is_private_ip("0177.0.0.1") is True` — then fix the function to make it pass. Not `assert _is_private_ip("0177.0.0.1") is False` with a comment about defense at the DNS layer.

**Confidence gate:** Before applying any fix:
- Am I 90%+ confident I've found the root cause (not just a symptom)?
- Am I 90%+ confident the fix addresses the root cause?
- Am I 90%+ confident the fix doesn't change other behavior?

If any answer is below 90%, investigate further before fixing.

---

## Input: Bug Report

Every bug enters this workflow with a report from the audit stage that found it:

```markdown
## Bug Report
Source audit: [1-STATIC / 2-TEST / 3-MUTATION / 4-ORCHESTRATOR / 5-DEPCHAIN / 6-SECURITY / 7-DOC]
Finding: [What the audit found]
File: [Source file where the bug exists]
Line: [If known]
Severity: [CRITICAL / HIGH / MEDIUM / LOW]
Category: [Security / Behavioral / Quality / Documentation]
```

If the triggering audit didn't provide this, construct it from the audit's findings before proceeding.

---

## Step 1: Reproduce

**Why:** Confirm the bug is real. Some findings are false positives, equivalent mutants, or environmental issues. Don't fix what isn't broken.

### For Test Failures (from Stage 2)

```bash
# Run the specific failing test
python -m pytest tests/test_file.py::test_name -x -v
# Confirm it fails
# Record the exact error message
```

### For Mutation Survivors (from Stage 3)

```bash
# Show the specific mutant
mutmut show <ID>
# Record what was changed
# Apply the mutation manually
# Run the test that should catch it
python -m pytest tests/test_file.py::test_name -x -v
# Confirm the test PASSES (doesn't catch the mutation) — this IS the bug
```

### For Security Gaps (from Stage 6)

```bash
# Construct an adversarial input that should be blocked
# Run it through the relevant function
python -c "
from tools.filesystem import tool_read
import asyncio
result = asyncio.run(tool_read('/etc/shadow'))
print(result)
"
# If it succeeds → vulnerability confirmed
# If it's blocked → finding may be false positive, investigate further
```

### For Static Analysis Findings (from Stage 1)

```bash
# Verify the finding is real
ruff check <file> --select <RULE>
# Read the flagged code
# Is it actually a bug, or is it intentional?
```

### For Documentation Discrepancies (from Stage 7)

```bash
# Verify the source says what the audit claims
grep -n "<relevant_pattern>" <source_file>
# Verify the doc says something different
grep -n "<relevant_pattern>" <doc_file>
```

**Confidence check:** Is the bug real? Am I 90%+ confident this needs fixing? If not, flag as "NEEDS REVIEW" and return to the triggering audit with the finding marked as unconfirmed.

---

## Step 2: Root Cause

**Why:** Fixing symptoms creates new bugs. The `expanduser()` bug looked like "monitor writes to wrong path." The root cause was "hardcoded path bypasses config system." The symptom-fix would be "change the hardcoded path." The root-cause fix was "use config.state_dir."

### Trace to Root Cause

```
1. WHAT is the observable bug? (symptom)
2. WHAT code produces the bug? (location)
3. WHY does the code behave this way? (mechanism)
4. WHAT assumption is wrong? (root cause)
5. WHERE ELSE might this assumption be wrong? (scope)
```

### Root Cause Categories

| Category | Example | Fix Pattern |
|----------|---------|-------------|
| **Wrong assumption** | Path is always relative | Add validation |
| **Missing check** | No boundary on this path | Add boundary |
| **Wrong mock in test** | Mock hides the bug | Fix the test |
| **Incorrect assertion** | `in` instead of `==` | Strengthen assertion |
| **Config bypass** | Hardcoded value ignoring config | Use config system |
| **Race condition** | Async operations interleave | Add synchronization |
| **Type mismatch** | Function expects str, gets None | Add type check or fix caller |
| **Stale reference** | Code references removed feature | Update or remove |

### Check Scope

Does this root cause affect other parts of the codebase?

```bash
# If root cause is "hardcoded path instead of config":
grep -rn "expanduser\|Path.*~/" lucyd.py tools/ channels/ | grep -v test

# If root cause is "missing boundary on input":
# Check if other tool modules have the same gap
for toolfile in $(find tools/ -name "*.py" -not -name "__init__.py" -not -path "*__pycache__*"); do
    grep -l "_check_path\|_validate\|_is_private" "$toolfile" > /dev/null || echo "NO BOUNDARY: $toolfile"
done
```

If the same root cause appears elsewhere, fix ALL instances. Don't leave copies of the same bug.

**Confidence check:** Am I 90%+ confident this is the ROOT CAUSE and not a symptom? If removing/changing this one thing fixes the bug AND explains why the bug existed, it's the root cause.

---

## Step 3: Write Failing Test

**Why:** A test that fails on the current (broken) code and passes on the fixed code is PROOF the fix works. Without it, you're hoping.

### For Source Bugs

Write a test that:
1. Exercises the buggy code path
2. Asserts on the correct behavior (which currently fails)
3. Does NOT mock away the thing that's broken

```bash
# Run the test — it should FAIL against current code
python -m pytest tests/test_file.py::test_new_name -x -v
# Record: FAILED (expected)
```

If you can't write a failing test (the code is too tangled), that's information about the code's testability. Consider extracting the logic first (see `4-ORCHESTRATOR-TESTING.md`).

### For Mutation Survivors

The "test" is a new or fixed test that kills the mutant:
1. Write/fix the test using the anti-drift rules from `3-MUTATION-TESTING.md`
2. Apply the mutation manually
3. Run the test — it should FAIL against the mutated code
4. Revert the mutation
5. Run the test — it should PASS against the original code

### For Security Gaps

Write a test that:
1. Sends adversarial input through the vulnerable path
2. Asserts the input is blocked/sanitized/rejected
3. Currently FAILS because the boundary doesn't exist

### For Doc Discrepancies

No test needed. Fix the doc directly. Skip to Step 5.

---

## Step 4: Fix

**Why:** Now — and only now — fix the code.

### Fix Protocol

```
1. Make the smallest change that addresses the root cause
2. Do NOT refactor adjacent code
3. Do NOT fix nearby style issues
4. Do NOT improve test coverage beyond this bug
5. Run the failing test — it should now PASS
6. Run the full test suite — all should PASS
```

```bash
# Apply fix
# ...

# Verify the specific test now passes
python -m pytest tests/test_file.py::test_new_name -x -v
# Record: PASSED

# Verify no regressions
python -m pytest tests/ -q
# Record: all pass
```

**If the fix breaks other tests:** The fix is wrong, or the other tests are wrong. Investigate. Do NOT patch the other tests to accommodate your fix without understanding why they break.

---

## Step 5: Verify the Fix

**Why:** Confirm the fix addresses the root cause, not just the symptom.

### Step 5a: Test passes on fixed code

```bash
python -m pytest tests/test_file.py::test_new_name -x -v
# PASSED
```

### Step 5b: Test fails on broken code

If the fix is a source change, temporarily revert it and run the test:

```bash
git stash
python -m pytest tests/test_file.py::test_new_name -x -v
# Should FAIL — this proves the test catches the bug
git stash pop
```

If the fix is a test change (for mutation survivors), apply the mutation manually:

```bash
# Edit source to apply the mutation
python -m pytest tests/test_file.py::test_new_name -x -v
# Should FAIL — test catches the mutation
git checkout <source_file>
```

### Step 5c: Full regression check

```bash
python -m pytest tests/ -q
# ALL pass
```

### Step 5d: Scope check

If Step 2 found the same root cause elsewhere, verify all instances are fixed:

```bash
# Re-run the search that found the scope
# Confirm all instances are addressed
```

**Confidence check:** Am I 90%+ confident that:
1. The fix addresses the root cause?
2. The test proves the fix works?
3. The test would catch the bug if it was reintroduced?
4. No regressions were introduced?

If any answer is below 90%, investigate further.

---

## Step 6: Document

**Why:** The fix is worthless if nobody knows what was wrong or why it was changed.

### Commit

```bash
git add <changed_files>
git commit -m "fix(<module>): <one-line description>

Root cause: <what was actually wrong>
Found by: <which audit stage>
Test: <test name that verifies the fix>"
```

### Update Audit Report

Return to the audit stage that triggered this workflow. Update its report:

```markdown
## Bug Fix Applied
Finding: [original finding]
Root cause: [what was actually wrong]
Fix: [what was changed]
Test: [test name]
Verified: Test fails on broken code, passes on fixed code
Regression check: Full suite passes
```

### Update Mutation Report (If Applicable)

If the fix affects a module covered by mutation testing, note that the module needs re-verification in the next mutation testing run.

### Evaluate Pattern Creation

**After every bug fix, ask: "Could this class of bug exist elsewhere or recur?"** If yes, create or update a pattern in `audit/PATTERN.md`.

A finding becomes a pattern when it represents a **class** of bug, not just a single instance. Decompose the finding:

1. **What is the class?** Not "TTS output_file missing `_check_path()`" but "tool parameter used as file path without `_check_path()` validation."
2. **What check would catch future instances?** A grep, a code review step, a specific question to ask during a specific audit stage.
3. **Which stages should run the check?** Map to the Pattern Index in PATTERN.md.

If `audit/PATTERN.md` doesn't exist yet, create it using the template structure (see existing PATTERN.md for format). If it exists, append the new pattern with:
- **P-NNN** (next sequential number)
- **Origin:** which cycle, which stage, what was found
- **Class:** the generalized bug class
- **Check:** concrete commands and questions
- **Stage index:** which stages should run this check

Also update the Pattern Index table at the bottom of PATTERN.md and add a Changelog entry.

Not every bug needs a pattern. Single-instance bugs with no generalization (e.g., a typo in one config file) don't need patterns. The test is: "If I grep the codebase for this class of issue, could there be other instances?" If yes → pattern. If no → skip.

---

## Output

Each bug fix produces:

```markdown
## Bug Fix Record

**Date:** [date]
**Source audit:** [which stage found it]
**Bug:** [one-sentence description]
**Severity:** [CRITICAL / HIGH / MEDIUM / LOW]

### Root Cause
[What was actually wrong and why]

### Scope
[Other instances of the same root cause, if any]

### Fix
File: [file changed]
Change: [what was changed]
Lines: [before/after or diff reference]

### Verification
Test: [test name]
Test fails on broken code: YES
Test passes on fixed code: YES
Full suite: [count] passed, 0 failed

### Commit
[commit hash and message]
```

---

## Rules

1. **One bug, one fix.** Don't bundle fixes. Don't refactor. Don't improve.
2. **Root cause, not symptom.** Trace to the actual cause before fixing.
3. **Every fix gets a test.** No exceptions (except doc fixes).
4. **Test fails on old code.** If you can't prove the test catches the bug, the test is worthless.
5. **Full regression after every fix.** No shortcuts.
6. **Check scope.** Same root cause might exist elsewhere.
7. **90% confidence before fixing.** If unsure, investigate more.
8. **Document in commit message.** Root cause + which audit found it.
9. **Return to triggering audit.** Re-run from the beginning of the stage.
10. **If the fix is bigger than expected (architectural change, multi-file refactor), flag for Nicolas.** Don't make architectural decisions autonomously.
