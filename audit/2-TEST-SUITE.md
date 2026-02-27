# 2 — Test Suite Audit

**What:** Run the full test suite, verify all tests pass, assess test health, and identify problems that could silently undermine test reliability. This is more than `pytest -q`. It's a health check on the test infrastructure itself.

**Why:** A passing test suite is the baseline assumption for every other audit stage. If tests are failing, mutation testing wastes hours killing mutants against broken tests. If tests are polluted (pass together but fail in isolation, or vice versa), results are unreliable. If test files exist but aren't collected, code is unprotected without anyone knowing.

This audit catches: broken tests, uncollected test files, test pollution, fixture issues, slow tests that indicate real problems, deprecation warnings from dependencies, and test suite structural problems.

**When to run:** Before every other audit stage, after any code change, or standalone as a quick health check.

---

## How to Think

You are checking the test infrastructure, not just running tests. A test suite that reports "738 passed" might still be broken if:

- 5 test files aren't being collected because of import errors
- Tests pass in batch but fail when run individually (dependency on execution order)
- Tests pass individually but fail in batch (fixture pollution)
- Tests pass but emit warnings that indicate upcoming breakage
- Tests take 10x longer than expected (possible real I/O instead of mocks)
- Test count dropped from last run and nobody noticed

Every finding must be reported. Don't rationalize warnings or skip slow tests. If something looks wrong, investigate it.

**Never explain away a failure.** A test that fails is telling you something. Categorizing it as "flaky" or "environment issue" without investigation is how real bugs survive. The `expanduser()` bug lived for months because tests mocked the broken path — the test passed, so nobody looked. Before categorizing any failure as anything other than "source bug," prove it. Run the test 5 times. Check the fixture. Check the mock. Read the source. If you can't prove it's not a source bug, treat it as one.

**Confidence gate:** Before declaring the suite healthy, reach 90% confidence that:
1. Every test file is being collected
2. Tests pass in isolation, not just in batch
3. No warnings indicate real problems
4. Test count matches expectations

---

## Pattern Checks

**Before starting Phase 1, run all pattern checks applicable to this stage.** Read `audit/PATTERN.md` for full context on each pattern. Report results in the stage report under a "Pattern Checks" section.

If `audit/PATTERN.md` does not exist (first audit cycle), skip this section.

### P-005: Shadowed test classes (verify count impact)
If Stage 1 found and fixed duplicate class/function names, verify the test count increased. A recovered shadowed test that doesn't change the count means the fix didn't work.

### P-006: Dead data pipeline (fixture check)
During Phase 1 inventory, note any test files that create pre-populated fixtures (test databases, pre-written files). For each, ask: "In production, what process creates this data?" If the answer is "nothing" or "a process that was removed," flag for Stage 5 (Dependency Chain) verification. If no round-trip test exists that covers both the write and read path, note as a finding.

### P-013: None-defaulted dependency hides untested code branch
During Phase 1 inventory and Phase 3 health checks, scan for test fixtures that pass `None` for dependency parameters:
```bash
grep -rn "=None\|= None" tests/test_*.py | grep -v "__pycache__"
```
For each, ask: "Does this `None` cause an entire code branch to be skipped in the function under test?" If the source has `if param is not None: <significant logic>` and the test passes `None`, that logic has zero coverage. Flag for mock coverage in Stage 3 (Mutation Testing).

---

## Phase 1: Discovery — Inventory the Test Suite

**Why:** Know what exists before checking if it works. Find orphaned tests, missing test files, and structural issues.

### Find All Test Files

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
[ -f lucyd.py ] || { echo "ERROR: Not in project root."; exit 1; }
source .venv/bin/activate

# All test files
find tests/ -name "*.py" -not -name "__pycache__" | sort

# Count
find tests/ -name "*.py" -not -name "__pycache__" | wc -l

# All production modules
find . -name "*.py" -not -path "./tests/*" -not -path "./.venv/*" \
    -not -path "./mutants/*" -not -path "./__pycache__/*" | sort
```

### Check Test Coverage Map

The test file naming convention is: `test_` + module name, flat in `tests/`. Subdirectory paths are dropped. Known exceptions:
- `channels/telegram.py` → `test_telegram_channel.py` (disambiguated with `_channel`)
- `lucyd.py` → split across `test_daemon_helpers.py`, `test_daemon_integration.py`, `test_orchestrator.py`, `test_monitor.py`
- Small modules may share a batch file (e.g., `test_zero_kill_modules.py` for status, memory_tools, skills_tool)
- `bin/lucyd-send` → `test_lucyd_send.py`

Verify by checking which production modules have corresponding test files:

```bash
for f in $(find . -name "*.py" -not -path "./tests/*" -not -path "./.venv/*" \
    -not -path "./mutants/*" -not -path "./__pycache__/*" -not -name "__init__.py"); do
    module=$(basename "$f" .py)
    # Check for exact match or partial match (disambiguated or batch files)
    matches=$(find tests/ -name "test_*${module}*.py" 2>/dev/null | head -5)
    if [ -n "$matches" ]; then
        echo "✓ $f → $matches"
    else
        # Check batch files that might cover this module
        batch=$(grep -rl "from.*${module}\|import.*${module}" tests/test_*.py 2>/dev/null | head -5)
        if [ -n "$batch" ]; then
            echo "~ $f → covered by $batch"
        else
            echo "✗ $f → NO TEST FILE"
        fi
    fi
