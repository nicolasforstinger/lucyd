# Audit Patterns

Accumulated from findings across audit cycles. Every pattern traces to a specific finding that was missed or nearly missed. The auditor reads this file before every stage and checks each applicable pattern.

Patterns are not stage-specific — a pattern discovered in Stage 6 may require a check in Stage 1.

## How to Use

Before each stage, scan this file for patterns tagged with that stage. Run the check described. If the check finds a new instance, it's a finding. If the pattern no longer applies (code removed, architecture changed), note it in the report and mark the pattern as retired.

---

## Retrospective Protocol — How Patterns Get Created

Patterns accumulate from two sources: findings during audit stages (handled by `9-BUG-FIX-WORKFLOW.md` Step 6), and **retrospective analysis after production fixes**. The second source is what makes the audit self-evolving — it closes gaps that all seven stages missed.

### When to Run

After any batch of production fixes, hardening changes, or incident responses that weren't caught by the audit pipeline. The trigger is: "We fixed something that the audit should have caught but didn't."

### Protocol

For each fix in the batch:

```
1. WHAT was fixed? (one sentence)
2. WHICH stage should have caught it? Trace through all 7:
   - Stage 1: Would a grep or ruff rule have flagged this?
   - Stage 2: Would a test health check (warnings, isolation) have surfaced it?
   - Stage 3: Would mutation testing have revealed it?
   - Stage 4: Would orchestrator contract tests have caught it?
   - Stage 5: Would dependency chain analysis have found it?
   - Stage 6: Would security boundary analysis have caught it?
   - Stage 7: Would documentation cross-referencing have caught it?
3. WHY did that stage miss it? What check is absent?
4. WHAT class of bug does this represent? (generalize beyond this instance)
5. DOES this reveal a rule that all code must follow?
   - If YES: create an AI-NNN invariant in addition to the P-NNN pattern.
   - The invariant is the rule. The pattern is the check.
   - Example: finding "compaction cost not tracked" creates P-027 (the check)
     AND AI-001 (the rule "all LLM calls must be tracked").
6. WHAT grep/check would catch future instances of this class?
7. CREATE pattern entry (P-NNN) with origin, class, check, stage index.
```

### Self-Evolution Mechanics

The audit improves through three feedback loops:

**Loop 1 — Stage-internal (already exists):** Each stage runs its pattern checks before starting. New patterns from previous cycles feed into the next cycle automatically.

**Loop 2 — Post-fix retrospective (this protocol):** Fixes that bypassed all stages generate new patterns. The retrospective traces the gap, generalizes it, and adds the check. This is the primary mechanism for catching blind spots.

**Loop 3 — Cross-stage propagation:** When Stage N finds something, ask: "Why didn't Stages 1 through N-1 catch this?" If the answer is "no check exists," create a pattern indexed to the earlier stage. This prevents findings from clustering at late stages when they could be caught cheaply at early ones.

### Pattern Retirement

A pattern becomes a candidate for retirement when:
- The code it checks has been removed or architecturally replaced
- Three consecutive audit cycles find zero instances
- A structural change makes the class of bug impossible (e.g., type system enforcement)

Retired patterns stay in this file marked `**RETIRED [date]:**` with the reason. They serve as historical record of what the audit has learned.

### Known Gaps Lifecycle

Audit reports contain "Known Gaps" sections. These must be tracked, not just noted:

| Status | Meaning |
|--------|---------|
| **Open** | Gap exists, no mitigation. Must be re-evaluated each cycle. |
| **Mitigated** | Gap exists but compensating control added (pattern check, test, etc.). |
| **Resolved** | Gap closed by code change. Remove from Known Gaps, note in changelog. |
| **Accepted** | Gap acknowledged as acceptable risk with justification. Re-evaluate annually. |

Each full audit cycle must review all Open and Accepted gaps. If a gap has been Open for 2+ cycles without action, escalate.

---

## P-001: Silent data truncation in zip()

**Origin:** Cycle 1 Stage 1 — `cosine_sim()` in memory.py used `zip(a, b)` without `strict=True`. Dimension mismatch silently truncated to shorter list, producing wrong similarity scores. Cycle 2 Stage 1 found the same bug in `tools/indexer.py` and `tests/test_context.py`.

**Class:** Any `zip()` call where mismatched lengths would be a logic error rather than intentional truncation.

**Check (Stage 1):**
```bash
# Find all zip() calls without strict=True
grep -rn 'zip(' --include='*.py' | grep -v 'strict=' | grep -v '#.*zip'
```
For each result: would mismatched lengths be a bug or intentional? If a bug, add `strict=True`.

**Recurrence:** Found in both Cycle 1 and Cycle 2. Treat as recurring until all `zip()` calls are audited.

---

## P-002: BaseException vs Exception in asyncio.gather

**Origin:** Cycle 1 Stage 1 — `asyncio.gather(return_exceptions=True)` returns `list[T | BaseException]`. Code checked `isinstance(result, Exception)`, missing `KeyboardInterrupt` and `SystemExit`. A `BaseException` from a tool would bypass the error handler and corrupt the API message.

**Class:** Any code that handles exceptions from `asyncio.gather(return_exceptions=True)` or similar patterns that surface `BaseException` subclasses.

**Check (Stage 1):**
```bash
grep -rn 'return_exceptions=True' --include='*.py'
```
For each result: trace where the returned list is consumed. Verify the isinstance check uses `BaseException`, not `Exception`.

---

## P-003: Unchecked filesystem write in tool parameters

**Origin:** Cycle 2 Stage 6 — `tool_tts()` accepted `output_file` parameter and wrote to disk without `_check_path()` validation. The messaging tool validated attachment paths, but TTS was implemented without the same check. Prompt injection could overwrite personality files with binary audio.

**Class:** Any tool that accepts a file path as a parameter and writes to that path. The LLM is untrusted — it can be instructed by prompt injection to pass any path.

**Check (Stage 6, also Stage 1 as grep):**
```bash
# Find all tool functions
grep -rn 'def tool_' --include='*.py'
```
For each tool: inspect every parameter. If any parameter is used as a file path for reading or writing, verify `_check_path()` is called before the I/O operation. Default/internal paths (e.g., mkstemp) are exempt only if the parameter cannot override them.

**Cross-reference:** Compare against the capability table in the security audit. If the table says "no filesystem access" for a tool, verify that claim by reading the source — don't trust the previous audit's assessment.

---

## P-004: Test fixture iteration-order blindness

**Origin:** Cycle 2 Stage 3 — `_safe_env()` tests added secret variables via `monkeypatch.setenv()`, which appends to `os.environ`. CPython dicts maintain insertion order, so secrets were always last in iteration. `continue→break` mutations produced identical results because `break` on the last matching item skips nothing. Kill rate appeared 87% but was actually missing a real class of mutation.

**Class:** Any test for a function that filters or iterates over a collection (dict, list, set) where test data is only placed at one position in the iteration order.

**Check (Stage 3):**
When reviewing mutation survivors in filter/iteration functions, ask: "Would this mutant survive if the matching item were at the beginning or middle of the collection instead of the end?" If yes, the test needs data at multiple positions.

