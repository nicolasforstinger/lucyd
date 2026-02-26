# 1 — Static Analysis Audit

**What:** Scan all production source code for bugs, security anti-patterns, type errors, and dead code that tests cannot catch. Fix what's fixable, document what's intentional.

**Why:** Tests verify behavior — they confirm what the code does. Static analysis verifies structure — it confirms the code isn't doing things it shouldn't. A test can't catch an unused import, an unreachable branch, a shadowed variable name, or a `subprocess.call(shell=True)` hidden in an error path that tests never exercise. These are bugs waiting to happen. Static analysis finds them in seconds.

For Lucyd specifically: Lucy processes external data and executes shell commands. A single `eval()` or unguarded `subprocess` call that tests don't cover is a remote code execution vulnerability. Static analysis scans every line, not just the lines tests exercise.

**When to run:** After any code changes, before mutation testing (Stage 3 of full audit), or standalone when you suspect code quality issues.

---

## How to Think

You are scanning for defects that are invisible to tests. The code may have 100% test pass rate and still contain:

- **Dead code** that nobody maintains but attackers could reach
- **Type mismatches** at boundaries between modules (function expects `str`, caller passes `None`)
- **Security anti-patterns** that are safe today but become vulnerabilities when the code around them changes
- **Shadowed variables** where an inner scope redefines a name from outer scope, causing subtle bugs
- **Missing error handling** where exceptions propagate to unexpected places

Every finding must be categorized and justified. Not everything flagged is a bug — some findings are intentional tradeoffs. The job is to distinguish real bugs from acceptable code and document both.

**Never explain away a finding.** When a security scanner flags something, the instinct is to justify why it's fine. Resist that. The correct sequence is: understand the finding → verify it in source → determine if an attacker could exploit it → fix it or prove it's unreachable. "It's fine because we trust the input" is not proof — trace the input to its source and verify that trust is warranted. Categorizing a finding as INTENTIONAL requires the same confidence as categorizing it as SECURITY — you're just as certain it's safe as you would be certain it's dangerous.

**Confidence gate:** Before fixing any finding, reach 90% confidence that:
1. The finding is a real issue (not a false positive)
2. Your fix doesn't change behavior
3. Tests will still pass after the fix

If confidence is below 90%, flag the finding for Nicolas's review instead of fixing it.

---

## Prerequisites

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
[ -f lucyd.py ] || { echo "ERROR: Not in project root."; exit 1; }
source .venv/bin/activate
```

### Install Tools

```bash
# ruff — fast Python linter and formatter
ruff --version 2>/dev/null || pip install ruff --break-system-packages

# mypy — static type checker (optional but valuable)
mypy --version 2>/dev/null || pip install mypy --break-system-packages
```

If either tool cannot be installed, document why and proceed with what's available. ruff alone provides significant value.

---

## Pattern Checks

**Before starting Phase 1, run all pattern checks applicable to this stage.** Read `audit/PATTERN.md` for full context on each pattern. Report results in the stage report under a "Pattern Checks" section.

If `audit/PATTERN.md` does not exist (first audit cycle), skip this section.

### P-001: Silent data truncation in zip()
```bash
grep -rn 'zip(' --include='*.py' | grep -v 'strict=' | grep -v '#.*zip' | grep -v test | grep -v __pycache__ | grep -v .venv
```
Any result where mismatched lengths would be a logic error → add `strict=True`.

### P-002: BaseException vs Exception in asyncio.gather
```bash
grep -rn 'return_exceptions=True' --include='*.py' | grep -v test | grep -v __pycache__
```
Trace each result to where the returned list is consumed. Verify isinstance checks use `BaseException`, not `Exception`.

### P-003: Unchecked filesystem write in tool parameters (grep only — full check in Stage 6)
```bash
grep -rn 'def tool_' --include='*.py' | grep -v test | grep -v __pycache__
```
List all tool functions found. Note any with path-like parameters (`file`, `path`, `output`, `dest`). Full boundary verification happens in Stage 6.

### P-005: Shadowed test classes (duplicate names)
```bash
for f in tests/test_*.py; do
  grep -n '^class ' "$f" | awk -F'[: (]' '{print $2}' | sort | uniq -d | while read cls; do
    echo "DUPLICATE CLASS: $cls in $f"
  done
