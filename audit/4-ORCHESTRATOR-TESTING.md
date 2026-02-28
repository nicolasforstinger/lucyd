# 4 — Orchestrator Testing Audit

**What:** Verify that the daemon's wiring code — the glue that connects channels, providers, tools, sessions, and the agentic loop — works correctly. This is done through contract tests (testing external behavior) and extracted decision functions (testing internal logic as pure functions).

**Why:** The orchestrator (`lucyd.py`) is the most critical file in the codebase. It processes every message, manages every session, enforces cost limits, triggers compaction, and delivers every reply. But mutation testing (Stage 3) doesn't work on it — `lucyd.py` produced 1121 untestable mutants, 497 fork deadlocks, and 76 ambiguous kills. The signal-to-noise ratio was too low.

The real bug we found in `lucyd.py` — `Path("~/.lucyd/monitor.json").expanduser()` hardcoding the monitor path instead of using `self.config.state_dir` — was found by manual testing, not mutmut. The old tests PASSED on the broken code because they mocked `expanduser()`, papering over the bug. Contract tests that use real `tmp_path` instead of mocking path resolution would have caught it.

This audit uses two strategies: extract testable decisions from the monolith into pure functions (which CAN be mutation-tested), and write contract tests that verify external behavior without testing internals.

**When to run:** After changes to `lucyd.py` or orchestrator logic, during full audit (Stage 4), or when behavior bugs appear that component tests didn't catch.

**Scope:** Orchestrator files only — currently `lucyd.py`. For component modules (tools, channels, providers), use `3-MUTATION-TESTING.md`.

---

## How to Think

Orchestrator code makes decisions AND does wiring in the same place. The decisions are testable. The wiring is testable. But only if you separate them.

**Decisions** = `if` statements, threshold checks, routing logic, classification. These can be extracted into pure functions with no `self`, no `await`, no side effects. Input → output. Mutation-testable.

**Wiring** = calling `session_mgr.get_or_create()`, calling `provider.complete()`, calling `channel.send()`. These are procedures with side effects. Testable through contract tests that verify: given this input and these conditions, what side effects occur?

**`_process_message` returns `None`.** All behavior is side effects — messages sent, sessions created, costs tracked. Every assertion in contract tests is on mock interactions, because the interactions ARE the behavior.

**Confidence gate:** Before executing any step:
- Am I 90%+ confident this is correct?
- If not, what am I unsure about? Investigate before proceeding.

After executing any step:
- Did the result match expectations?
- If not, diagnose before moving on.

**Never write a test that accepts broken behavior.** Contract tests assert on mock interactions — which means you decide what the "correct" interaction looks like. If `_process_message` doesn't call `channel.send` when it should, the wrong response is to remove the assertion. The wrong response is to assert it was called zero times and document "delivery suppression by design." The right response is to fix the code or investigate why the mock setup doesn't reach that code path. Every assertion you weaken is a behavior you stop verifying.

---

## Pattern Checks

**Before starting Phase 1, check `audit/PATTERN.md` for any patterns indexed to Stage 4.** Currently indexed patterns: P-017 (compaction state persistence order), P-023 (CLI/API data parity). Run those checks and report results.

### P-017: Crash-unsafe state mutation sequences
For each state-mutating operation in the orchestrator (compaction, session creation, cost tracking):
1. WHERE is in-memory state modified?
2. WHERE is it persisted (`_save_state`, db write, file write)?
3. WHAT happens between those two points?
4. If the process crashes between 1 and 2, is the state recoverable?

If non-trivial work (network calls, other I/O, event logging) happens between the state mutation and the persist, the persist should be moved earlier. Verify the order: critical state change → persist → supplementary operations.

### P-023: CLI/API Interface Parity
Verify that `build_session_info()` (shared function) is used by both CLI and HTTP API, and that cost queries return `cache_read_tokens`/`cache_write_tokens` on both interfaces. Enforced by `tests/test_audit_agnostic.py:TestInterfaceParity`.

If `audit/PATTERN.md` does not exist (first audit cycle), skip this section.

---

## Phase 1: Discovery — Map the Orchestrator