done
```

Record any production modules without test files. These are blind spots — code that has zero test coverage. Not necessarily a problem (some modules are thin wrappers), but it must be documented.

### Check Test Collection

```bash
# What pytest actually collects
python -m pytest tests/ --collect-only -q 2>&1 | tail -5

# Check for collection errors (import failures, syntax errors)
python -m pytest tests/ --collect-only 2>&1 | grep -i "error\|ERROR\|warning\|WARNING"
```

**Critical:** If the collection count is lower than the test file count suggests, some tests aren't being collected. This is a silent failure — tests exist but don't run. Investigate immediately:

```bash
# Find files with zero collected tests
for f in tests/test_*.py; do
    count=$(python -m pytest "$f" --collect-only -q 2>&1 | tail -1 | grep -oP '\d+')
    echo "$f: $count tests"
done
```

### Record Baseline

```markdown
## Test Suite Inventory
Test files: [count]
Tests collected: [count]
Production modules: [count]
Production modules with test files: [count]
Production modules WITHOUT test files: [list]
Collection errors: [count, details]
```

**Confidence check:** Did pytest collect the same number of tests you expected? If the last known count was 820 and now it's 815, where did 5 tests go? Investigate before proceeding.

---

## Phase 2: Run Full Suite

**Why:** Establish the baseline. All tests must pass before any other analysis.

### Standard Run

```bash
# Full suite with verbose output
python -m pytest tests/ -v --tb=short 2>&1 | tee /tmp/test-suite-full.txt

# Check result
tail -5 /tmp/test-suite-full.txt
```

**If all pass:** Record count and timing. Proceed to Phase 3.

**If any fail:** Stop. This is a blocking issue. For each failure:

```markdown
| Test | Error | Category |
|------|-------|----------|
| test_file.py::test_name | AssertionError: expected X got Y | Broken test / Source bug |
```

Categorize each failure:
- **Source bug:** Production code changed and test correctly caught it. Fix the source. Use audit/8-BUG-FIX-WORKFLOW.md.
- **Broken test:** Test is wrong (outdated assertion, environment dependency). Fix the test.
- **Flaky test:** Passes sometimes, fails sometimes. Mark as flaky, investigate timing/ordering issues.
- **Environment issue:** Missing dependency, wrong Python version, file not found. Fix the environment.

**Do not proceed to Phase 3 until all tests pass.** Re-run after each fix:
```bash
python -m pytest tests/ -v --tb=short
```

---

## Phase 3: Test Health Checks

**Why:** "All passing" is necessary but not sufficient. These checks catch problems that a simple pass/fail run hides.

### 3a: Warnings Check

```bash
# Run with warnings visible
python -m pytest tests/ -v --tb=short -W all 2>&1 | grep -i "warning\|Warning\|WARN" | sort | uniq -c | sort -rn
```

Categorize each warning:

| Category | Action |
|----------|--------|
| DeprecationWarning from YOUR code | Fix before it breaks |
| DeprecationWarning from dependency | Note the dependency and version. Check if update is available. |
| RuntimeWarning | Investigate — often indicates real problems (overflow, coroutine not awaited) |
| PytestUnraisedExceptionWarning | Test is swallowing an exception. Investigate. |
| UserWarning | Usually informational. Suppress if noisy. |

**Critical warnings to never ignore:**
- `RuntimeWarning: coroutine '...' was never awaited` — async bug, test is broken
- `DeprecationWarning` from stdlib — Python version upgrade may break this
- `ResourceWarning: unclosed file/socket` — resource leak, may cause flaky tests

### 3b: Isolation Check

**Why:** Tests that depend on execution order are unreliable. A test that passes only because a previous test set up some state is a bug — it will fail in CI, in mutation testing, and when run individually.

```bash
# Run each test file individually
for f in tests/test_*.py; do
    echo "--- $f ---"
    python -m pytest "$f" -q --tb=line 2>&1 | tail -2
