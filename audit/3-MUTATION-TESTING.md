# 3 — Mutation Testing Audit

**What:** Verify that component tests actually catch bugs by mutating the source code and checking if tests fail. For every module in the codebase, systematically confirm that security checks, data transformations, and control flow decisions have tests that break when those checks are removed, inverted, or changed.

**Why:** We had 502 tests. All passing. Green across the board. Every security gate in the codebase was unverified.

Mutation testing proved it. mutmut changes one line of source code at a time — flips a condition, swaps an operator, removes a call — and runs the test suite against each change. If all tests still pass after a security check is removed, no test actually verifies that check. It's decorative.

The original results were catastrophic. `tools/agents.py` had a deny-list preventing sub-agent privilege escalation: 116 mutants, 0 killed — the tests had reimplemented the filtering logic inline instead of calling the real function. `tools/web.py` had SSRF protection with 324 mutants and 5.6% killed. The redirect handler preventing `302 → http://169.254.169.254/` had 15 mutants, zero killed.

This manual is the methodology that fixed it. Built across 5 remediation cycles. Every rule exists because we broke it and paid for it.

**When to run:** After writing tests for new modules, after changing security-critical code, during full audit (Stage 3), or standalone when test quality is in question.

**Scope:** Component modules — tools, channels, providers, session, agentic loop. For orchestrator code that wires components together (like `lucyd.py`), use `4-ORCHESTRATOR-TESTING.md` instead.

---

## How to Think

### Five Checkpoints (Before Every Test)

```
1. WHAT am I testing? Name the attack or failure in one sentence.
2. WHAT function am I calling? Read the source. Exact signature, return type.
3. HOW will I verify? "If I remove X on line Y, this assertion fails because..."
4. Am I calling REAL code or reimplementing it? (Reimplemented = 0% kill rate)
5. Could this pass for the wrong reason? Mock too aggressive? Weak assertion?
```

If any checkpoint answer is "I'm not sure," STOP. Read the source again. Reach 90% confidence before writing the test.

### Verification Loop (Non-Negotiable for Security Tests)

```
1. Run test → PASSES
2. Apply mutation manually (comment out / invert the check)
3. Run test → must FAIL
   If PASSES → test is lying. Investigate. Rewrite.
4. Revert: git checkout <file>
5. Run full suite → all pass, no regressions
```

### Confidence Gate

Before executing any step, assess your confidence:
- **90%+ confident** → Execute, then immediately review the result
- **70-89% confident** → State what you're unsure about. Investigate. Raise to 90% before executing.
- **Below 70%** → STOP. You're guessing. Read more source code, re-diagnose, or flag for Nicolas.

After executing any step, immediately review:
- Did the result match expectations?
- If not, WHY? Diagnose before proceeding.
- Never explain away unexpected results. Investigate them.

### The Explaining-Away Trap

This is the single most dangerous failure mode in mutation testing. It works like this:

1. A security function doesn't handle an input class (e.g., octal-encoded IP addresses)
2. You write a test: `assert _is_private_ip("0177.0.0.1") is False`
3. The test passes. You document it as "by design — defense is at DNS layer."
4. You've just documented a vulnerability and called it a feature.

The correct response was: fix `_is_private_ip` to handle octal encoding, then write a test that asserts `True`. This happened during this project's first security audit. The 42-minute audit declared it "by design." A human spot-check caught it.

**The rule:** When a security function fails to catch something, the function is wrong — not the test expectation. Fix the function. Write tests that demand correct behavior. Never write tests that accept broken behavior and call it documented.

This applies to every categorization decision in this audit:
- **"Equivalent mutant"** on a security function → Prove the mutation is unreachable by adversarial input. If you can't prove it, it's not equivalent.
- **"Cosmetic"** on a security function → There are no cosmetic mutations on security functions. A log message change in a security boundary might leak information. Prove it doesn't.
- **"By design"** → Prove the design is correct. "The function doesn't handle this input class" is not a design decision, it's a bug.

---

## Anti-Drift Rules

These are specific failure patterns we've seen. You will be tempted by all of them.

### Why Each Rule Exists