**Why:** The orchestrator's internal architecture must be understood from source before writing any tests. No assumptions. Read the code.

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
[ -f lucyd.py ] || { echo "ERROR: Not in project root."; exit 1; }
source .venv/bin/activate

# Identify orchestrator files (files that wire components together)
# Currently: lucyd.py is the only orchestrator (confirmed — no second orchestrator)
# channels/__init__.py and providers/__init__.py have factory functions but aren't orchestrators
grep -rn "async def _process\|async def _message_loop\|async def run" *.py | grep -v test
```

### Read `_process_message` Completely

Read the method from start to end. Map every decision point:

```markdown
| Line | Decision | Currently Extracted? | Priority |
|------|----------|---------------------|----------|
```

Check which decisions are already pure functions:
```bash
grep -n "def _should_\|def _is_\|def _inject_\|def _classify_" lucyd.py
```

Check which decisions are still inline:
```bash
# Look for if statements in _process_message that aren't just calling extracted functions
grep -n "if " lucyd.py | head -40  # filter to _process_message range
```

### Read Existing Test Infrastructure

```bash
# Existing test files for the orchestrator
# lucyd.py splits across 4 test files by testing layer:
# test_daemon_helpers.py, test_daemon_integration.py, test_orchestrator.py, test_monitor.py
ls tests/test_daemon*.py tests/test_orchestrator*.py tests/test_monitor*.py 2>/dev/null

# Existing mock setup patterns
grep -n "def _make_daemon\|def daemon\|@pytest.fixture" tests/test_daemon*.py tests/test_orchestrator*.py tests/test_monitor*.py 2>/dev/null
```

**Read the existing mock setup helper(s) completely.** Record:
- What components are mocked (provider, channel, session_mgr, etc.)
- How they're mocked (MagicMock, AsyncMock, return values)
- What fixtures exist

### Map `_process_message` Flow

Record the actual flow with verified line numbers:

```markdown
## _process_message Flow
1. [line] [what happens] (sync/async)
2. [line] [what happens] (sync/async)
...
```

### Identify What Must Be Mocked

Record every external component `_process_message` touches:

```markdown
| Component | Access Pattern | Mock Type |
|-----------|---------------|-----------|
```

**Confidence check:** Have you read the ENTIRE method? Do you know every component it touches? Can you trace the flow from message receipt to reply delivery? If not, read it again.

---

## Phase 2: Extract Decisions

**Why:** Inline decisions trapped in 200+ lines of async orchestration are unreachable by unit tests. Extract them into pure functions that can be tested directly and mutation-verified.

### Identify Extraction Candidates

From Phase 1's decision map, identify decisions that:
- Contain an `if` statement or comparison
- Could be a pure function (no `self`, no `await`, no side effects needed)
- Are NOT already extracted

**Skip these:**
- Pure wiring (`response = await provider.complete(messages)` — no decision)
- Logging (`log.info(...)` — cosmetic)
- Single-use setup (no branches = nothing to test)
- Already-tested component calls (agentic loop, session manager have their own tests)

### Extraction Process (Per Candidate)

```
1. IDENTIFY the decision in the source
2. VERIFY: Is it actually extractable? Does it depend on values from
   an immediately preceding await? If so, extraction may not be clean.
   Confidence check: 90%+ that this can be a pure function?
3. WRITE the pure function:
   - No self parameter
   - No await
   - No side effects
   - Input: only the values needed for the decision
   - Output: the decision result (bool, enum, value, tuple)
4. REPLACE the inline logic with a call to the function
5. TEST: python -m pytest tests/ -q (existing tests still pass?)
   If tests break, the extraction changed behavior. Revert and investigate.