done
for f in tests/test_*.py; do
  grep -n '^\s*def test_' "$f" | awk -F'def |(' '{print $2}' | sort | uniq -d | while read fn; do
    echo "DUPLICATE FUNCTION: $fn in $f"
  done
done
```
Any output is a finding — a test is being silently dropped.

### P-010: Suppressed security findings without verification
```bash
grep -rn 'noqa: S' --include='*.py' | grep -v __pycache__
```
For each suppression: read the justification comment, then read the surrounding code. Is the justification still accurate? Has the data flow changed?

### P-020: Magic numbers / hardcoded runtime values
```bash
# Numeric literals in function signatures (timeout, limit, max, etc.)
grep -rn 'timeout\s*=\s*[0-9]\|limit\s*=\s*[0-9]\|max_\w*\s*=\s*[0-9]' --include='*.py' | grep -v test | grep -v __pycache__ | grep -v .venv | grep -v mutants | grep -v 'config\.'

# Module-level numeric constants (ALL_CAPS = number)
grep -rn '^[A-Z_]*\s*=\s*[0-9]' --include='*.py' | grep -v test | grep -v __pycache__ | grep -v .venv | grep -v mutants

# Hardcoded URLs in production code
grep -rn 'https\?://.*\.\(com\|io\|org\|net\)' --include='*.py' | grep -v test | grep -v __pycache__ | grep -v .venv | grep -v mutants | grep -v '#'
```
For each result: could a different deployment need a different value? If yes, it should be config-driven. Exempt: mathematical constants, protocol constants, framework-internal invariants.

### P-021: Provider-specific defaults in framework code
```bash
# OpenAI-specific defaults (outside providers/)
grep -rn 'openai\|text-embedding\|whisper-1\|gpt-' --include='*.py' | grep -v test | grep -v __pycache__ | grep -v .venv | grep -v mutants | grep -v providers/ | grep -v '#'

# Anthropic-specific defaults (outside providers/)
grep -rn '200.000\|200000\|anthropic\|claude-' --include='*.py' | grep -v test | grep -v __pycache__ | grep -v .venv | grep -v mutants | grep -v providers/ | grep -v '#'

# ElevenLabs-specific defaults (outside providers/)
grep -rn 'elevenlabs\|eleven_' --include='*.py' | grep -v test | grep -v __pycache__ | grep -v .venv | grep -v mutants | grep -v '#'
```
For each result: is this a provider-specific value used as a framework default? Allowed in provider files and runtime dispatch branches. NOT allowed as config.py property defaults, function parameter defaults, or module constants in framework code.

### P-022: Channel/transport identifiers in framework code
```bash
grep -rn "telegram\|whatsapp\|signal\|discord" lucyd/ \
  --exclude-dir=channels --exclude-dir=providers \
  --exclude-dir=providers.d --exclude-dir=tests \
  --include="*.py"
```
Expected: zero matches. Channel names belong in `channels/` modules and config, never in framework logic. Also enforced by `tests/test_audit_agnostic.py:TestChannelAgnosticism`.

---

## Phase 1: Discovery — Scope the Codebase

**Why:** Know what you're scanning before you scan it. Don't miss source files. Don't scan generated files or vendor code.

```bash
# Find all production Python files
find . -name "*.py" -not -path "./tests/*" -not -path "./.venv/*" \
    -not -path "./mutants/*" -not -path "./__pycache__/*" | sort

# Count
find . -name "*.py" -not -path "./tests/*" -not -path "./.venv/*" \
    -not -path "./mutants/*" -not -path "./__pycache__/*" | wc -l

# Find all test files
find ./tests -name "*.py" | sort