**Rule 1: Assert with `==`, not `in`.**
WHY: `tools/messaging.py` had 11 survivors. Every one died when we changed `"Error" in result` to exact equality. mutmut wraps strings: `"Error"` → `"XXErrorXX"`. Substring match still passes. Exact match catches it.
HOW: Use `==` for exact values. Use `in` only when substring matching is genuinely the correct behavior.
WHAT: `assert result == "Expected error message"` not `assert "error" in result`.

**Rule 2: `assert_called_once_with(args)`, not `assert_called_once()`.**
WHY: `channels/telegram.py` had `send_typing` tests that verified the API was called but not with what arguments. mutmut changed `"sendChatAction"` to `"XXsendChatActionXX"`. Test still passed.
HOW: Always check the exact arguments passed to mocked functions.
WHAT: `mock_api.assert_called_once_with("sendMessage", {"chat_id": 123, "text": "hello"})`.
**Scope note:** This rule applies to component tests where you're verifying the function calls external APIs correctly. For orchestrator tests where a `None`-returning method's side effects ARE the behavior (like `_process_message` calling `channel.send`), asserting on mock interactions is correct — see `4-ORCHESTRATOR-TESTING.md`.

**Rule 3: AsyncMock for async, never MagicMock.**
WHY: Two `TestResolveIntegration` tests were broken. `MagicMock` on `run_agentic_loop` (async function) produced an object that can't be properly awaited. Tests passed because the mock was consumed before the await. They tested nothing.
HOW: If the function is `async def`, use `AsyncMock`. If you `await` the result, use `AsyncMock`.
WHAT: `from unittest.mock import AsyncMock` for all async function mocks.

**Rule 4: Don't reimplement logic in tests.**
WHY: `tools/agents.py` scored 0%. Tests rebuilt the deny-list filtering inline instead of calling `tool_sessions_spawn()`. If someone removes the deny-list from source, this test still passes — it tests the test's copy, not the source.
HOW: Call the REAL function. Mock only EXTERNAL dependencies (network, API, filesystem).
WHAT: Test by calling `tool_sessions_spawn()` with a mocked agentic loop, then inspect what tools were passed.

**Rule 5: monkeypatch, not raw os.environ.**
WHY: If a test crashes between `os.environ["KEY"] = "val"` and `del os.environ["KEY"]`, the env var leaks to every subsequent test. `monkeypatch` auto-reverts even on crash.
HOW: Always use pytest's `monkeypatch` fixture for environment variables and module globals.
WHAT:
```python
def test_something(monkeypatch):
    monkeypatch.setenv("KEY", "val")
```

**Rule 6: Prefix tests must use suffix-free variable names.**
WHY: 3 lying tests caught during remediation. `LUCYD_ANTHROPIC_KEY` matches both the `LUCYD_` prefix filter AND the `_KEY` suffix filter. Removing the prefix check didn't break the test because the suffix check still caught it.
HOW: Use variable names that match ONLY the filter being tested.
WHAT: `LUCYD_CUSTOM_SETTING` (matches prefix only, no `_KEY` suffix).

**Rule 7: `pythonpath = ["."]`, never `[".", ".."]`.**
WHY: With `..` in pythonpath, Python imports the original unmutated source from the parent directory instead of mutmut's mutated copy in `mutants/`. All mutation testing silently becomes a no-op.
HOW: Set `pythonpath = ["."]` in `pyproject.toml` under `[tool.pytest.ini_options]`.
WHAT: Check this EVERY TIME before running mutmut.

**Rule 8: Use directory targets for package modules.**
WHY: `paths_to_mutate = ["channels/telegram.py"]` (single file) uses `copyfile` which skips `__init__.py`. The mutant sandbox has no package, so Python imports unmutated source from parent. `["channels/"]` (directory) uses `copytree` which includes `__init__.py`.
HOW: Always use directory targets when the module lives inside a Python package.
WHAT: `paths_to_mutate = ["channels/"]` not `["channels/telegram.py"]`.

**Rule 9: Scope `tests_dir` to avoid sandbox import failures.**
WHY: With `tests_dir = ["tests/"]`, mutmut runs ALL test files. Tests for other modules fail because their imports aren't in the mutant sandbox. These failures look like killed mutants but are infrastructure errors.
HOW: Scope to just the relevant test file.
WHAT: `tests_dir = ["tests/test_telegram_channel.py"]`.

