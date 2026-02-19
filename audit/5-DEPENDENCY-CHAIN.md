# 5 — Dependency Chain Audit

**What:** Map every data pipeline end-to-end — producer to consumer — and verify that every link in the chain exists, runs, and produces fresh data. A module that reads from a data source is useless if nothing writes to that data source. Test fixtures mask this gap by simulating populated stores.

**Why:** Stages 1–4 verify that existing code works correctly. Stage 6 verifies security boundaries. Stage 7 verifies documentation. None of them ask:

**"Is anything missing?"**

This stage answers that question. It catches the class of bug where a system reads from a source that nothing writes to — the exact pattern that caused the 8-day memory indexer gap (Feb 10–18, 2026). Every other stage passed because test fixtures simulated a populated database.

**When to run:** After Stage 4 (Orchestrator Testing), before Stage 6 (Security Audit). The security audit needs the dependency chain to be verified first — an unmapped dead pipeline is a false sense of capability, which affects threat model accuracy.

---

## How to Think

You are mapping plumbing, not testing valves. Stages 2–4 test that each valve works. This stage asks: are all the pipes connected?

For every module that reads data from a persistent store, ask two questions:
1. **Who writes to this store?** (Producer identification)
2. **Is that writer actually running?** (Producer verification)

If either answer is "nobody" or "no," you've found a dead pipeline — a consumer with no producer. This is invisible to unit tests because fixtures simulate the populated state.

**Confidence gate:** Before declaring any pipeline "healthy," reach 90% confidence that:
1. You've identified the correct producer
2. The producer actually runs on the live system
3. The data is fresh (not stale from a previous era)

---

## Pattern Checks

**Before starting Phase 1, run all pattern checks applicable to this stage.** Read `audit/PATTERN.md` for full context on each pattern. Report results in the stage report under a "Pattern Checks" section.

If `audit/PATTERN.md` does not exist (first audit cycle), skip this section.

### P-006: Dead data pipeline (producer removed, consumer remains)
This pattern is the entire reason this stage exists. During Phase 1, for every consumer in the data flow matrix, verify the producer exists, is enabled, and has run recently. Pay special attention to:
- Test fixtures that pre-populate databases — in production, what writes to this database?
- Modules that read from SQLite — is the indexer/writer still present and running?
- Any pipeline where the producer was recently added, removed, or refactored

If a test fixture simulates data that no production process creates, that's the exact gap this stage is designed to catch.

### P-012: Auto-populated pipeline misclassified as static
For every pipeline classified as "Manual," "N/A (static)," or "admin-managed" in the data flow matrix, verify the classification against source code. Grep for automated write operations on that table or file:
```bash
# For each "manual" pipeline, check if any automated process actually writes to it
grep -rn "INSERT.*INTO.*<table_name>" --include='*.py' | grep -v test | grep -v __pycache__
```
If any automated producer exists, the classification is wrong. This pattern caught `entity_aliases` being misclassified as manual when `consolidation.py:extract_facts()` auto-populates it on every run.

Additionally, verify that extraction prompts contain anti-fragmentation directives (e.g., "use the shortest common name" for entities). If a prompt edit drops this, entity fragmentation silently returns — new facts scatter across `nicolas_forstinger`, `lucy_belladonna`, etc. instead of resolving to canonical short names. The prompt is a pipeline input; treat prompt regressions as pipeline breaks.

---

## Phase 1: Data Flow Mapping

### Method

1. Identify every module that reads from a persistent store (SQLite, filesystem, JSONL, TOML, named pipe).
2. For each read path, trace the corresponding write path: what process populates that store?
3. Build the producer-consumer matrix.

### How to Find Consumers

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
[ -f lucyd.py ] || { echo "ERROR: Not in project root."; exit 1; }
source .venv/bin/activate

# SQLite reads
grep -rn "sqlite3\.\|\.execute(" *.py tools/ channels/ --include="*.py" | grep -v test | grep -v __pycache__

# File reads (filesystem consumers)
grep -rn "read_text\|open.*\"r\"\|Path.*read" *.py tools/ channels/ --include="*.py" | grep -v test | grep -v __pycache__

# JSONL reads
grep -rn "json\.loads\|jsonl\|\.jsonl" *.py tools/ channels/ --include="*.py" | grep -v test | grep -v __pycache__