# Check for any config files that might affect linting
ls pyproject.toml setup.cfg .flake8 .ruff.toml ruff.toml 2>/dev/null
```

Record the inventory:

```markdown
## Source Inventory
Production files: [count]
Test files: [count]
Existing lint config: [yes/no, which file]
```

**Confidence check:** Did you find ALL production Python files? Check for files in unexpected locations (bin/, scripts/, root directory). Are there any `.py` files outside the expected directories?

---

## Phase 2: Configure

**Why:** Default ruff rules produce noise. Security-relevant rules produce signal. Configure for what matters.

### Check for Existing Config

```bash
# Check if ruff config already exists
grep -A 20 "\[tool.ruff\]" pyproject.toml 2>/dev/null
cat ruff.toml 2>/dev/null
cat .ruff.toml 2>/dev/null
```

### If No Config Exists

Create a ruff configuration section in `pyproject.toml`. If editing `pyproject.toml` feels risky (it also configures pytest and mutmut), create a standalone `ruff.toml` instead.

**Recommended rule selection for Lucyd:**

```toml
[tool.ruff]
target-version = "py311"  # Adjust to actual Python version
line-length = 120

[tool.ruff.lint]
select = [
    # Security (highest priority for Lucyd)
    "S",      # flake8-bandit: security anti-patterns
    
    # Bugs
    "E",      # pycodestyle errors
    "F",      # pyflakes: unused imports, undefined names, dead code
    "W",      # pycodestyle warnings
    "B",      # flake8-bugbear: common bug patterns
    
    # Code quality
    "UP",     # pyupgrade: outdated Python syntax
    "SIM",    # flake8-simplify: unnecessary complexity
    "RET",    # flake8-return: return statement issues
    "PTH",    # flake8-use-pathlib: os.path → pathlib
    
    # Import hygiene
    "I",      # isort: import ordering
    "TID",    # flake8-tidy-imports: import restrictions
]

ignore = [
    # Common intentional patterns in Lucyd
    "S603",   # subprocess call - check manually, don't auto-flag
    "S607",   # partial executable path - check manually
    "E501",   # line too long (handled by formatter if used)
]

[tool.ruff.lint.per-file-ignores]
"tests/*" = ["S101", "S106"]  # assert and hardcoded passwords OK in tests
```

**Why these rules:**
- **S (bandit):** Security. Finds `eval()`, `exec()`, `pickle.loads()`, hardcoded passwords, weak crypto, SQL injection, command injection. This is the most valuable rule set for Lucyd because Lucy executes external data.
- **F:** Dead code, undefined names, unused imports. Unused imports are attack surface — code that's importable but unmaintained.
- **B:** Bug patterns. Things like mutable default arguments, bare `except`, `assert` in production code.
- **SIM:** Simplification. Complex code hides bugs. Simpler code is auditable.
- **UP:** Outdated patterns. Python 3.11+ has better patterns for things Lucyd does.

**Why these ignores:**
- **S603/S607:** Lucyd intentionally uses subprocess for shell tool execution. Auto-flagging every subprocess call produces noise. These get manual review in Phase 4 instead.

**Confidence check:** Does the config target the right Python version? Run `python --version` and match `target-version`. Does the config exclude test files from security rules that are OK in tests (`assert`, test passwords)?

---

## Phase 3: Run Linter

**Why:** Generate the findings. Run on production code first, then tests separately.

### Production Code

```bash
# Full lint — show all findings grouped by rule
ruff check . --exclude tests --exclude .venv --exclude mutants --statistics