**Rule 10: Read tests before debugging tooling.**
WHY: 3 modules showed zero improvement after 19 new tests. We spent time investigating mutmut's trampoline. Then we read the tests — root causes were visible in source: substring assertions, mocks on functions under test, exercised-but-not-asserted code paths. 10 minutes of reading saved hours.
HOW: When mutmut results are unexpected, READ THE TESTS FIRST. Not mutmut output. The tests.

**Rule 11: Self-reported verification is not verification.**
WHY: Remediation log claimed "all 12 security mutations verified killed." mutmut showed 3 modules unchanged. The manual verification was done incorrectly. mutmut is the judge.
HOW: Always re-verify with mutmut. Never trust self-reported results.

**Rule 12: Document equivalents, don't chase them.**
WHY: `"utf-8"` ↔ `"UTF-8"` is unkillable because the behavior is identical. Writing a test that differentiates them is waste. Writing garbage tests to inflate kill rates is worse.
HOW: Identify equivalent mutants, document them with justification, move on.

**Rule 13: Don't run mutmut on orchestrators.**
WHY: `lucyd.py` produced 1121 untestable mutants, 497 fork deadlocks, 76 ambiguous kills. The real bug (hardcoded `expanduser()` path) was found by manual testing, not mutmut. Orchestrators need extraction + contract testing.
HOW: Use `4-ORCHESTRATOR-TESTING.md` for orchestrator files.

**Rule 14: None-defaulted dependencies hide untested branches.**
WHY: `recall()` accepted `memory_interface=None` in all test fixtures. The entire vector search branch (~50 mutants) was never exercised. Decay formula, sort order, and `top_k` reduction were all unverified — invisible because the `if memory_interface is not None` guard short-circuited the entire path.
HOW: When a function parameter defaults to `None` and guards a code branch with `if param is not None`, passing `None` in tests skips that branch entirely. Provide a mock instead of `None`.
WHAT: `memory_interface = MagicMock(); memory_interface.search = AsyncMock(return_value=[...])` — then verify the branch exercises the decay, sort, and truncation logic.

---

## Pattern Checks

**Before starting Phase 1, run all pattern checks applicable to this stage.** Read `audit/PATTERN.md` for full context on each pattern. Report results in the stage report under a "Pattern Checks" section.

If `audit/PATTERN.md` does not exist (first audit cycle), skip this section.

### P-004: Test fixture iteration-order blindness
When reviewing mutation survivors in filter/iteration functions, ask: "Would this mutant survive if the matching item were at the beginning or middle of the collection instead of the end?" CPython dicts maintain insertion order — test data appended via `monkeypatch.setenv()` is always last.

```bash
grep -rn 'monkeypatch.setenv\|os.environ' tests/ --include='*.py'
```
For tests that add environment variables to test filter functions: verify that both matching and non-matching entries exist, and that non-matching entries appear AFTER matching ones in insertion order. If all test data is appended at the end, `continue→break` mutations are invisible.

### P-013: None-defaulted dependency hides untested code branch
When reviewing mutation survivors, check if surviving mutants cluster in a code path guarded by an `if`-not-`None` check on a function parameter:

```bash
# In the source file under test, find None guards:
grep -n "if.*is not None\|if.*is None" <source_file>
# In the test file, find None-defaulted fixtures:
grep -n "=None\|= None" tests/test_<module>.py
```

If survivors cluster behind a dependency guard and tests pass `None` for that dependency, the test fixtures need a proper mock, not `None`. This caught ~50 hidden survivors in `recall()`'s vector search path.

---

## Phase 1: Discovery — Scope the Modules

**Why:** Discover all testable component modules from source. Don't hardcode module lists. If a module was added since the last audit, find it.

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
[ -f lucyd.py ] || { echo "ERROR: Not in project root."; exit 1; }
source .venv/bin/activate

# Find all component modules (exclude orchestrator, tests, venv, mutants)
find tools/ channels/ -name "*.py" -not -name "__init__.py" -not -path "*__pycache__*" | sort