Specifically for dict-iterating filters:
```bash
grep -rn 'monkeypatch.setenv\|os.environ' tests/ --include='*.py'
```
Verify that tests which add environment variables place both matching and non-matching entries, and that non-matching entries appear AFTER matching ones in insertion order.

---

## P-005: Shadowed test classes (duplicate names)

**Origin:** Cycle 2 Stage 1 — `TestVectorSearchLimit` was defined twice in `test_memory.py`. Python silently replaces the first class with the second. One test was invisible — it showed in no output, no failure, no collection count. Recovered by renaming.

**Class:** Duplicate class or function names in test files. Python's name resolution silently shadows earlier definitions.

**Check (Stage 1):**
```bash
# Find duplicate class names within test files
for f in tests/test_*.py; do
  grep -oP '^class \K[A-Za-z_]+' "$f" | sort | uniq -d | while read cls; do
    echo "DUPLICATE: $cls in $f"
  done
done
```
Any output is a finding — a test class is being silently shadowed.

Also check for same-method shadowing within a class (the real danger):
```python
python3 -c "
import ast
for fname in __import__('glob').glob('tests/test_*.py'):
    tree = ast.parse(open(fname).read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            seen = {}
            for m in methods:
                if m in seen:
                    print(f'SHADOW: {node.name}.{m}() in {fname}')
                else:
                    seen[m] = True
"
```

---

## P-006: Dead data pipeline (producer removed, consumer remains)

**Origin:** Cycle 1 post-audit review — OpenClaw indexer was removed, leaving `memory.py` reading from SQLite that nothing populates. All six stages passed because test fixtures simulated a populated database. No stage asked "does something write to this store?"

**Class:** A module reads from a persistent store, but the process that writes to that store has been removed, disabled, or broken. Test fixtures mask the gap by pre-populating the store.

**Check (Stage 5 — Dependency Chain):**
This pattern is now the entire purpose of Stage 5. For every consumer identified in the data flow matrix, verify the producer exists, is enabled, and has run recently. See `5-DEPENDENCY-CHAIN.md` Phase 1–3.

**Additional check (Stage 2):**
For every test that creates pre-populated fixtures (test databases, pre-written files), ask: "In production, what process creates this data?" If the answer is not covered by another test, add a round-trip test.

---

## P-007: Documentation drift on test counts

**Origin:** Cycle 1 Stage 6 — README.md said "843 tests" when actual count was 855. Cycle 2 Stage 7 — README.md said "855 tests" when actual count was 916.

**Class:** Any documentation that contains a specific number derived from a command output (test count, module count, line count). These go stale every time tests are added or code changes.

**Check (Stage 7):**
```bash
# Extract claimed test count from README
grep -i 'test' README.md | grep -oP '\d{3,}'

# Compare with actual
python -m pytest --collect-only -q 2>/dev/null | tail -1
```
If they differ, update. This has recurred in both cycles — treat as expected maintenance, not a one-time fix.

---

## P-008: New module without documentation

**Origin:** Cycle 2 Stage 7 — `tools/indexer.py` (419 lines) was added without updating `docs/architecture.md` module map or `docs/operations.md` cron table.

**Class:** A new source module or external process is added to the codebase but not reflected in documentation.