# Detailed output with file and line
ruff check . --exclude tests --exclude .venv --exclude mutants --output-format=full
```

### Test Code

```bash
# Tests get relaxed rules (S101 assert is fine)
ruff check tests/ --output-format=full
```

### Type Checking (If Type Hints Exist)

```bash
# Check if the codebase uses type hints at all
grep -rn "def .*->.*:" lucyd.py channels/ tools/ | head -20
grep -rn ": str\|: int\|: bool\|: list\|: dict\|: Optional" lucyd.py channels/ tools/ | head -20
```

If type hints are present:
```bash
# Run mypy — may need configuration
mypy lucyd.py channels/ tools/ --ignore-missing-imports --no-strict-optional 2>&1 | head -50
```

If type hints are sparse or absent, skip mypy and note it in the report:
```markdown
Type checking: SKIPPED — codebase has minimal type annotations.
Recommendation: Add type hints to security-critical functions first.
```

**Do not add type hints during this audit.** That's a separate task. This audit finds and fixes bugs, not refactors.

---

## Phase 4: Categorize Findings

**Why:** Not all findings are equal. A security finding in `_process_message` matters more than a missing import in a test helper. Categorize before fixing.

For each finding from Phase 3, assign a category:

| Category | Priority | Description | Action |
|----------|----------|-------------|--------|
| **SECURITY** | Critical | `eval()`, `exec()`, unsafe `subprocess`, hardcoded secrets, SQL injection, command injection, path traversal, unsafe deserialization | Fix immediately. Verify fix. |
| **BUG** | High | Undefined names, unreachable code, type errors at boundaries, mutable defaults, bare excepts catching too broadly | Fix. Write test if none exists. |
| **DEAD CODE** | Medium | Unused imports, unused variables, unused functions, unreachable branches | Remove. Verify tests still pass. |
| **STYLE** | Low | Import ordering, line length, naming conventions, outdated syntax | Fix if easy. Skip if contentious. |
| **INTENTIONAL** | None | Findings that are correct code flagged by overzealous rules | Suppress with `# noqa: XXXX` + comment explaining why |
| **FALSE POSITIVE** | None | Tool got it wrong — the code is fine | Suppress with `# noqa: XXXX` |

### Manual Security Review

Regardless of what ruff finds, manually check for these patterns in production code. ruff's S603/S607 were ignored in config — review them now:

```bash
# subprocess calls — verify each one uses _safe_env() and explicit args (not shell=True)
grep -rn "subprocess\.\|Popen\|call\|check_output\|check_call" tools/ channels/ lucyd.py | grep -v test | grep -v __pycache__

# eval/exec — should not exist in production code
grep -rn "eval(\|exec(" tools/ channels/ lucyd.py | grep -v test | grep -v __pycache__

# pickle/marshal/shelve — unsafe deserialization
grep -rn "pickle\.\|marshal\.\|shelve\." tools/ channels/ lucyd.py | grep -v test | grep -v __pycache__

# os.system — should never be used
grep -rn "os\.system(" tools/ channels/ lucyd.py | grep -v test | grep -v __pycache__

# SQL injection — check for f-string or .format() in SQL queries
grep -rn "execute.*f\"\|execute.*\.format\|executemany.*f\"\|executemany.*\.format" tools/ channels/ lucyd.py | grep -v test | grep -v __pycache__

# Hardcoded secrets — tokens, passwords, API keys in source
grep -rn "token\s*=\s*['\"].\+['\"]" tools/ channels/ lucyd.py | grep -v test | grep -v __pycache__ | grep -vi "token\s*=\s*['\"]['\"]" | grep -vi example | grep -vi placeholder

# tempfile without cleanup
grep -rn "tempfile\.\|mktemp\|mkdtemp" tools/ channels/ lucyd.py | grep -v test | grep -v __pycache__
```

For each match:
```markdown
| File:Line | Pattern | Assessment | Action |
|-----------|---------|------------|--------|
| tools/shell.py:45 | subprocess.run | Uses _safe_env(), explicit args list, timeout | INTENTIONAL — safe |
| tools/web.py:120 | eval( | ... | SECURITY — must investigate |
```

**Confidence check before proceeding to fixes:** Have you categorized EVERY finding? Are you at 90%+ confidence on each categorization? If a finding is ambiguous, flag it for Nicolas's review — don't guess.

---

## Phase 5: Fix

**Why:** Findings have no value if they stay findings. Fix what can be fixed safely.

### Fix Order