6. WRITE unit tests for the extracted function
7. MUTATION-TEST the extracted function using 3-MUTATION-TESTING.md methodology
   (It's an isolated pure function now — mutmut works perfectly)
8. COMMIT: git add lucyd.py tests/ && git commit -m "refactor: extract _function_name"
```

**One extraction at a time.** Extract, test, verify, commit. Never batch.

### Example Extraction

Before:
```python
# Inside _process_message — find with:
# grep -n "pending_system_warning\|total_tokens.*max_context" lucyd.py
if (session.total_tokens > self.config.max_context * 0.8
        and not session.pending_system_warning
        and session.compaction_count == 0):
    session.pending_system_warning = "Context is getting long..."
```

After:
```python
def _should_warn_context(total_tokens: int, max_context: int,
                         already_warned: bool, compaction_count: int,
                         threshold: float = 0.8) -> bool:
    """Pure decision: should we warn about context length?"""
    if already_warned or compaction_count > 0:
        return False
    return total_tokens > max_context * threshold

# In _process_message:
if _should_warn_context(session.total_tokens, self.config.max_context,
                        bool(session.pending_system_warning),
                        session.compaction_count):
    session.pending_system_warning = "Context is getting long..."
```

Tests for the extracted function:
```python
def test_warn_at_threshold():
    assert _should_warn_context(8001, 10000, False, 0) is True

def test_no_warn_below_threshold():
    assert _should_warn_context(7999, 10000, False, 0) is False

def test_no_warn_already_warned():
    assert _should_warn_context(9999, 10000, True, 0) is False

def test_no_warn_after_compaction():
    assert _should_warn_context(9999, 10000, False, 1) is False

def test_exact_boundary():
    assert _should_warn_context(8000, 10000, False, 0) is False  # not >
```

These tests are mutation-testable. mutmut changes `>` to `>=` → boundary test catches it. mutmut changes `and` to `or` → compaction test catches it.

---

## Phase 3: Contract Tests

**Why:** Contract tests verify the orchestrator's external behavior without testing internals. Given this input and these conditions, what side effects occur?

### Setup Pattern

```python
@pytest.fixture
def daemon(tmp_path):
    """Real daemon with all external components mocked.
    IMPORTANT: Read the existing mock helper in tests/test_monitor.py
    and adapt. This template is illustrative — the actual mock setup
    must match what _process_message accesses."""
    # Build from existing fixture pattern
    # ...
```

**Critical:** Read the existing mock helpers in the test files before writing new fixtures. The mock structure must match what `_process_message` actually accesses. If Phase 1 found an existing `_make_daemon_for_monitor` or similar helper, use it as the starting point.

### Contract Test Categories

Write tests for each behavioral contract. Each test:
1. Sets up the daemon with specific conditions
2. Calls `_process_message` with specific inputs
3. Asserts on specific side effects (mock calls, state changes)

**Category 1: Basic message flow**
- Message in → reply delivered via `channel.send`
- Session created for sender
- User message persisted

**Category 2: Error handling**
- Provider error → graceful message sent, no crash
- Unknown model → early return with error
- Source in `_NO_CHANNEL_DELIVERY` → no channel delivery on error

**Category 3: Typing indicators**
- Typing sent for channel sources (telegram, cli)
- Typing NOT sent for suppressed sources (system, http)
- Typing NOT sent when disabled in config

**Category 4: Silent token suppression**
- Reply matching silent tokens → `channel.send` NOT called
- Normal reply → `channel.send` called

**Category 5: Delivery suppression**
- System source → no channel delivery
- HTTP source → no channel delivery (uses future instead)
- Empty reply → no delivery
- CLI source → delivery proceeds

**Category 6: Warning injection**
- `pending_system_warning` set → warning prepended to user text, warning consumed
- No warning → text unchanged

**Category 7: Compaction**
- Session over threshold → compaction triggered
- Session under threshold → no compaction
- Warning set before hard compaction

**Category 8: HTTP future resolution**
- `response_future` provided → result set on future
- Provider error with future → error set on future
- No future → no crash

**Category 9: Message persistence**
- Assistant messages added to session
- Tool results added to session
- State saved after processing

**Category 10: Memory v2 wiring (if consolidation_enabled)**
- Structured recall injected at session start (`recall()` called, result added to context)
- Structured memory tools (`memory_write`, `memory_forget`, `commitment_update`) registered when `consolidation_enabled=True`
- Structured memory tools NOT registered when `consolidation_enabled=False`
- Pre-compaction consolidation called before compaction truncates session (and failure doesn't block compaction)
- Session close callback fires consolidation for the closing session
- All Memory v2 paths are try/except isolated — failure in any structured memory operation does not crash `_process_message`

**Category 11: System session auto-close**
- System-sourced sessions are one-shot — auto-closed after processing
- Session close callback fires for auto-closed sessions
- Agentic loop error → no auto-close (session stays for retry)

**Category 12: Quote reply context injection**
- Telegram quote replies inject `[quoting: "..."]` into user text
- Quote text from `reply_to_message` and `quote` fields is extracted
- Long quotes are truncated
- Non-text quotes are handled gracefully

### Writing Contract Tests

For each test:

```
1. WHAT contract am I testing? (one sentence)
2. WHAT conditions do I set up? (daemon state, mock config)
3. WHAT do I call? (exact _process_message args)
4. WHAT side effects do I assert? (mock calls, state changes)
5. Confidence check: Am I 90%+ sure this tests what I claim?
```

After writing each test:
```bash
# Run it
python -m pytest tests/test_orchestrator.py::TestClass::test_name -x -v

# Verify it's not trivially true — does it fail when it should?
# Break the condition being tested, re-run, confirm failure
```

---

## Phase 4: Verify

**Why:** Confirm everything works together.

```bash
# All tests pass
python -m pytest tests/ -q

# No regressions
python -m pytest tests/ -v --tb=short 2>&1 | grep FAILED
# Should be empty
```

### Mutation-test extracted functions

For each extracted function, run mutmut:

```bash
# Configure for lucyd.py directory
# paths_to_mutate = ["./"]  # or wherever extracted functions live
# tests_dir = ["tests/test_orchestrator.py", "tests/test_daemon_helpers.py", "tests/test_daemon_integration.py"]
# pythonpath = ["."]

rm -rf mutants/ .mutmut-cache/
mutmut run
mutmut results
```

**Note:** mutmut on all of `lucyd.py` will still produce noise for non-extracted code. Focus on the kill rates for the EXTRACTED FUNCTIONS specifically, not the module-level rate.

### Contract tests don't need mutation testing

Contract tests verify behavioral contracts — "message in → reply out." They assert on mock interactions because the side effects ARE the behavior. Mutation testing on the orchestrator itself is unreliable (that's why this manual exists). The value of contract tests is verified by:
1. Test passes against correct code
2. Test fails when you break the behavior manually
3. Both verified during Phase 3

---

## Phase 5: Report

Write the report to `audit/reports/4-orchestrator-testing-report.md`:

```markdown
# Orchestrator Testing Report

**Date:** [date]
**Duration:** [time]
**Target:** [orchestrator files tested]
**EXIT STATUS:** PASS / FAIL

## Phase 1: Architecture Map
Decision points found: [count]
Already extracted: [count]
Still inline: [count]
Components mocked: [list]

## Phase 2: Extractions
| Function | Purpose | Tests | Mutation Kill Rate |
|----------|---------|-------|--------------------|

## Phase 3: Contract Tests
| Category | Tests | Status |
|----------|-------|--------|
| Basic message flow | | PASS/FAIL |
| Error handling | | PASS/FAIL |
| Typing indicators | | PASS/FAIL |
| Silent token suppression | | PASS/FAIL |
| Delivery suppression | | PASS/FAIL |
| Warning injection | | PASS/FAIL |
| Compaction | | PASS/FAIL |
| HTTP future resolution | | PASS/FAIL |
| Message persistence | | PASS/FAIL |
| Memory v2 wiring | | PASS/FAIL/N/A |

## Test Counts
| Type | Count |
|------|-------|
| Existing orchestrator tests | |
| New contract tests | |
| New extracted function tests | |
| Total | |

## _process_message Metrics
Lines before extraction: [count]
Lines after extraction: [count]
Inline decisions remaining: [count]

## Confidence
[Overall confidence in orchestrator verification: X%]
[Areas of uncertainty]
```

### Exit Status Criteria

- **PASS:** All contract test categories covered. Extracted functions mutation-verified. All tests pass. `_process_message` got shorter.
- **FAIL:** Contract tests missing for critical behavior. Extracted functions not mutation-verified. Tests failing. Blocks proceeding to Stage 5.