# Also check root-level modules that aren't orchestrators
# agentic.py, session.py, etc. — testable components
ls *.py | grep -v lucyd.py  # lucyd.py is orchestrator, skip it
```

For each module found, check:
```bash
# Does a test file exist?
# Convention: test_ + module name, flat in tests/
# Exceptions: channels use _channel suffix, lucyd.py splits across 4 files,
# small modules may share batch files (test_zero_kill_modules.py covers status, memory_tools, skills_tool)
for module in $(find tools/ channels/ -name "*.py" -not -name "__init__.py" -not -path "*__pycache__*"); do
    base=$(basename "$module" .py)
    matches=$(find tests/ -name "test_*${base}*.py" 2>/dev/null)
    if [ -n "$matches" ]; then
        echo "✓ $module → $matches"
    else
        batch=$(grep -rl "from.*${base}\|import.*${base}" tests/test_*.py 2>/dev/null)
        if [ -n "$batch" ]; then
            echo "~ $module → covered by $batch"
        else
            echo "✗ $module → NO TEST FILE"
        fi
    fi
done
```

### Discover Tools (Authoritative Method)

Tools are registered via module-level `TOOLS` lists — dicts with `name`, `function`, `input_schema`. The `tool_` function prefix is cosmetic. Registration is explicit, not discovered by naming. The authoritative inventory comes from the TOOLS lists:

```bash
# Find all tool registrations
grep -rn "^TOOLS\s*=" tools/*.py | grep -v test | grep -v __pycache__
# Count individual tool entries (each has a "name" key)
grep -rn '"name":' tools/*.py | grep -v test | grep -v __pycache__
# Cross-reference with what the daemon loads
grep -rn "_init_tools\|tools_enabled" lucyd.py | grep -v test
```

The daemon's `_init_tools()` imports each module's TOOLS list, filters against `config.tools_enabled` from `lucyd.toml`, and registers survivors via `ToolRegistry.register_many()`. Static whitelist dispatch — no decorators, no auto-discovery.

### Classify Each Module

| Module | Type | Has Tests? | Security-Critical? | Priority |
|--------|------|------------|-------------------|----------|
| tools/shell.py | Component | Yes | YES — executes commands | 1 |
| tools/agents.py | Component | Yes | YES — sub-agent boundaries | 1 |
| tools/web.py | Component | Yes | YES — SSRF, redirects | 1 |
| tools/filesystem.py | Component | Yes | YES — path traversal | 1 |
| channels/http_api.py | Component | Yes | YES — auth, rate limits | 1 |
| channels/telegram.py | Component | Yes | Medium — input handling | 2 |
| ... | ... | ... | ... | ... |

**Priority 1:** Security-critical modules. Must achieve target kill rates.
**Priority 2:** Important modules with behavioral complexity.
**Priority 3:** Simple modules, utility functions.

**Confidence check:** Have you found ALL component modules? Check for files in unexpected locations. Is the classification correct — is each module actually a component (testable in isolation) and not an orchestrator?

---

## Phase 2: Check Existing State

**Why:** Know where you stand before doing work. Some modules may already be at target kill rates from previous audits.

### Check for Existing Mutation Reports

```bash
ls audit/reports/MUTATION-REPORT-*.md 2>/dev/null
```

Read each existing report. Record the last known state:

```markdown
## Existing Mutation State
| Module | Last Kill Rate | Last Date | Security Status |
|--------|---------------|-----------|-----------------|
```

### For Modules Without Reports: Run Initial mutmut

For each untested module, run mutmut to establish baseline:

```bash
# Configure pyproject.toml for the target module
# paths_to_mutate = ["tools/"]  # directory, not file
# tests_dir = ["tests/test_shell.py"]  # scoped to relevant test
# pythonpath = ["."]  # CRITICAL

rm -rf mutants/ .mutmut-cache/
mutmut run
mutmut results
```

Record the baseline per module.

**Important:** Restore `pyproject.toml` after each module run if you change `paths_to_mutate` and `tests_dir`.

---

## Phase 3: Remediation — Writing and Fixing Tests

**Why:** Survivors need tests that kill them. But writing more tests on top of broken tests produces more broken tests. Diagnose first, then fix.

### For Each Module (In Priority Order)

#### Step 3a: Read Before Write

Read completely:
- The source module you're testing
- `tests/conftest.py` — shared fixtures
- The existing test file — understand conventions

For every function you'll test, note:
- Exact signature (params, types, defaults)
- Return type (string? dict? None? raises?)
- Error pattern (raise ValueError? return error string? return dict with error key?)
- Import path

#### Step 3b: Categorize Survivors

For the current module's mutmut results, categorize every survivor:

| Category | Description | Action |
|----------|-------------|--------|
| Security | Auth, validation, filtering, boundaries | Must kill. Verification loop required. |
| Behavioral | Return values, control flow, data transformation | Should kill. Worth the effort. |
| Cosmetic | Log messages, format strings, display text | Skip. Document as cosmetic. |
| Equivalent | Mutation doesn't change behavior | Skip. Document with justification. |
| No coverage | Zero tests exercise this function | Write tests from scratch. |

**CRITICAL — categorization of security function survivors:**

Before categorizing ANY survivor in a security function as "Cosmetic" or "Equivalent," answer this question: **"If an attacker knew this mutation survived, could they exploit it?"**

- If YES → It's not cosmetic or equivalent. Fix it.
- If MAYBE → It's not cosmetic or equivalent. Fix it.
- If NO, and you can prove why → Document the proof, then categorize.

"The function doesn't handle this input format" is NEVER equivalent — it's a bug. `_is_private_ip("0177.0.0.1") → False` is not "by design," it's an SSRF bypass via octal encoding. The first audit of this project categorized it as "defense is at DNS layer." The second audit fixed the function.

The explaining-away instinct is strongest on categorization decisions. This is where you rationalize not writing a fix because writing a justification is easier. Fight it.

**Common equivalent mutants (don't chase):**
- `"utf-8"` ↔ `"UTF-8"` (case-insensitive encoding)
- Default params matching constructor defaults
- `open(f, "r")` → `open(f, "")` (default mode is "r")
- `.replace(x, y)` count param when exactly 1 match exists
- `mkdir(parents=True)` when parent always exists in tests
- `.get(key, {})` ↔ `.get(key, None)` when both cause downstream error
- `httpx.Timeout` values (operational tuning, mocked in tests)
- `@decorator` mutations (some decorators block mutmut instrumentation)

#### Step 3c: Write Tests

**Golden Rule:**
```
Write tests for real-world failures and attack scenarios.
The mutant dies as a side effect of testing real behavior.
test_attachment_outside_allowlist_blocked = good
test_line_29_not_endswith = garbage
```

**By survivor pattern — what to write:**

**String constant survivors** (`"endpoint"` → `"XXendpointXX"`):
Assert exact string values, not substrings.
```python
# KILLS string mutations
assert call_args[0][0] == "sendMessage"
# DOES NOT kill string mutations
assert "sendMessage" in call_url
```

**Operator survivors** (`<=` → `<`, `//` → `/`):
Add boundary tests at exactly the threshold, one above, one below.
```python
assert chunk_text("x" * 4000, limit=4000) == ["x" * 4000]  # exactly at limit
assert len(chunk_text("x" * 4001, limit=4000)) == 2          # one over
```

**Removed-call survivors** (function call deleted):
Assert on the OUTCOME of the call, not just that code after it runs.
```python
result = await tool_message(target="t", text="", attachments=["/etc/passwd"])
assert "error" in result.lower()
```

**Boolean/condition survivors** (`if x` → `if not x`):
Test both branches explicitly.
```python
assert parse_allowed_message(from_allowed_user) is not None
assert parse_allowed_message(from_unknown_user) is None
```

**Default parameter survivors** (`timeout=60` → `timeout=61`):
Usually equivalent. Document and skip unless security-relevant.

**Return value survivors** (`return x` → `return None`):
Assert on the actual return value.
```python
result = _extract_attachments(msg)
assert len(result) == 1
assert result[0]["file_id"] == "abc"
```

**No-coverage functions:**
Write tests from scratch. Focus on: what does it return on success? What does it do on failure? What are the edge cases?

#### Step 3d: Verify Security Tests

For every security-critical test, run the verification loop (non-negotiable):

```
1. Run test → PASSES
2. Manually break the security check in source
3. Run test → must FAIL
4. git checkout <file>
5. Full suite → all pass
```

**Save the output.** `python -m pytest test_file.py::test_name -x -v 2>&1 | tee /tmp/verify_<name>.txt`

If the test passes with the check removed, it's lying. Do not proceed. Fix it.

---

## Phase 4: Verification — mutmut Run

**Why:** Self-reported test quality means nothing. mutmut is the judge.

```bash
# Configure for target module
# paths_to_mutate = ["<directory>/"]  # DIRECTORY, not file
# tests_dir = ["tests/<test_file>.py"]  # SCOPED
# pythonpath = ["."]  # CRITICAL

rm -rf mutants/ .mutmut-cache/
mutmut run
mutmut results
```

### Decision Point Per Function

| New Score | Remaining Survivors | Action |
|-----------|-------------------|--------|
| 90%+ | Mostly cosmetic/equivalent | Done. Document survivors. |
| 70-90% | Mix of behavioral and cosmetic | Inspect survivors. Fix behavioral, document rest. |
| 50-70% | Significant behavioral survivors | Go to Phase 5 (Diagnosis). |
| <50% | Tests are likely still broken | Go to Phase 5 immediately. |
| No change | Zero improvement after new tests | Go to Phase 5 immediately. Something is wrong. |

---

## Phase 5: Diagnosis — Why Tests Don't Kill Mutants

**Why:** When tests fail to kill mutants despite appearing correct, the root cause is usually in the test, not the tooling. This phase finds the root cause.

**Start by reading the tests. Not mutmut output. The tests.**

### Diagnostic Protocol (One Mutant at a Time)

```
1. IDENTIFY — Pick one surviving mutant: mutmut show <id>
2. UNDERSTAND — What exactly did mutmut change? (the diff)
3. FIND — Which test SHOULD kill it?
4. TEST — Apply mutation manually, run the test. Does it fail?
5. DIAGNOSE — If it passes, WHY?
6. FIX — Fix based on diagnosis
7. VERIFY — Re-run mutmut
```

Do this for ONE mutant first. The root cause for one usually explains the others in the same function.

### Root Cause Categories

| Cat | Name | How to Identify | Fix |
|-----|------|-----------------|-----|
| A | Mock too aggressive | Test mocks the function under test | Mock externals only |
| B | Assertion too weak | `in` instead of `==`, no arg checking | Strengthen assertions |
| C | Wrong code path | Inputs don't reach mutated line | Add inputs for that branch |
| D | Import mismatch | `pythonpath` or single-file target issue | Fix config per Rules 7-9 |
| E | Test is fake | Reimplements logic, tests mock behavior | Rewrite from scratch |
| F | Equivalent mutant | Mutation doesn't change behavior | Document and skip |

### When Manual Verification Disagrees With mutmut

If your test fails against manual mutation but mutmut says it survived:

```bash
# Check what mutmut's sandbox looks like
ls mutants/
# Check which source file the test imports
python -c "import <module>; print(<module>.__file__)"
# Run from inside mutants/
cd mutants/ && python -m pytest ../tests/test_file.py::test_name -x -v
```

Usually: import path resolving to parent directory instead of mutants/.

---

## Phase 6: Iterate

Repeat Phases 3-5 for each module until:

**Security-critical functions:** Kill rate target met. All security mutants killed or documented as equivalent with justification.

**Non-security functions:** 70%+ with remaining survivors categorized. Cosmetic and equivalent survivors documented.

**A function is DONE when:**
- Security function: All security-relevant mutants killed. Cosmetic survivors documented.
- Non-security function: 70%+ with remaining survivors categorized and justified.
- No uncategorized survivors.

**Why 70% for non-security:** Pushing above 70% on non-security functions means chasing log-string mutations, default parameter changes, and encoding case variations that don't affect behavior. The Telegram channel hit 79.5% overall with 174 survivors — all cosmetic, equivalent, or operational. The effort to kill those would produce garbage tests. Spend the time on documented test gaps (message loop coverage, end-to-end compaction) before hunting cosmetic survivors.

**A module is DONE when:**
- All functions meet completion criteria.
- Module-level report written.
- No uncategorized survivors.

---

## Phase 7: Report

Write per-module reports to `audit/reports/MUTATION-REPORT-<module>.md` AND the audit report to `audit/reports/3-mutation-testing-report.md`.

### Per-Module Report Template

```markdown
# Mutation Report — <module>

**Date:** [date]
**Tests:** [count]
**Tool:** mutmut [version]

## Summary
| Metric | Before | After | Delta |
|--------|--------|-------|-------|

## Per-Function Breakdown
| Function | Kill | Surv | NoTest | Total | Score |

## Security-Critical Functions
| Function | Status | Detail |

## Equivalent Mutants
[List with justification]

## Remaining Survivors — Categorized
### Cosmetic (~N): [description]
### Equivalent (~N): [description]
### Behavioral (~N): [description and why not fixed]

## Fixes Applied
[Root causes, tests written, before/after per function]
```

### Audit Report Template

```markdown
# Mutation Testing Audit Report

**Date:** [date]
**Duration:** [time]
**EXIT STATUS:** PASS / PARTIAL / FAIL

## Scope
[Modules tested, modules skipped with reason]

## Results Summary

| Module | Tests | Kill Rate | Security Status |
|--------|-------|-----------|-----------------|

## Security Verification
| Security Function | Module | Status | Evidence |
|-------------------|--------|--------|----------|
| deny-list filtering | agents.py | KILLED 13/13 | |
| env filtering | shell.py | KILLED 7/8 | |
| ... | ... | ... | |

## New Tests Written
[Count per module, total]

## Root Causes Found
[Patterns across modules — same disease or different?]

## Equivalent Mutants Documented
[Summary count per module]

## Confidence
[Overall confidence in results: X%]
[Modules where confidence is lower and why]
```

### Exit Status Criteria

- **PASS:** All security-critical functions at target kill rates. All survivors categorized. All modules have reports.
- **PARTIAL:** Security functions verified. Some non-security modules at lower kill rates with documented plan. Acceptable for deployment.
- **FAIL:** Any security-critical function has unverified mutants. Any module has uncategorized survivors. Blocks proceeding to Stage 4.

---

## Completion Checklist

```
[ ] All component modules discovered and classified
[ ] Security-critical modules tested first
[ ] All security functions verified (verification loop passed)
[ ] mutmut run completed per module, results recorded
[ ] Every survivor categorized (security / behavioral / cosmetic / equivalent)
[ ] Equivalent mutants documented with justification
[ ] No lying tests (security tests verified against manual mutation)
[ ] Full test suite passes
[ ] Per-module reports saved to audit/reports/
[ ] Audit report saved to audit/reports/
[ ] pyproject.toml restored to default state
[ ] Committed with descriptive message
```

---

## Lessons Learned (Don't Repeat These)

1. **502 tests can be decorative.** Green output means nothing without mutation verification.
2. **`in` assertions are weak.** `"XXErrorXX"` contains `"Error"`.
3. **Reimplemented logic tests score 0%.** Testing a copy tests the copy.
4. **MagicMock on async = silent failure.** Always AsyncMock.
5. **Prefix tests matching suffixes lie.** Use suffix-free variable names.
6. **`pythonpath = [".", ".."]` defeats mutmut.** Use `["."]` only.
7. **`assert_called_once()` without args doesn't kill string mutations.**
8. **3 modules zero improvement after 19 new tests.** Read the tests first.
9. **Self-reported verification is not verification.** mutmut is the judge.
10. **Equivalent mutants are real.** Document them, don't chase them.
11. **Single-file targets skip `__init__.py`.** Use directory targets.
12. **Unscoped `tests_dir` causes false kills.** Scope to relevant test file.
13. **Don't run mutmut on orchestrators.** Use `4-ORCHESTRATOR-TESTING.md`.
14. **Never write a test that asserts broken behavior is correct.** `assert _is_private_ip("0177.0.0.1") is False` doesn't test the function — it documents a vulnerability and calls it a feature. First audit declared it "by design." Human spot-check caught it. Fix the function, then assert correct behavior.
15. **None-defaulted dependencies hide entire branches.** `recall(memory_interface=None)` skipped ~50 lines of vector search logic. If a parameter guards a code branch and tests pass `None`, those lines have zero coverage and mutants survive silently.