1. **SECURITY findings** — Fix immediately. Verify each fix doesn't break tests.
2. **BUG findings** — Fix. If a test doesn't exist for the affected code, write one.
3. **DEAD CODE** — Remove. Verify tests still pass.
4. **STYLE** — Fix if easy and non-controversial. Skip if it would change many files.
5. **INTENTIONAL** — Add `# noqa: XXXX — reason` suppression.

### Fix Protocol (Per Finding)

```
1. UNDERSTAND the finding. Read the code. Read surrounding context.
2. ASSESS confidence. Am I 90%+ sure this fix is correct?
   - If yes → proceed
   - If no → flag for Nicolas, add to report as "NEEDS REVIEW"
3. FIX the code
4. VERIFY: python -m pytest tests/ -q (all tests pass?)
5. VERIFY: ruff check <file> (finding is gone?)
6. COMMIT if clean, or continue to next finding
```

**Do not batch fixes.** One finding, one fix, one verification. If a fix breaks something, you know exactly which fix caused it.

### Suppression Rules

When adding `# noqa` suppression:
```python
# GOOD — explains why the rule doesn't apply
result = subprocess.run(cmd, env=_safe_env(), timeout=30)  # noqa: S603 — args list, safe env, timeout bounded

# BAD — suppresses without explanation
result = subprocess.run(cmd)  # noqa: S603
```

Every suppression must have a comment explaining why the code is correct despite the rule violation. Suppression without explanation is not allowed.

---

## Phase 6: Verify

**Why:** Confirm all fixes are clean and nothing was broken.

```bash
# All tests still pass
python -m pytest tests/ -q

# All lint findings addressed (fixed or suppressed with reason)
ruff check . --exclude .venv --exclude mutants --statistics

# Zero security findings remaining
ruff check . --exclude .venv --exclude mutants --select S --statistics
```

**Expected output:** Zero errors on security rules. Low or zero errors on bug rules. Style findings may remain if deferred.

---

## Phase 7: Report

Write the report to `audit/reports/1-static-analysis-report.md`:

```markdown
# Static Analysis Report

**Date:** [date]
**Duration:** [time]
**Tools:** ruff [version], mypy [version or SKIPPED]
**Python version:** [version]
**Files scanned:** [count]
**EXIT STATUS:** PASS / FAIL / PARTIAL

## Scope
[List of directories/files scanned]

## Configuration
[Ruff rules enabled, ignores, per-file-ignores]

## Findings Summary

| Category | Count | Fixed | Suppressed | Deferred |
|----------|-------|-------|------------|----------|
| SECURITY | | | | |
| BUG | | | | |
| DEAD CODE | | | | |
| STYLE | | | | |
| INTENTIONAL | | | | |
| FALSE POSITIVE | | | | |

## Security Review
[Results of manual grep checks from Phase 4]

| Pattern | Occurrences | All Safe? | Details |
|---------|-------------|-----------|---------|
| subprocess | | | |
| eval/exec | | | |
| pickle | | | |
| os.system | | | |
| SQL f-strings | | | |
| Hardcoded secrets | | | |
| tempfile | | | |

## Fixes Applied
[For each fix: file, finding, what was changed, verification]

## Suppressions Added
[For each suppression: file:line, rule, justification]

## Deferred Items
[Findings flagged for Nicolas's review with explanation of uncertainty]

## Type Checking
[Results or SKIPPED with reason]

## Recommendations
[Suggested improvements for next audit cycle]

## Confidence
[Overall confidence in results: X%]
[Any areas of uncertainty]
```

### Exit Status Criteria

- **PASS:** Zero SECURITY findings. Zero BUG findings. All dead code removed or justified. All suppressions have explanations.
- **PARTIAL:** Zero SECURITY findings. Some BUG or DEAD CODE findings deferred with justification. Acceptable if deferred items are non-critical.
- **FAIL:** Any SECURITY finding unfixed. Any BUG finding in security-critical code unfixed. Blocks proceeding to Stage 2.