**Check (Stage 7):**
```bash
# Compare documented modules against actual source files
find tools/ channels/ providers/ -name '*.py' ! -name '__init__.py' | sort > /tmp/actual_modules.txt
# Extract module names from architecture.md module map
grep -oP '`[a-z_/]+\.py`' docs/architecture.md | tr -d '`' | sort > /tmp/documented_modules.txt
diff /tmp/actual_modules.txt /tmp/documented_modules.txt
```
Any module in actual but not documented is a finding.

Same for cron jobs:
```bash
crontab -l | grep lucyd
# Compare against docs/operations.md cron table
```

---

## P-009: Capability table stale after tool changes

**Origin:** Cycle 2 Stage 6 — TTS was listed as "MEDIUM — API key isolation, no filesystem access" in Cycle 1's security audit. This was wrong — `output_file` writes to disk. The capability assessment from the previous cycle was carried forward without re-verification.

**Class:** The security audit's capability table (tool → danger level → boundaries) becomes stale when tools gain new parameters or behavior between audit cycles.

**Check (Stage 6):**
Do not carry forward the capability table from the previous audit. Re-derive it from source every cycle:
```bash
# For each tool, extract its function signature
grep -A5 'def tool_' tools/*.py | grep -E 'def tool_|path|file|write|output|dest'
```
Any parameter that could be a file path, URL, or external identifier must be traced to a validation boundary. Compare the re-derived table against the previous cycle's table — differences are findings.

---

## P-010: Suppressed security findings without verification

**Origin:** Cycle 1 Stage 1 — 16 findings suppressed with `# noqa` comments. Cycle 2 Stage 1 — 1 new S310 suppression added for indexer.py.

**Class:** A `# noqa` suppression on a security rule (S-prefixed) that was added in a previous cycle and never re-verified. Over time, the code around the suppression may change, making the original justification invalid.

**Check (Stage 1):**
```bash
grep -rn 'noqa: S' --include='*.py'
```
For each suppression: read the justification comment. Then read the surrounding code. Is the justification still accurate? Has the data flow changed? If the justification references "not user-controlled" or "hardcoded URL," verify that's still true.

---

## P-011: Config-to-documentation label mismatch

**Origin:** Cycle 3 Stage 7 — `docs/operations.md` said "primary (Opus)" but the primary model was `claude-sonnet-4-5-20250929` (Sonnet). The label was always wrong — it survived Cycle 2 undetected because the model string itself was correct; only the parenthetical human-readable label was wrong.

**Class:** Documentation uses informal labels (model family names, version nicknames, feature shorthand) that don't match the actual values in config files or source code. The label looks plausible enough to survive visual review.

**Check (Stage 7):**
```bash
# Check model names in config vs docs
grep -r 'model\s*=' providers.d/*.toml | grep -v '#'
grep -ri 'opus\|sonnet\|haiku' docs/ README.md
```
For each model reference in documentation: trace to the actual model string in config. Verify the human-readable label matches. Also check cost rates and any other derived descriptions that could drift when config changes.

---

## P-012: Auto-populated pipeline misclassified as static

**Origin:** Cycle 3 Stage 5 — `entity_aliases` table classified as "Manual SQL (backfill) — not auto-populated" in the dependency chain report, and "admin-managed" in the security report. In reality, `consolidation.py:225` auto-populates aliases via `INSERT OR IGNORE` on every fact extraction call. All three producer paths (cron, pre-compaction, session close) converge on `extract_facts()` which stores aliases. The misclassification was carried forward to Stage 6 unchallenged.

**Class:** A data pipeline classified as "manual" or "static" that is actually auto-populated by an automated process. The auditor sees the data, assumes a simpler provenance, and doesn't verify the write path against source code.

**Check (Stage 5):**
For every pipeline classified as "Manual" or "N/A (static)" in the data flow matrix, verify by grepping for INSERT/write operations on that table or file:

```bash
# For SQLite tables:
grep -rn "INSERT.*INTO.*<table_name>" --include='*.py' | grep -v test | grep -v __pycache__
# For files:
grep -rn "\.write\|write_text\|open.*w" --include='*.py' | grep -v test | grep -v __pycache__
```

If any automated producer exists, the classification is wrong. Trace ALL write paths, not just the obvious ones.

Also verify that auto-populated pipelines with ordering dependencies maintain correct ordering. For example, if table A must be populated before table B (because B's insert logic resolves through A), confirm that ordering is preserved in the code. A refactor that reverses INSERT order can silently break resolution without any test failing — the data just fragments.

**Check (Stage 6):**
If the security audit references a data source as "admin-managed" or "static," verify that claim against Stage 5's producer inventory. Don't trust a previous stage's classification without tracing to source.

---

## P-013: None-defaulted dependency hides untested code branch

**Origin:** Cycle 3 Stage 3 — `recall()` in `memory.py` accepted `memory_interface` as a parameter. All test fixtures passed `None`, which caused the entire vector search code path (~50 mutants) to be skipped. The decay formula, sort order, and `top_k` reduction logic were completely unexercised. Discovered when mutation survivors clustered behind the `if memory_interface is not None` guard.

**Class:** A function accepts an optional/defaulted dependency (database connection, API client, external service interface). Tests use the default (`None` or a no-op mock) which causes an entire code branch to be skipped silently. The function "works" in tests but a significant execution path is unverified.

**Check (Stage 3):**
When reviewing mutation survivors, check if surviving mutants cluster in a code path guarded by an `if`-not-`None` check on a function parameter:

```bash
# Look for patterns in source where None guards a branch:
grep -n "if.*is not None\|if.*is None" <source_file>
```

Then check if any test fixtures pass `None` for that parameter:

```bash
grep -n "memory_interface=None\|conn=None\|provider=None" tests/test_*.py
```

If survivors cluster behind a dependency guard and tests pass `None` for that dependency, the test fixtures need a proper mock, not `None`.

**Check (Stage 2):**
For test fixtures that pass `None` for any dependency parameter, ask: "Does this `None` cause an entire code branch to be skipped?" If yes, flag for mock coverage in Stage 3.

---

## P-014: Unhandled errors at system boundaries

**Origin:** Production hardening 2026-02-22 — `provider.complete()` in `agentic.py` had zero error handling. Rate limits (429), network errors, and server 5xx errors propagated as unhandled exceptions. Both providers (Anthropic, OpenAI) affected. No retry, no backoff. No audit stage checked for error handling at API call sites.

**Class:** Any call to an external system (API, database, network) that lacks try/except for transient failures. The call may work 99% of the time, so tests pass and code reviews don't flag it. But rate limits, network blips, and server errors are inevitable in production.

**Check (Stage 1):**
```bash
# Find external API call sites (provider, httpx, requests, urllib)
grep -rn "\.complete(\|\.post(\|\.get(\|\.put(\|\.delete(\|\.request(" --include='*.py' | grep -v test | grep -v __pycache__
# Find database execute calls
grep -rn "\.execute(\|\.executemany(\|\.executescript(" --include='*.py' | grep -v test | grep -v __pycache__
```
For each call site: is it wrapped in try/except for transient errors? If it's a critical path (message processing, session persistence), the absence of error handling is a finding. Internal helper calls within already-handled blocks are exempt.

**Check (Stage 5):**
For each edge in the dependency chain data flow matrix, ask: "What happens when this edge fails?" If the answer is "unhandled exception propagates to the event loop," that's a finding. Every external edge should have defined failure behavior (retry, fallback, graceful error message, or documented intentional crash).

---

## P-015: Implementation parity across parallel modules

**Origin:** Production hardening 2026-02-22 — `anthropic_compat.py` used bare `json.loads(block.input)` without try/except. `openai_compat.py` had the correct pattern with `_safe_parse_args()` fallback. Same interface, inconsistent error handling. No audit stage compared the two providers' implementations. **(FIXED:** `anthropic_compat.py` now uses `_safe_parse_args()`.)

**Class:** Modules that implement the same protocol or interface (providers, channels) but handle edge cases, errors, or malformed data differently. The inconsistency is invisible when testing each module in isolation — both "work" — but one is fragile where the other is robust.

**Check (Stage 1):**
```bash
# List all implementations of the same interface
# Providers:
ls providers/*.py | grep -v __init__
# Channels:
ls channels/*.py | grep -v __init__
```
For each group of parallel implementations: compare error handling patterns. Specifically:
- Do all providers handle malformed tool input the same way?
- Do all channels handle send failures the same way?
- Do all channels implement the full protocol (including lifecycle methods)?

**Check (Stage 3):**
When mutation-testing one implementation (e.g., `anthropic_compat.py`), check if the same edge-case tests exist for the parallel implementation (`openai_compat.py`). If provider A has a test for malformed JSON input and provider B doesn't, that's a finding.

---

## P-016: Resource lifecycle completeness (open without close)

**Origin:** Production hardening 2026-02-22 — two independent instances:
1. `memory.py` created `self._memory_conn` (sqlite3 connection) in `lucyd.py:_get_memory_conn()`, never closed. WAL files accumulated.
2. `TelegramChannel` created `self._client` (httpx.AsyncClient) in `connect()`, never closed. The `Channel` protocol had no `disconnect()` method at all.

Stage 2 had caught a `ResourceWarning` for the indexer's connection in cycle 5 but didn't generalize the finding.

**Class:** Any resource (database connection, HTTP client, file handle, temp directory) that is created/opened but never closed/cleaned up. The lifecycle is incomplete: init without teardown, connect without disconnect, open without close.

**Check (Stage 1):**
```bash
# Database connections
grep -rn "sqlite3\.connect\|\.connect(" --include='*.py' | grep -v test | grep -v __pycache__
# HTTP clients
grep -rn "httpx\.\|requests\.Session\|aiohttp\.ClientSession" --include='*.py' | grep -v test | grep -v __pycache__
# File opens without context manager
grep -rn "open(" --include='*.py' | grep -v "with " | grep -v test | grep -v __pycache__
```
For each resource creation: trace to its cleanup. If the resource is assigned to `self.*`, the class must have a close/cleanup method that's called during shutdown. If no cleanup exists, that's a finding.

**Check (Stage 2):**
`ResourceWarning` in test output is a **pattern trigger**, not just a one-off finding. When any ResourceWarning appears:
```bash
# Find ALL similar resource creations, not just the one that warned
grep -rn "<resource_type>" --include='*.py' | grep -v test | grep -v __pycache__
```

**Check (Stage 5):**
For each resource in the dependency chain that has a creation step, verify: does the shutdown/cleanup path close it? Trace both normal exit and error paths.

---

## P-017: Crash-unsafe state mutation sequences

**Origin:** Production hardening 2026-02-22 — `session.py` compaction code modified in-memory state (`compaction_count`, `warned_about_compaction`) but called `_save_state()` only after `append_event()`. A crash between the mutation and the persist would lose the compaction. **Fixed:** `_save_state()` now runs at `session.py:506` immediately after state mutation, before `append_event()` at line 507.

**Class:** Code that modifies in-memory state AND persists it to disk/database, where the persist operation doesn't happen immediately after the critical state change. If the process crashes between the mutation and the persist, the state is lost or inconsistent.

**Check (Stage 4):**
For each state-mutating operation in the orchestrator (compaction, session creation, cost tracking):
```
1. WHERE is in-memory state modified?
2. WHERE is it persisted (_save_state, db write, file write)?
3. WHAT happens between those two points?
4. If the process crashes between 1 and 2, is the state recoverable?
```
If non-trivial work (network calls, other I/O, event logging) happens between the state mutation and the persist, the persist should be moved earlier. The supplementary work (audit logs, events) can happen after the critical persist.

**Check (Stage 5):**
For each state persistence flow in the dependency chain, verify the order: critical state change → persist → supplementary operations. Not: critical state change → supplementary operations → persist.

---

## P-018: Unbounded runtime data structures

**Origin:** Production hardening 2026-02-22 — `self._last_inbound_ts` in `lucyd.py` was a plain `dict[str, int]` with one entry per unique sender, never pruned. In a Telegram group scenario with thousands of unique senders, this grows without bound for the daemon's entire lifetime. **(FIXED:** now an `OrderedDict` bounded at 1000 entries.)

**Class:** Any `dict`, `list`, or `set` assigned to `self.*` (instance state) that grows proportional to input volume without eviction or pruning. These are memory leaks that only manifest under sustained production load — tests with 3-5 senders never trigger them.

**Check (Stage 1):**
```bash
# Find dict/set/list assignments on self in production code
grep -rn "self\._.*= {}\|self\._.*= \[\]\|self\._.*= set()\|self\._.*= dict()\|self\._.*= OrderedDict(" --include='*.py' | grep -v test | grep -v __pycache__
```
For each: does the collection grow with input? Is there a cap, eviction, or periodic cleanup? Fixed-size collections (config-derived, known-bounded keys) are exempt. Collections that grow with unique senders, sessions, messages, or external IDs need bounds.

**Check (Stage 6):**
As a resource exhaustion vector: could an attacker (or organic growth) cause unbounded memory consumption by sending messages from many unique senders/sources? Any unbounded collection proportional to attacker-controlled input is a DoS vector.

---

## P-019: Stale gap carried without code verification

**Origin:** Cycle 8 (2026-02-24) — `_check_path()` prefix match finding carried as OPEN for 6 cycles. The code already had the `os.sep` guard since implementation. A dedicated test (`test_sibling_directory_name_rejected`) verified it. The audit was tracking a phantom vulnerability because no cycle checked whether the gap was still open.

**Class:** Known gap or security finding carried forward across audit cycles without verifying the actual code/tests. The audit report says "OPEN" but the code says "fixed." Erodes trust in the audit and wastes attention on resolved issues.

**Check (Aggregate Report — Post-Audit Known Gaps Review):**
For each gap carried from the previous cycle:
1. Read the source code or tests referenced by the gap.
2. Has the gap been fixed since it was reported? (code changed, tests added, config updated)
3. If yes → status: Resolved (stale finding). Do NOT carry forward.
4. If no → verify the gap is still exploitable/relevant given current architecture.

This check is mandatory in `0-FULL-AUDIT.md` Post-Audit: Known Gaps Review (step 3). It applies at the aggregate report level, not per-stage.

---

## P-020: Magic numbers / hardcoded runtime values

**Origin:** Production hardening 2026-02-25 — systematic audit found 18 hardcoded magic numbers across the framework: timeout values (`timeout=15`, `timeout=60`), rate limits (`max_requests=30`), log rotation params (`maxBytes=10*1024*1024, backupCount=3`), JPEG quality steps (`[85, 60, 40]`), scheduling limits (`_MAX_SCHEDULED = 50`), context token assumptions (`MAX_CONTEXT_TOKENS = 200_000`), chunk sizes, read limits, reconnect backoff params. All were reasonable values but none were configurable by operators. A local deployment needing different timeouts, limits, or quality settings required source code changes.

**Class:** Any numeric literal, URL string, or behavioral constant in production source that controls runtime behavior and could reasonably differ between deployments. These should be config-driven (`config.py` property → `configure()` param → module global) with sensible defaults.

**Check (Stage 1):**
```bash
# Find numeric literals in function signatures (timeout, limit, max, etc.)
grep -rn 'timeout\s*=\s*[0-9]\|limit\s*=\s*[0-9]\|max_\w*\s*=\s*[0-9]' --include='*.py' | grep -v test | grep -v __pycache__ | grep -v .venv | grep -v mutants | grep -v 'config\.'

# Find module-level numeric constants (ALL_CAPS = number)
grep -rn '^[A-Z_]*\s*=\s*[0-9]' --include='*.py' | grep -v test | grep -v __pycache__ | grep -v .venv | grep -v mutants

# Find hardcoded URL strings in production code
grep -rn 'https\?://.*\.\(com\|io\|org\|net\)' --include='*.py' | grep -v test | grep -v __pycache__ | grep -v .venv | grep -v mutants | grep -v '#'
```
For each result: is this value deployment-specific (could different operators need different values)? If yes, it should be read from config with a sensible default. Mathematical constants (e.g., `1000` for ms-to-seconds), protocol constants (HTTP status codes), and framework-internal invariants that no operator would change are exempt.

**Check (Stage 7):**
For each configurable value documented in `docs/configuration.md` or `lucyd.toml.example`, verify the default matches the `config.py` property default. If a new config property was added without updating the example file, that's a documentation gap — the operator doesn't know the setting exists.

---

## P-021: Provider-specific defaults in framework code

**Origin:** Production hardening 2026-02-25 — framework code contained OpenAI-specific defaults (`text-embedding-3-small`, `https://api.openai.com/...`), Anthropic-specific assumptions (`MAX_CONTEXT_TOKENS = 200_000`, `supports_vision` defaulting to `True`), and ElevenLabs-specific URLs hardcoded in source. A deployment using only local models (Ollama + whisper.cpp) would inherit cloud-provider defaults, requiring manual overrides even if those providers aren't used. Provider-specific config belongs in provider files (`providers.d/*.toml`) or explicit TOML settings, not in framework defaults.

**Class:** Any default value in framework source code (config.py defaults, function parameter defaults, module constants) that assumes a specific provider (OpenAI, Anthropic, ElevenLabs, etc.). Framework defaults must be provider-agnostic — empty strings, `False`, or `0` for provider-specific capabilities.

**Check (Stage 1):**
```bash
# OpenAI-specific defaults
grep -rn 'openai\|text-embedding\|whisper-1\|gpt-' --include='*.py' | grep -v test | grep -v __pycache__ | grep -v .venv | grep -v mutants | grep -v providers/ | grep -v '#'

# Anthropic-specific defaults
grep -rn '200.000\|200000\|anthropic\|claude-' --include='*.py' | grep -v test | grep -v __pycache__ | grep -v .venv | grep -v mutants | grep -v providers/ | grep -v '#'

# ElevenLabs-specific defaults
grep -rn 'elevenlabs\|eleven_' --include='*.py' | grep -v test | grep -v __pycache__ | grep -v .venv | grep -v mutants | grep -v '#'
```
For each result: is this a provider-specific value used as a framework default (in a function signature, `config.py` property, or module constant)? Provider-specific values are allowed in:
- Provider files (`providers.d/*.toml`, `providers/*.py`)
- Provider-specific `if provider == "..."` branches (runtime dispatch, not defaults)
- TOML config (operator's explicit choice)

They are NOT allowed as:
- `config.py` property defaults
- Function parameter defaults in tools, channels, or core modules
- Module-level constants in framework code

**Check (Stage 7):**
Verify that `lucyd.toml.example` and `providers.d/*.toml.example` make the provider split clear: framework settings in `lucyd.toml`, provider-specific settings in `providers.d/*.toml`. If a provider-specific value appears in `lucyd.toml.example` without being clearly labeled as deployment-specific, flag it.

---

## P-022: Hardcoded channel or transport identifiers in framework code

**Origin:** Audit cycle 9, CLI/API parity review — framework code referenced Telegram-specific config paths for contact resolution, making session listing fail for non-Telegram deployments. Channel-specific identifiers belong only in `channels/` modules and config, never in framework logic.

**Class:** Coupling — framework code references specific channels/transports by name (e.g., `"telegram"`, `"whatsapp"`) outside of `channels/` modules.

**Check (Stage 1):**
```bash
grep -rn "telegram\|whatsapp\|signal\|discord" . \
  --exclude-dir=channels --exclude-dir=providers \
  --exclude-dir=providers.d --exclude-dir=tests \
  --exclude-dir=.venv --exclude-dir=mutants \
  --exclude-dir=.git --exclude-dir=audit \
  --include="*.py"
```
Expected: zero matches. Channel names belong in `channels/` modules and config files, never in framework logic (`lucyd.py`, `session.py`, `context.py`, `agentic.py`, `tools/`, etc.).

Allowed in: `channels/*.py`, `providers/*.py`, `tests/`, config examples, comments.

**Enforcement:** `tests/test_audit_agnostic.py:TestChannelAgnosticism` — static grep over framework source.

---

## P-023: CLI and HTTP API return different data for the same query

**Origin:** Audit cycle 9, interface parity review — CLI `--sessions` returned context tokens, per-session cost, cache tokens, and log metadata, while HTTP `/api/v1/sessions` returned only basic info. CLI `--cost` returned `cache_read_tokens`, HTTP `/cost` did not. The `build_session_info()` shared function now ensures both interfaces return equivalent data.

**Class:** Inconsistency — same query returns different fields/values depending on interface (CLI vs HTTP API).

**Check (Stage 4):**
Contract test that verifies CLI query functions and HTTP callback functions return equivalent data schemas:
- Sessions: both include `context_tokens`, `context_pct`, `cost_usd`, `message_count`, `compaction_count`, `log_files`, `log_bytes`
- Cost: both include `cache_read_tokens`, `cache_write_tokens`, same week window definition
- Monitor: both read from same `monitor.json`

**Enforcement:** `tests/test_audit_agnostic.py:TestInterfaceParity` — verifies shared function output schema.

---

## P-024: HTTP endpoint documentation incomplete

**Origin:** Audit cycle 9 post-audit review — Stage 7 passed despite 6/8 endpoints missing response schemas, all endpoints missing error code documentation, and 3 new endpoints having only one-sentence descriptions.

**Class:** API documentation that lists an endpoint's existence and purpose but omits the response JSON schema, error status codes, or rate limit classification. Operators integrating via HTTP need all three.

**Check (Stage 7):**
For each route in `channels/http_api.py`, verify `docs/operations.md` documents:
1. Request fields with types
2. Response JSON schema with field descriptions
3. All status codes and when they fire
4. Which rate limit group applies

---

## P-025: Python default parameter binding with module globals

**Origin:** Audit cycle 10, Stage 5 dependency chain — embedding indexer freshness check revealed 48 cumulative failures in log.

**Class:** Python function default parameters that reference module-level globals. Python evaluates defaults at function *definition* time, not call time. If the global is `""` at import and set by `configure()` later, functions using `param: str = GLOBAL` capture the empty string forever.

**Root cause:** `tools/indexer.py` used `base_url: str = EMBEDDING_BASE_URL` where `EMBEDDING_BASE_URL` starts as `""` and gets set by `configure()`. Callers that omit `base_url` (like `bin/lucyd-index`) got the stale empty string.

**Fix:** Use `None` sentinel: `param: str | None = None`, resolve at call time: `param = param if param is not None else MODULE_GLOBAL`.

**Status:** FIXED in cycle 10 remediation. 3 functions patched: `embed_batch()`, `cache_embeddings()`, `index_workspace()`.

**Check (Stage 1):**
Grep for function signatures where a default value references a module-level `ALL_CAPS` variable:
```
grep -nP "def \w+\(.*:\s*\w+\s*=\s*[A-Z_]{2,}" *.py tools/*.py
```
Verify the default is either a constant (safe) or resolved via `None` sentinel (safe).

---

## P-026: SDK mid-stream SSE errors misclassified by exception type

**Origin:** Production incident 2026-02-27 — Anthropic API returned `overloaded_error` during a streaming response. The two-tier retry system (P-014) did not fire. Root cause: the Anthropic SDK's `Stream.__stream__()` catches SSE `"error"` events and calls `_make_status_error(response=self.response)`, but `self.response` is the original HTTP 200 — not the error. So `overloaded_error` (equivalent to HTTP 529) arrives as `APIStatusError(status_code=200)`. The retry classifier saw `200 < 429` and classified it as non-transient. SDK's own `max_retries` also doesn't apply — it only covers the HTTP handshake, not stream iteration.

**Class:** An SDK or client library that wraps errors from one layer (SSE events, WebSocket frames, gRPC status codes) using metadata from a different layer (HTTP status). The resulting exception has correct content (error body) but wrong classification metadata (status code, exception class). Generic retry/error-handling logic that dispatches on the metadata misses the error.

**Fix:** Provider-level workaround in `anthropic_compat.py` — catches `APIStatusError` with `status_code < 429` in the streaming path, inspects body for `overloaded_error` / `api_error`, re-raises as correct exception class (`OverloadedError` / `InternalServerError`) with a synthesized `httpx.Response` carrying the correct status code (529/500). This ensures the re-raised exception is indistinguishable from what the SDK should have produced — no other module needs to know the workaround exists. Tagged `HOTFIX(2026-02-27)`. Canary test `test_sdk_bug_still_exists` in `test_providers.py` will fail when the SDK is fixed — that's the signal to delete the hotfix and the `TestAnthropicMidstreamSSEReRaise` test class. SDK: `anthropics/anthropic-sdk-python` (issue #688).

**Check (Stage 5):**
For each provider's `complete()` method, trace the error path for streaming vs non-streaming calls:
1. What exceptions can the SDK raise during stream iteration (not just connection)?
2. Do those exceptions carry the correct status code and class for the retry classifier?
3. If the SDK wraps transport-layer errors using HTTP-layer metadata, is the provider compensating?

```bash
# Find streaming call sites in providers
grep -rn "\.stream(\|\.aiter\|async for" providers/*.py | grep -v test | grep -v __pycache__
```

For each streaming call: verify that errors during iteration (not just connection) are handled or re-raised with correct classification.

**Check (Stage 3):**
Mutation-test the provider's error re-raise logic. Key mutants: removing the body inspection, changing the `status_code < 429` threshold, or removing individual `etype` checks. All should be killed by the `TestAnthropicMidstreamSSEReRaise` tests.

---

## P-027: LLM call without cost tracking

**Origin:** Architecture review — compaction, consolidation, memory synthesis, and STT calls bypass `_record_cost()`. The operator's cost.db underreports actual spend.

**Class:** Any code path that calls `provider.complete()` or an external inference API (Whisper, embeddings) without recording the cost in cost.db.

**Check (Stage 1):**
```bash
# Find all provider.complete() call sites
grep -rn 'provider\.complete\|\.complete(' --include='*.py' | grep -v test | grep -v __pycache__

# Find all external API calls (Whisper, embeddings, synthesis)
grep -rn 'transcri\|whisper\|embed\|synthesis\|synthesize' --include='*.py' | grep -v test | grep -v __pycache__ | grep -i 'await\|result\|response'
```
For each call site: trace forward. Is `_record_cost()` called with the usage data? If not, the cost is invisible.

**Check (Stage 5):**
In the dependency chain for cost.db: are all LLM call sites listed as producers? Compare the Stage 1 grep results against the cost.db producer list. Any call site not producing cost records is a gap.

---

## P-028: Mutation endpoint bypassing message queue

**Origin:** Architecture review — `/sessions/reset` directly mutates session state without going through the queue, creating a race condition with `_process_message()`.

**Class:** Any HTTP endpoint or internal function that modifies session state, memory DB, or workspace files without routing through the sequential message queue.

**Check (Stage 4):**
```bash
# Find all HTTP route handlers
grep -rn 'def handle_\|async def handle_\|@routes\.' channels/http_api.py | grep -v test

# For each handler that modifies state (POST/PUT/DELETE endpoints):
# Does it push to the queue, or does it directly call session/memory/file operations?
grep -A20 'def handle_' channels/http_api.py | grep -E 'session\.|_save|\.close|\.reset|\.write|memory'
```
Any handler that directly mutates state rather than enqueueing a control message is a race condition with the message loop.

**Check (Stage 6):**
For each state-mutating HTTP endpoint: can a concurrent message be mid-processing when the endpoint fires? If the message loop is the serialization point, all mutations must go through it.

---

## P-029: Tool output truncation without signal

**Origin:** Architecture review — character-based truncation can break structured output (JSON, code). Model reasons over incomplete data without knowing it's incomplete.

**Class:** Any truncation of tool output, LLM response, or memory recall that doesn't signal the truncation to the consumer.

**Check (Stage 3):**
Mutation-test the truncation logic in `tools/__init__.py`. Key mutants: removing the truncation marker, changing the truncation boundary logic. Tests must kill these — if the marker can be removed without a test failing, the model's ability to detect truncated output is unverified.

**Check (Stage 1):**
```bash
# Find all truncation sites
grep -rn 'truncat\|max_len\|[:limit]\|output_truncation' --include='*.py' | grep -v test | grep -v __pycache__
```
For each: does the truncated output include a signal (marker text, metadata flag) that tells the consumer it was truncated?

---

## P-030: Log statement without trace_id

**Origin:** Architecture review — no correlation ID exists to link a message's journey from queue entry through delivery. Debugging requires manual timestamp correlation.

**Class:** Any log call in the message processing path that doesn't include the per-message trace_id.

**Check (Stage 1):**
```bash
# After trace_id is implemented, verify all log calls in the processing path include it
grep -rn 'log\.\(info\|warning\|error\|debug\)' lucyd.py agentic.py | grep -v 'trace_id' | grep -v test
```
Every log call in `_process_message()`, `run_agentic_loop()`, tool dispatch, and session persistence should include `trace_id`. Log calls in startup/shutdown are exempt.

**Check (Stage 2):**
After implementing trace_id, add an integration test: send a message through the pipeline, capture log output, verify every log line between queue entry and delivery contains the same trace_id.

---

## P-031: System prompt exceeds model context capacity

**Origin:** Architecture review — no budget enforcement exists for the assembled system prompt. For small-context models (4k-8k), the system prompt can silently consume the entire context window.

**Class:** Any configuration where the total system prompt (workspace files + skills + tool descriptions + recall) exceeds a dangerous percentage of the model's context window, with no warning to the operator.

**Check (Stage 4):**
Contract test: construct a config with a small `max_context_tokens` value and large workspace files. Verify the startup sequence emits a warning. If no warning mechanism exists, this is a finding.

**Check (Stage 7):**
Verify the default `lucyd.toml.example` documents the relationship between system prompt size and model context window. If the comment block from the architecture review isn't present, it's a documentation gap.

---

## P-032: Architectural default tuned for specific deployment

**Origin:** Architecture review — memory priority ordering and context budget behavior were justified with "fits Lucy" rather than evaluated as framework defaults.

**Class:** Any configurable default where the chosen value only makes sense for a specific deployment type (companion, business, local, cloud) without documentation explaining the tradeoff.

**Check (Stage 7):**
For each `[section]` in `lucyd.toml.example` that has tunable behavior parameters:
```bash
grep -B2 -A2 'priority\|threshold\|budget\|limit\|timeout\|window' lucyd.toml.example
```
Does each parameter have a comment explaining what it controls, why the default was chosen, and when an operator should change it? Defaults without context are invisible tradeoffs.

---

## P-033: Notification-to-LLM cost amplification

**Origin:** Production incident 2026-03-09 — HR chest strap sent telemetry every 5 seconds via `/notify`. Each notification triggered a full agentic loop (~$0.05 per call × 17,280 calls/day = $54/day). No audit stage caught it because every individual LLM call was correctly tracked (P-027 passed), data structures were bounded (P-018 passed), and HTTP rate limiting protected against abuse (Stage 6 passed). The cost was legal per-message spending that added up to an expensive day because nobody asked "how often will this fire?"

**Class:** Any notification pathway (`/notify`, `--notify`, FIFO system events) that can receive high-frequency external inputs (>1/minute) without cost bounding at the LLM invocation level. HTTP rate limiting protects against abuse — it doesn't protect against legitimate high-frequency sources (IoT sensors, monitoring systems, health probes, n8n polling workflows). The cost is `frequency × cost_per_call × uptime`. A 5-second interval at $0.05/call is $864/day.

**Fix:** `passive_notify_refs` config — notification refs matching the list are buffered at latest-value-per-ref without triggering an LLM call. Buffered entries are injected as `[telemetry: ...]` context on the next real message. `data.priority = "active"` bypasses the buffer for urgent notifications.

**Check (Stage 4):**
Contract tests for the passive telemetry buffer:
1. Passive ref → message buffered, NOT queued for LLM processing
2. Active priority → bypasses buffer, processed normally
3. Non-passive ref → not buffered, processed normally
4. Stale entries (>30s) → drained and discarded
5. Multiple refs → each buffered independently at latest value
```bash
# Verify passive telemetry tests exist and cover all paths
grep -rn 'TestPassiveTelemetry\|TestDrainTelemetry\|passive_notify' tests/ --include='*.py'
```

**Check (Stage 5):**
For each notification source in the dependency chain data flow matrix, annotate expected frequency:
```
| Source | Frequency | Buffered? | Daily cost at frequency |
```
Any source with frequency >1/minute that is NOT in `passive_notify_refs` is a cost risk. Calculate: `frequency × estimated_cost_per_call × 86400 / interval_seconds`. If daily cost exceeds $1, flag for review.

**Check (Stage 7):**
Verify `docs/configuration.md` and `docs/operations.md` document:
1. `passive_notify_refs` — what it does, how to configure
2. `primary_sender` — how notification routing works
3. Cost implications of high-frequency notification sources (in `/notify` endpoint docs or a dedicated section)
4. The `data.priority = "active"` bypass mechanism

---

## P-034: Passive telemetry buffer silent failure

**Origin:** Same incident, prevention layer — the passive telemetry buffer (`passive_notify_refs` + `_drain_telemetry()`) is a critical cost-saving mechanism. If the buffer logic breaks silently (e.g., a refactor removes the `continue` in `_message_loop`, or the `ref` field extraction path changes), notifications fall through to the LLM at full frequency. No error, no warning — just a $54/day cost spike that looks like normal operation in the logs.

**Class:** A cost optimization mechanism that, when it fails, produces no error signal — only a cost increase. The failure mode is indistinguishable from "the agent is busy today" until the operator checks the bill.

**Check (Stage 3):**
Mutation-test the passive telemetry interception in `lucyd.py`:
```bash
# Key mutation targets:
# 1. The `continue` statement after buffering (removing it = fall through to LLM)
# 2. The `ref in self._passive_refs` check (inverting it = all refs buffered or none)
# 3. The priority bypass check (removing it = urgent messages also buffered)
# 4. The `_drain_telemetry` max_age check (removing it = stale data injected)
grep -n 'passive_refs\|_telemetry_buffer\|_drain_telemetry\|continue' lucyd.py
```
All four mutations must be killed by existing tests. If the `continue` removal survives, the cost optimization is unverified.

**Check (Stage 4):**
Structural invariant: the passive telemetry interception MUST happen before the message enters the processing queue (debounce → session → agentic loop). If it happens after, the LLM call has already been made.
```bash
# Verify the passive check is in _message_loop, not in _process_message
grep -n 'passive_refs\|_telemetry_buffer' lucyd.py
```
Results should show the check in `_message_loop` (before `_process_message` is called). If it appears only in `_process_message`, the interception is too late.

---

## Architectural Invariants

Unlike bug patterns (which describe a class of defect found in existing code), architectural invariants describe rules that ALL code must follow. A violation isn't a recurring bug — it's a new piece of code that broke an existing rule.

Invariants are checked in Stage 1 (as greps) and Stage 4 (as contract tests). New code that violates an invariant is a finding even if it works correctly in isolation.

### AI-001: All LLM calls produce cost records
Every call to provider.complete(), transcription APIs, embedding APIs, and synthesis calls must record cost in cost.db. No exceptions.
Checked by: P-027

### AI-002: All state mutations route through the message queue
Session state, memory DB, and workspace files must only be modified by code running inside _process_message() or functions called from it. HTTP endpoints and external triggers must enqueue control messages, not mutate directly.
Checked by: P-028

### AI-003: All truncation signals the consumer
When any output (tool result, memory recall, LLM response) is truncated, the truncated content must include a machine-readable signal. The consumer must never receive silently incomplete data.
Checked by: P-029

### AI-004: All message-path logging includes trace_id
Every log call in the path from queue dequeue to channel delivery must include the per-message trace_id. Startup, shutdown, and background task logging are exempt.
Checked by: P-030

### AI-005: Single provider architecture
One `[models.primary]` config, one `self.provider` instance, used for all LLM operations (messages, sub-agents, compaction, consolidation, synthesis). No model routing, no model selection based on message source, no tier-based context stripping. Embeddings (`[models.embeddings]`) are the sole exception — fundamentally different API type (vectors, not chat).

**Origin:** Refactoring 2026-03-06 — retired multi-model routing (`route_model()`, `self.providers` dict, `[routing]` config) and context tier system (`full`/`operational`/`minimal`). Both added complexity without value — all routes pointed to the same model, all tiers should deliver the full persona.

**Violations (grep checks for Stage 1):**
```bash
# Multi-provider re-introduction
grep -rn 'self\.providers\b' --include='*.py' | grep -v test | grep -v __pycache__ | grep -v .venv

# Model routing re-introduction
grep -rn 'route_model\|model_override\|_default_model' --include='*.py' | grep -v test | grep -v __pycache__ | grep -v .venv | grep -v tools/tts.py

# Context tier re-introduction
grep -rn 'context_tiers\|tier_overrides\|_files_for_tier' --include='*.py' | grep -v test | grep -v __pycache__ | grep -v .venv

# Separate model configs for non-embedding operations
grep -rn 'compaction_model\|consolidation_model\|subagent_model\|all_model_names' --include='*.py' | grep -v test | grep -v __pycache__ | grep -v .venv
```
Expected: zero matches on all four. Any match means the single-provider architecture is being violated.

**Contract test (Stage 4):** `_process_message()` signature must not accept `tier` or `model_override` parameters. Sub-agent tool (`tool_sessions_spawn`) must not accept `model` parameter.

### AI-006: High-frequency notifications must be cost-bounded
Every notification pathway that can receive messages at frequencies above 1/minute from external sources must have cost bounding. Acceptable mechanisms: `passive_notify_refs` buffering (zero LLM cost), application-level rate limiting at the source, or explicit operator documentation of expected cost at the configured frequency. A notification source with no cost bounding that fires >1/minute is a finding, regardless of whether it's currently connected.

**Origin:** Production incident 2026-03-09 — HR telemetry at 5-second intervals via `/notify` triggered $54/day in LLM costs. Every individual call was correctly tracked (AI-001 satisfied), correctly queued (AI-002 satisfied), and correctly logged (AI-004 satisfied). No existing invariant covered the amplification pattern because all rules operate at the per-call level, not at the aggregate cost level.

**Violations (grep checks for Stage 1):**
```bash
# Find all notification entry points
grep -rn 'notify\|system.*event\|FIFO\|control.pipe' --include='*.py' | grep -v test | grep -v __pycache__ | grep -v .venv | grep -v audit

# Verify passive_notify_refs config property exists and is used
grep -rn 'passive_notify_refs\|_passive_refs\|_telemetry_buffer' --include='*.py' | grep -v test | grep -v __pycache__
```

**Contract test (Stage 4):** Verify that a message with a passive ref and no active priority does NOT reach `_process_message()`. Verify that a message with active priority DOES reach `_process_message()`. These two tests together prove the buffer is both effective and bypassable.

Checked by: P-033, P-034

The `AI-NNN` namespace separates invariants from bug patterns (`P-NNN`). Invariants are never retired — they're permanent rules. They can only be superseded if the architecture changes.

---

## Pattern Index by Stage

| Stage | Applicable Patterns |
|-------|-------------------|
| 1. Static Analysis | P-001, P-002, P-003 (grep), P-005, P-010, P-014, P-015, P-016, P-018, P-020, P-021, P-022, P-025, P-026 (hotfix tag grep), P-027 (cost tracking grep), P-029 (truncation grep), P-030 (trace_id grep), P-032 (defaults grep), AI-006 (notification entry points grep) |
| 2. Test Suite | P-005 (verify count), P-006 (fixture check), P-013, P-016 (ResourceWarning trigger), P-030 (trace_id integration test) |
| 3. Mutation Testing | P-004, P-013, P-015 (parity check), P-026 (re-raise logic), P-029 (truncation logic), P-034 (telemetry buffer mutations) |
| 4. Orchestrator Testing | P-017, P-023, P-028 (queue bypass), P-031 (context budget contract test), P-033 (telemetry buffer contract tests), P-034 (buffer interception placement), AI-001 through AI-006 (invariant contract tests) |
| 5. Dependency Chain | P-006, P-012, P-014 (failure behavior), P-016 (shutdown path), P-017 (persist order), P-026 (streaming error path), P-027 (cost.db completeness), P-033 (notification frequency annotation) |
| 6. Security Audit | P-003, P-009, P-012, P-018 (resource exhaustion), P-028 (control endpoint audit) |
| 7. Documentation Audit | P-007, P-008, P-011, P-020 (config-to-default parity), P-021 (provider split), P-024, P-031 (context budget docs), P-032 (default documentation), P-033 (notification cost docs) |
| Aggregate Report | P-019 (gap verification) |

---

## Changelog

| Date | Pattern | Event |
|------|---------|-------|
| 2026-02-17 | P-001 | Added from Cycle 1 Stage 1 (memory.py zip) |
| 2026-02-17 | P-002 | Added from Cycle 1 Stage 1 (agentic.py BaseException) |
| 2026-02-17 | P-006 | Added from Cycle 1 post-audit review (dead indexer pipeline) |
| 2026-02-17 | P-007 | Added from Cycle 1 Stage 6 (stale test count) |
| 2026-02-18 | P-001 | Recurred in Cycle 2 Stage 1 (indexer.py zip, test_context.py zip) |
| 2026-02-18 | P-003 | Added from Cycle 2 Stage 6 (TTS output_file bypass) |
| 2026-02-18 | P-004 | Added from Cycle 2 Stage 3 (_safe_env iteration order) |
| 2026-02-18 | P-005 | Added from Cycle 2 Stage 1 (shadowed TestVectorSearchLimit) |
| 2026-02-18 | P-007 | Recurred in Cycle 2 Stage 7 (855→916) |
| 2026-02-18 | P-008 | Added from Cycle 2 Stage 7 (missing indexer in docs) |
| 2026-02-18 | P-009 | Added from Cycle 2 Stage 6 (stale TTS capability assessment) |
| 2026-02-18 | P-010 | Added from Cycle 1+2 Stage 1 (noqa accumulation) |
| 2026-02-18 | P-011 | Added from Cycle 3 Stage 7 (operations.md "Opus" label was always wrong) |
| 2026-02-19 | P-012 | Added from Cycle 3 Stage 5 (entity_aliases misclassified as manual/static) |
| 2026-02-19 | P-012 | Updated: added ordering invariant check (aliases must store before facts) |
| 2026-02-19 | P-013 | Added from Cycle 3 Stage 3 (recall() vector path untested via None default) |
| 2026-02-22 | P-014 | Added from production hardening retrospective (provider.complete() no error handling) |
| 2026-02-22 | P-015 | Added from production hardening retrospective (Anthropic vs OpenAI json.loads parity) |
| 2026-02-22 | P-016 | Added from production hardening retrospective (memory_conn + httpx client never closed) |
| 2026-02-22 | P-017 | Added from production hardening retrospective (compaction state persisted after event log) |
| 2026-02-22 | P-018 | Added from production hardening retrospective (_last_inbound_ts unbounded dict) |
| 2026-02-22 | — | Added Retrospective Protocol, Known Gaps Lifecycle, Pattern Retirement rules |
| 2026-02-25 | P-020 | Added from production hardening retrospective (18 magic numbers across framework) |
| 2026-02-25 | P-021 | Added from production hardening retrospective (OpenAI/Anthropic/ElevenLabs defaults in framework code) |
| 2026-02-26 | P-022 | Added from Cycle 9 interface parity review (channel-specific config paths in framework code) |
| 2026-02-26 | P-023 | Added from Cycle 9 interface parity review (CLI/HTTP API return different data schemas) |
| 2026-02-26 | P-024 | Added from Cycle 9 post-audit review (HTTP endpoints missing response schemas, error codes, rate limits) |
| 2026-02-26 | P-025 | Added from Cycle 10 Stage 5 (indexer.py default parameter binding captures stale module global). FIXED same cycle. |
| 2026-02-27 | P-026 | Added from production incident (Anthropic SDK mid-stream SSE overloaded_error bypassed retry system). HOTFIXED in anthropic_compat.py. |
| 2026-03-02 | P-027 | Added from architecture review (LLM calls bypassing cost tracking — compaction, consolidation, synthesis, STT) |
| 2026-03-02 | P-028 | Added from architecture review (/sessions/reset bypassing message queue — race condition) |
| 2026-03-02 | P-029 | Added from architecture review (tool output truncation without signal to consumer) |
| 2026-03-02 | P-030 | Added from architecture review (log statements without trace_id correlation) |
| 2026-03-02 | P-031 | Added from architecture review (system prompt exceeding model context capacity without warning) |
| 2026-03-02 | P-032 | Added from architecture review (architectural defaults tuned for specific deployment without documentation) |
| 2026-03-02 | AI-001–AI-004 | Added Architectural Invariants section — permanent rules for all code |
| 2026-03-02 | — | Updated Retrospective Protocol — added question 5 (architectural invariant check) |
| 2026-03-06 | AI-005 | Added single provider architecture invariant — guards against multi-model routing re-introduction |
| 2026-03-09 | P-033 | Added from production incident retrospective (HR telemetry $54/day — notification-to-LLM cost amplification) |
| 2026-03-09 | P-034 | Added from production incident retrospective (passive telemetry buffer silent failure mode) |
| 2026-03-09 | AI-006 | Added high-frequency notification cost bounding invariant — guards against unbounded notification amplification |