# Config/TOML reads
grep -rn "toml\.\|\.toml" *.py tools/ channels/ --include="*.py" | grep -v test | grep -v __pycache__

# Named pipe / FIFO reads
grep -rn "mkfifo\|O_RDONLY\|control\.pipe" *.py --include="*.py" | grep -v test
```

### Matrix Format

```
+-----------------+--------------------------+-----------------------+----------------+
|     Consumer    |       Data Source         |       Producer        | Producer Runs? |
+-----------------+--------------------------+-----------------------+----------------+
| module.function | file/db/table             | module.function/cron  | Yes/No/N/A     |
+-----------------+--------------------------+-----------------------+----------------+
```

### Verification

For each row:
- **Producer blank** = FINDING (dead consumer, no data source)
- **Producer Runs? = No** = FINDING (broken pipeline)
- **Producer Runs? = N/A** = Acceptable only for static config files edited manually
- **Producer Runs? = Yes** = Verify in Phase 2 and Phase 3

### Known Pipelines to Map

At minimum, trace these (expand as codebase grows):

| Consumer | Data Source | Expected Producer |
|----------|-----------|-------------------|
| `memory.py` search/recall | `memory/main.sqlite` (chunks, chunks_fts, embedding_cache) | `tools/indexer.py` via `bin/lucyd-index` cron |
| `session.py` load | `sessions/*.jsonl` | `session.py` save (daemon) |
| `context.py` build | `workspace/*.md` | Lucy via tools / manual |
| `config.py` load | `lucyd.toml`, `providers.d/*.toml` | Manual / Claudio (static) |
| `skills.py` load | `workspace/skills/*.md` | Manual (static) |
| `tools/status.py` cost query | `cost.db` (table: `costs`) | `agentic.py` `_record_cost()` |
| `lucyd.py` PID check | `~/.lucyd/lucyd.pid` | `lucyd.py` daemon startup |
| `lucyd.py` FIFO reader | `~/.lucyd/control.pipe` | `bin/lucyd-send` and cron jobs |
| Daily memory logs | `workspace/memory/YYYY-MM-DD.md` | Lucy via `write` tool (conversational, non-deterministic) |
| `lucyd.py` monitor reader | `~/.lucyd/monitor.json` | `lucyd.py` `_process_message` (daemon) |
| `memory.py` structured recall → `lookup_facts()` | `memory/main.sqlite` (`facts` table) | `consolidation.py` (cron :15, pre-compaction, session close) + `tools/structured_memory.py` (`memory_write` agent tool) |
| `memory.py` structured recall → `search_episodes()` | `memory/main.sqlite` (`episodes` table) | `consolidation.py` (cron :15, session close) |
| `memory.py` structured recall → `get_open_commitments()` | `memory/main.sqlite` (`commitments` table) | `consolidation.py` + `tools/structured_memory.py` (`commitment_update` agent tool) |
| `memory.py` structured recall → `resolve_entity()` | `memory/main.sqlite` (`entity_aliases` table) | `consolidation.py` `extract_facts()` — auto-populated via LLM extraction on every consolidation run (see P-012). **Ordering invariant:** aliases are stored BEFORE facts in `extract_facts()`. If reversed, new entities in the same extraction batch won't resolve through aliases. Verify ordering on any refactor of the extraction pipeline. |
| `consolidation.py` skip check | `memory/main.sqlite` (`consolidation_state` table) | `consolidation.py` `update_consolidation_state()` |
| `consolidation.py` hash check | `memory/main.sqlite` (`consolidation_file_hashes` table) | `consolidation.py` `extract_from_file()` |

**Note on non-deterministic producers:** Some data sources (daily memory logs, MEMORY.md) are written by Lucy as a conscious choice during conversations — there is no cron job or automatic process. Their freshness depends on conversation activity. Flag these separately from deterministic pipelines (cron, daemon auto-writes).

---

## Phase 2: External Process Inventory

### Method

List every process the system depends on that is not the daemon itself. Sources:

```bash
# Cron jobs
crontab -l | grep -v "^#" | grep -v "^$"

# Systemd units
systemctl list-units --type=service | grep lucyd
systemctl is-enabled lucyd

# Documented processes (cross-reference CLAUDE.md, operations.md)
grep -r "cron\|systemd\|timer\|scheduled\|hourly\|daily" docs/ README.md 2>/dev/null
```

### Expected External Processes

Build this table from documentation and config:

| Process | Type | Schedule | Output | Status |
|---------|------|----------|--------|--------|
| `lucyd.service` | systemd | continuous | daemon | |
| Memory indexer (`lucyd-index`) | cron | `10 * * * *` | `memory/main.sqlite` | |
| Memory consolidation (`lucyd-consolidate`) | cron | `15 * * * *` | `memory/main.sqlite` (facts, episodes, commitments, aliases) | |
| Memory maintenance (`lucyd-consolidate --maintain`) | cron | `0 4 * * *` | dedup, decay, cleanup in `memory/main.sqlite` | |
| Workspace auto-commit | cron | `0 * * * *` | git commits | |
| Code auto-commit | cron | `5 * * * *` | git commits | |
| Heartbeat | cron | (check if enabled/disabled) | system message | |

### Verification

For each expected external process:

| Check | Method | Finding if... |
|-------|--------|---------------|
| Exists | `which`, `systemctl status`, `crontab -l` | Process not found |
| Enabled | `systemctl is-enabled` or crontab entry present and uncommented | Disabled or commented out |
| Last run | `journalctl -u lucyd --since "1 hour ago"`, log files, cron output | Never ran or failed |
| Output fresh | Phase 3 freshness check on its output | Stale output |

Any documented process that doesn't exist on the system is a FINDING.
Any process that exists but hasn't run successfully is a FINDING.
Any process intentionally disabled must be documented as such (not silently absent).

---

## Phase 3: Freshness Checks

### Method

For every data store that should be continuously populated (not static config), verify recent writes.

### Freshness Queries

```sql
-- Memory DB: most recent indexed chunk
SELECT path, updated_at FROM chunks ORDER BY updated_at DESC LIMIT 1;
-- Threshold: 48 hours. Beyond = stale.

-- Cost DB: most recent cost entry
SELECT timestamp FROM costs ORDER BY timestamp DESC LIMIT 1;
-- Threshold: 24 hours (if daemon is running). Beyond = stale.

-- Structured memory: most recent fact
SELECT entity, attribute, updated_at FROM facts WHERE valid = 1 ORDER BY updated_at DESC LIMIT 1;
-- Threshold: matches last consolidation run. If cron runs at :15, should be within 2 hours.

-- Structured memory: consolidation state (has the pipeline run?)
SELECT session_file, consolidated_at FROM consolidation_state ORDER BY consolidated_at DESC LIMIT 1;
-- Threshold: should match last session activity. If sessions exist that aren't in consolidation_state, pipeline is behind.

-- Structured memory: episodes
SELECT title, created_at FROM episodes ORDER BY created_at DESC LIMIT 1;
-- Threshold: 48 hours (depends on conversation activity).

-- Structured memory: commitments
SELECT description, updated_at FROM commitments WHERE status = 'open' ORDER BY updated_at DESC LIMIT 1;
-- Threshold: informational only — stale open commitments may be legitimate.
```

```bash
# Session files: most recent write
ls -lt ~/.lucyd/sessions/ | head -5
# Threshold: should match last conversation timestamp

# Memory daily logs: most recent file
ls -lt ~/.lucyd/workspace/memory/*.md | head -3
# Threshold: 72 hours (conversational — Lucy writes these when she chooses)

# PID file: current?
cat ~/.lucyd/lucyd.pid && ps -p $(cat ~/.lucyd/lucyd.pid)
# Finding if PID file exists but process doesn't

# Indexer lock file: stale?
ls -la ~/.lucyd/lucyd-index.lock 2>/dev/null
# Finding if lock file exists but no indexer process running (stale lock)
```

### Freshness Thresholds

| Data Source | Threshold | Rationale |
|-------------|-----------|-----------|
| Memory SQLite (chunks) | 48h | Indexer runs hourly via cron |
| Cost SQLite (`costs`) | 24h | Every API call logs cost (if daemon is running) |
| Session JSONL | Matches last conversation | Written on every message |
| Daily memory logs | 72h | Written by Lucy during conversations (non-deterministic) |
| PID file | Current process | Stale PID = unclean shutdown |
| Monitor JSON | 5 min (if daemon running) | Written on every `_process_message` |
| Structured memory (`facts`) | 2h | Consolidation cron runs at :15 every hour |
| Structured memory (`episodes`) | 48h | Extracted from sessions during consolidation |
| Structured memory (`consolidation_state`) | Matches session activity | One entry per processed session |
| Structured memory (`consolidation_file_hashes`) | Matches workspace changes | Updated when markdown files change |

Any data source beyond its freshness threshold is a FINDING. For non-deterministic sources (daily memory logs), check whether the daemon had conversations in the threshold period before flagging — no conversations = no logs is expected behavior, not a broken pipeline.

---

## Phase 4: Round-Trip Test Verification

### Method

For every data pipeline identified in Phase 1, verify that a round-trip integration test exists in the test suite:

```
Write -> Store -> Read -> Assert content matches
```

Check `test_*.py` files for end-to-end coverage of each pipeline. A unit test that mocks the store does NOT count — the test must exercise the actual write and read path against a real (temporary) store.

### How to Check

```bash
# For each pipeline, search for tests that cover both the write and read side
# Example: memory pipeline
grep -l "index_workspace\|update_chunks" tests/test_*.py
grep -l "memory_search\|_search_fts\|_search_vector" tests/test_*.py
# If both exist in SEPARATE files only, there may be no round-trip test
```

### Minimum Required Round-Trips

| Pipeline | Test Must Cover |
|----------|----------------|
| Memory: write file -> index -> search | `index_workspace()` -> `memory_search()` finds content |
| Session: save -> load | `save()` -> `load()` preserves messages + metadata |
| Context: write file -> build includes it | Write to workspace -> `context_builder.build()` includes content |
| Cost: log cost -> status reads it | `_record_cost()` -> `tool_session_status()` shows cost |
| Structured memory: consolidate -> recall | `extract_facts()` -> `lookup_facts()` returns extracted fact |
| Structured memory: agent write -> recall | `memory_write()` tool -> `lookup_facts()` returns written fact |
| Structured memory: episodes | `extract_episodes()` -> `search_episodes()` returns episode |
| Structured memory: commitments | `extract_commitments()` -> `get_open_commitments()` returns commitment |
| Structured memory: aliases | `extract_facts()` (with multi-name entity) -> `resolve_entity()` resolves alias |

Missing round-trip test = FINDING (add to Stage 2 remediation).

---

## Phase 5: Report

Write the report to `audit/reports/5-dependency-chain-report.md`:

```markdown
# Dependency Chain Audit Report

**Date:** [date]
**EXIT STATUS:** PASS / PARTIAL / FAIL

## Data Flow Matrix
| Consumer | Data Source | Producer | Producer Runs? | Status |
|----------|-----------|----------|---------------|--------|

## External Process Inventory
| Process | Type | Schedule | Expected Output | Exists? | Enabled? | Last Run | Status |
|---------|------|----------|----------------|---------|----------|----------|--------|

## Freshness Checks
| Data Source | Threshold | Last Write | Fresh? |
|-------------|-----------|-----------|--------|

## Round-Trip Test Coverage
| Pipeline | Test Exists? | Test File | Status |
|----------|-------------|-----------|--------|

## Findings
| # | Phase | Severity | Description | Remediation |
|---|-------|----------|-------------|-------------|

## Confidence
[Per-phase confidence + overall]
```

---

## Exit Criteria

**PASS:** All data pipelines have active producers. All external processes exist and run. All data sources within freshness thresholds. All round-trip tests exist.

**PARTIAL:** Findings exist but are LOW severity (e.g., freshness slightly beyond threshold for non-deterministic source, non-critical pipeline gap). Document and defer.

**FAIL:** Any critical data pipeline has no producer, or a security-relevant pipeline lacks round-trip test coverage. Blocks proceeding to Stage 6.

---

## Relationship to Other Stages

| Stage | What it catches | What it misses (that Stage 5 catches) |
|-------|----------------|---------------------------------------|
| 2 (Test Suite) | Functions work with test data | Test fixtures mask missing producers |
| 3 (Mutation) | Security functions kill mutants | Mocked data hides empty pipelines |
| 4 (Orchestrator) | Decision logic is correct | Doesn't trace data origin |
| 6 (Security) | Attack surface boundaries | Doesn't ask "is the data pipeline complete?" |
| 7 (Documentation) | Docs match source | Source itself may be incomplete |

This is the only stage that asks: **"For every module that reads data, does something write that data?"**