done
```

If any file fails in isolation but passed in the full run, that's a dependency on execution order. Record it:

```markdown
## Isolation Failures
| File | Error | Likely Dependency |
|------|-------|-------------------|
| test_foo.py | NameError: 'shared_state' | Relies on test_bar.py running first |
```

### 3c: Reverse Order Check

```bash
# Run in reverse order to catch order-dependent tests
python -m pytest tests/ -v --tb=short -p no:randomly 2>&1 | tail -5
# If pytest-randomly is installed:
# python -m pytest tests/ -v --tb=short -p randomly --randomly-seed=12345
```

If tests fail in reverse order but pass in normal order, there's execution order dependency.

### 3d: Timing Check

```bash
# Find slow tests (anything over 2 seconds is suspicious)
python -m pytest tests/ -v --tb=short --durations=20 2>&1 | grep -A 25 "slowest"
```

Slow tests often indicate:
- Real network calls instead of mocks (should be < 100ms)
- Real file I/O instead of tmp_path
- Sleep calls in test code
- Inefficient fixtures that recreate expensive objects

**Threshold:** Individual tests should complete in under 1 second. Test files should complete in under 5 seconds. The full suite should complete in under 60 seconds for ~1500 tests. Anything dramatically slower indicates a problem.

### 3e: Fixture Health

```bash
# Check for unused fixtures
python -m pytest tests/ --fixtures -q 2>&1 | head -50

# Check conftest.py for issues
cat tests/conftest.py
```

Look for:
- Fixtures that aren't used by any test (dead code in test infrastructure)
- Fixtures with broad scope (`session` or `module`) that could cause pollution
- Fixtures that do real I/O without cleanup

---

## Phase 4: Test Quality Indicators

**Why:** Beyond pass/fail, these metrics indicate the overall health of the test suite.

### Test-to-Production Ratio

```bash
# Production lines
find . -name "*.py" -not -path "./tests/*" -not -path "./.venv/*" \
    -not -path "./mutants/*" -not -path "./__pycache__/*" -exec cat {} + | wc -l

# Test lines
find tests/ -name "*.py" -exec cat {} + | wc -l
```

**Healthy ratio:** 1.5:1 to 3:1 test-to-production. Below 1:1 suggests under-testing. Above 4:1 suggests over-engineering or copy-paste tests.

### Test Naming Consistency

```bash
# Check test naming patterns
grep -rn "def test_" tests/ | sed 's/.*def //' | sed 's/(.*$//' | sort | head -30
# Look for inconsistencies: test_thing vs test_thing_works vs testThing
```

Inconsistent naming makes tests harder to find and maintain. Not a blocker, but note it.

### Assert Density

```bash
# Average asserts per test (rough indicator)
total_tests=$(python -m pytest tests/ --collect-only -q 2>&1 | tail -1 | grep -oP '\d+')
total_asserts=$(grep -rn "assert " tests/ | grep -v "__pycache__" | wc -l)
echo "Tests: $total_tests, Asserts: $total_asserts, Ratio: $(echo "scale=1; $total_asserts / $total_tests" | bc)"
```

**Healthy ratio:** 1.5+ asserts per test. Below 1.0 suggests tests that call functions but don't verify outcomes (the exact disease mutation testing caught).

---

## Phase 5: Report

Write the report to `audit/reports/2-test-suite-report.md`:

```markdown
# Test Suite Report

**Date:** [date]
**Duration:** [time]
**Python version:** [version]
**Pytest version:** [version]
**EXIT STATUS:** PASS / FAIL

## Inventory
| Metric | Value |
|--------|-------|
| Test files | |
| Tests collected | |
| Tests passed | |
| Tests failed | |
| Production modules | |
| Modules with tests | |
| Modules WITHOUT tests | |

## Suite Run
Total time: [seconds]
All passed: [yes/no]
Failures: [list if any]

## Health Checks

### Warnings
| Warning | Count | Category | Action |
|---------|-------|----------|--------|

### Isolation
All files pass in isolation: [yes/no]
Failures: [list if any]

### Timing
Slowest tests: [top 5 with times]
Any over 2s threshold: [yes/no, details]
Total suite time: [seconds]

### Fixture Health
Unused fixtures: [list]
Broad-scope fixtures: [list]
Issues found: [details]

## Quality Indicators
| Metric | Value | Healthy Range |
|--------|-------|---------------|
| Test-to-production ratio | | 1.5:1 — 3:1 |
| Assert density | | > 1.5 |
| Test naming consistency | | Consistent / Mixed |

## Modules Without Test Files
[List each with assessment: needs tests / thin wrapper / covered by integration]

## Fixes Applied
[Any test fixes made during this audit]

## Confidence
[Overall confidence in suite health: X%]
[Any areas of uncertainty]
```

### Exit Status Criteria

- **PASS:** All tests pass. No collection errors. No isolation failures. No critical warnings. Test count matches or exceeds previous known count.
- **FAIL:** Any test fails. Collection errors exist. Isolation failures found. Critical warnings (unawaited coroutine, resource leaks) present. Blocks proceeding to Stage 3.
