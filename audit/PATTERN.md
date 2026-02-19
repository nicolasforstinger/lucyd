# Audit Patterns

Accumulated from findings across audit cycles. Every pattern traces to a specific finding that was missed or nearly missed. The auditor reads this file before every stage and checks each applicable pattern.

Patterns are not stage-specific — a pattern discovered in Stage 6 may require a check in Stage 1.

## How to Use

Before each stage, scan this file for patterns tagged with that stage. Run the check described. If the check finds a new instance, it's a finding. If the pattern no longer applies (code removed, architecture changed), note it in the report and mark the pattern as retired.

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
  grep -n '^class ' "$f" | awk -F'[: (]' '{print $2}' | sort | uniq -d | while read cls; do
    echo "DUPLICATE: $cls in $f"
  done
done
```
Any output is a finding — a test is being silently dropped.

Also check function names:
```bash
for f in tests/test_*.py; do
  grep -n '^\s*def test_' "$f" | awk -F'def |(' '{print $2}' | sort | uniq -d | while read fn; do
    echo "DUPLICATE: $fn in $f"
  done
done
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
For each model reference in documentation: trace to the actual model string in config. Verify the human-readable label matches. Also check cost rates, tier labels, and any other derived descriptions that could drift when config changes.

---

## P-012: Auto-populated pipeline misclassified as static

**Origin:** Cycle 3 Stage 5 — `entity_aliases` table classified as "Manual SQL (backfill) — not auto-populated" in the dependency chain report, and "admin-managed" in the security report. In reality, `consolidation.py:279-287` auto-populates aliases via `INSERT OR IGNORE` on every fact extraction call. All three producer paths (cron, pre-compaction, session close) converge on `extract_facts()` which stores aliases. The misclassification was carried forward to Stage 6 unchallenged.

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

## Pattern Index by Stage

| Stage | Applicable Patterns |
|-------|-------------------|
| 1. Static Analysis | P-001, P-002, P-003 (grep), P-005, P-010 |
| 2. Test Suite | P-005 (verify count), P-006 (fixture check), P-013 |
| 3. Mutation Testing | P-004, P-013 |
| 4. Orchestrator Testing | — |
| 5. Dependency Chain | P-006, P-012 |
| 6. Security Audit | P-003, P-009, P-012 |
| 7. Documentation Audit | P-007, P-008, P-011 |

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
