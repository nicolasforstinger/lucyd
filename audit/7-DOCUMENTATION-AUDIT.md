# 7 — Documentation Audit

**What:** Verify every claim in every public-facing document against the actual source code. Fix discrepancies. Ensure docs are complete (nothing exists in source that isn't documented) and accurate (nothing is documented that doesn't exist in source).

**Why:** Documentation drifts from source silently. A tool gets added but TOOLS.md isn't updated. A config option gets renamed but the example file still has the old name. README says "13 tools" when there are 16. These gaps erode trust — if the docs are wrong about tool count, what else is wrong?

For Lucyd specifically: this is an MIT-licensed project. The docs ARE the user interface for anyone evaluating, deploying, or contributing. Wrong docs waste people's time and damage credibility.

**When to run:** After adding tools, channels, providers, or config options. Before any public release. During full audit (Stage 7). When Nicolas says "update the docs."

---

## How to Think

You are an auditor. Your job is to find discrepancies between source code and documentation.

**Ground truth is ALWAYS the source code.** Not your memory. Not the existing docs. Not what makes sense. Read the actual `.py` files, `.toml` files, and module definitions. If the source says the function takes 3 parameters, the docs say 2, the docs are wrong — even if 2 looks right.

**Trace, don't assume.** For every claim in a doc file (tool count, feature name, config key, env var, CLI flag), find the source code line that proves it. If you can't find the source line, the claim is either wrong or undocumented.

**Be exhaustive within scope.** Don't update 3 of 5 stale sections and call it done. If you're auditing a file, audit the ENTIRE file.

**Never assume a doc claim is correct because it looks reasonable.** "13 agent tools" looked reasonable. It was wrong — there were 16. "thinking_mode = enabled" looked reasonable. It was wrong — the valid value is "budgeted." Every claim gets traced to source, no matter how plausible it seems. The claims that look most correct are the ones most likely to survive unverified.

**Confidence gate:** Before changing any doc content, reach 90% confidence that:
1. The source code says what you think it says
2. Your change accurately reflects the source
3. The change doesn't contradict other doc files

After every change, immediately check: did I introduce a new inconsistency?

---

## Pattern Checks

**Before starting Phase 1, run all pattern checks applicable to this stage.** Read `audit/PATTERN.md` for full context on each pattern. Report results in the stage report under a "Pattern Checks" section.

If `audit/PATTERN.md` does not exist (first audit cycle), skip this section.

### P-007: Documentation drift on test counts
```bash
# Extract claimed test count from README
grep -i 'test' README.md | grep -oP '\d{3,}'

# Compare with actual
python -m pytest --collect-only -q 2>/dev/null | tail -1
```
If they differ, update. Also check any per-layer breakdown — subtotals must sum to the stated total.

### P-008: New module without documentation
```bash
# Compare documented modules against actual source files
find tools/ channels/ providers/ -name '*.py' ! -name '__init__.py' | sort > /tmp/actual_modules.txt
grep -oP '`[a-z_/]+\.py`' docs/architecture.md | tr -d '`' | sort > /tmp/documented_modules.txt
diff /tmp/actual_modules.txt /tmp/documented_modules.txt
```
Any module in actual but not documented is a finding. Also compare cron jobs:
```bash
crontab -l | grep lucyd
# Compare against docs/operations.md cron table
```

### P-011: Config-to-doc label consistency
```bash
# Check model names in config vs docs
grep -r 'model\s*=' providers.d/*.toml | grep -v '#'
grep -ri 'opus\|sonnet\|haiku' docs/ README.md
```
Verify that every model reference in documentation matches the actual model string in the config files. Check parenthetical labels like "(Opus)" or "(Sonnet)" against the actual model string. Config files are ground truth.

### P-020: Config-to-default parity
```bash
# Extract all config.py property defaults
grep -A2 'def [a-z_]*.*self.*:' config.py | grep 'default='

# Compare against lucyd.toml.example values
cat lucyd.toml.example
```
For each `config.py` property: is the default value documented in `lucyd.toml.example`? If a property exists in `config.py` but the setting is missing from the example file, operators won't know it exists. If the example file states a value that differs from the `config.py` default, the documentation is misleading.

### P-021: Provider split in documentation
```bash
# Check for provider-specific values in lucyd.toml.example
grep -in 'openai\|anthropic\|elevenlabs\|eleven_\|api\.openai' lucyd.toml.example

# Check provider files have their own settings
cat providers.d/*.toml.example
```
Verify that `lucyd.toml.example` contains only framework settings (provider-agnostic). Provider-specific values (model names, API URLs, provider capabilities like `supports_vision`) belong in `providers.d/*.toml.example`. If a provider-specific value appears in `lucyd.toml.example`, it should be clearly marked as deployment-specific (e.g., TTS api_url with a comment that it's provider-dependent).

---

## Phase 1: Source Inventory

**Why:** Build the ground truth from source code. Everything else is checked against this.

### 1a: Tools

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
[ -f lucyd.py ] || { echo "ERROR: Not in project root."; exit 1; }
source .venv/bin/activate

# Find all tool definitions — tools are registered via TOOLS lists, not function names
# This is the authoritative tool count
grep -rn "^TOOLS\s*=" tools/*.py | grep -v test | grep -v __pycache__
grep -rn '"name":' tools/*.py | grep -v test | grep -v __pycache__
```

For each tool found, record:
```
Tool name: ___
Source file: ___
Function: ___
Brief description (from docstring): ___
Category (if applicable): ___
```

Count them. This is the authoritative tool count.

### 1b: Channels

```bash
find channels/ -name "*.py" -not -name "__init__.py" -not -path "*__pycache__*"
```

For each:
```
Channel name: ___
Source file: ___
Protocol: ___
```

### 1c: Providers

```bash
ls providers.d/ 2>/dev/null
grep -rn "class.*Provider\|def complete" providers/ 2>/dev/null
```

For each:
```
Provider name: ___
Config file: ___
Models: ___
```

### 1d: Configuration

```bash
# All config keys accessed in source
grep -rn "config\.\|\.get(\|\.raw(\|config\[" lucyd.py channels/ tools/ | grep -v test | grep -v __pycache__ | sort -u

# All environment variables
grep -rn "os\.environ\|os\.getenv\|LUCYD_" lucyd.py channels/ tools/ | grep -v test | grep -v __pycache__ | sort -u
```

For each config section/key:
```
Section: ___  Key: ___  Type: ___  Default: ___  Source: ___
```

For each environment variable:
```
Name: ___  Purpose: ___  Required when: ___  Source: ___
```

### 1e: CLI Utilities

```bash
ls bin/ 2>/dev/null
for f in bin/*; do head -5 "$f" && echo "---"; done
```

For each:
```
Name: ___  Purpose: ___  Key flags: ___
```

### 1f: Features

Read through the main modules and list every user-facing feature:

Sources to check:
- `lucyd.py` — daemon features (compaction, monitoring, debounce, cost tracking)
- `channels/` — communication features (Telegram, HTTP API, CLI)
- `tools/` — agent capabilities (all tools)
- `agentic.py` — loop features (tool use, thinking, cost limits)
- Config — configurable behaviors

For each:
```
Feature: ___  Source evidence: ___  User-facing: yes/no
```

**Confidence check:** Is this inventory COMPLETE? Check for features in unexpected places. Are there any capabilities you found in Stages 3-6 (mutation testing, orchestrator testing, dependency chain, security audit) that should be documented?

---

## Phase 2: Document Audit

**Why:** Compare every claim in every doc file against the Phase 1 inventory. Find discrepancies.

### Files to Audit

```bash
# Find all documentation files
find . -name "*.md" -not -path "./.venv/*" -not -path "./mutants/*" -not -path "./tests/*" -not -path "./audit/*" | sort
find . -name "*.example" -o -name "*.example.*" | sort
ls docs/ 2>/dev/null
```

Check each of these (if they exist):
```
README.md
docs/configuration.md
workspace.example/TOOLS.md
workspace.example/ (all files)
lucyd.toml.example
.env.example
lucyd.service.example
providers.d/*.toml (examples)
CHANGELOG.md
LICENSE
```

### Audit Protocol (Per File)

For each file, read line by line and check:

**Counts and lists:**
```bash
# Example: verify tool count in README
grep "agent tools" README.md
# Compare to Phase 1a tool count
```
- "N agent tools" → matches Phase 1a count?
- Tool lists → every tool from 1a present? Any listed that don't exist in source?
- Config key lists → every key from 1d present? Any listed that don't exist?
- Env var tables → every var from 1d present? Any listed that don't exist?

**Feature claims:**
- Every feature mentioned → exists in source (Phase 1f)?
- Every feature from Phase 1f → mentioned in docs?
- Descriptions accurate? Not just present but correct?

**Code examples:**
- Config examples → keys and values match current schema?
- CLI examples → flags and syntax match current implementation?
- Any example that would FAIL if copy-pasted into a real setup?

**Structure descriptions:**
- File/directory references → do those paths exist?
- "Project structure" sections → match actual layout?

### Recording Discrepancies

For each issue:

```markdown
| File | Line | Claim | Reality (from source) | Fix |
|------|------|-------|----------------------|-----|
```

**STOP after auditing each file.** Review:
- Did I check EVERY claim, or did I skim?
- Did I check for MISSING content (features that exist but aren't documented)?
- Are there sections I assumed were correct without verifying against source?

---

## Phase 3: Fix

**Why:** Discrepancies need correction, not documentation.

### Fix Order

1. **Factually wrong** (wrong counts, wrong names, wrong config keys) — Fix immediately
2. **Missing content** (features exist but aren't documented) — Add
3. **Stale content** (references to removed features) — Remove
4. **Misleading examples** (copy-paste would fail) — Fix
5. **Minor wording** — Fix if easy, skip if contentious

### Fix Protocol (Per Discrepancy)

```
1. VERIFY the source one more time (don't trust your inventory — double-check)
2. CHANGE the doc to match source
3. CHECK surrounding context — does the fix create inconsistency with adjacent text?
4. Am I 90% confident this is correct? If not, flag for Nicolas.
```

### Writing Style Rules

- Match the existing document's tone and formatting
- State what the feature does, not how amazing it is
- Keep descriptions concise
- Use the same terminology as the source code
- Config examples must be copy-pasteable
- Don't rewrite correct sections to improve style
- Don't add documentation for internal/private APIs
- Don't document unfinished features or TODOs

---

## Phase 4: Cross-Reference

**Why:** Fixing individual files can create inconsistencies BETWEEN files. A tool count fixed in README must also be fixed in TOOLS.md and configuration.md.

Check each of these across ALL doc files:

```
[ ] Tool count consistent: README = TOOLS.md = configuration.md
[ ] Tool names consistent: same names in all files
[ ] Env vars consistent: .env.example = configuration.md
[ ] Config sections consistent: lucyd.toml.example = configuration.md
[ ] Feature list consistent: README features = actual capabilities
[ ] Provider examples consistent: providers.d/*.toml = configuration.md
[ ] Project structure consistent: README = actual directory layout
[ ] File path references: all mentioned paths actually exist
```

```bash
# Verify file references exist
grep -oP '`[^`]*\.(py|toml|md|json|service|example)`' README.md 2>/dev/null | sort -u | while read f; do
    f=$(echo "$f" | tr -d '`')
    [ -e "$f" ] || echo "MISSING: $f referenced in README.md"
done
```

Any cross-file inconsistency → fix in ALL files.

---

## Phase 5: Verify

```bash
# Tests still pass (no accidental source changes during doc editing)
python -m pytest tests/ -q

# Spot-check key references
echo "=== Tool count ==="
grep "agent tools" README.md
echo "Source count:" $(grep -c '"name":' tools/*.py | grep -v test | awk -F: '{s+=$2}END{print s}')

echo "=== Env vars ==="
grep "LUCYD_" .env.example 2>/dev/null | wc -l
echo "vars in source:" $(grep -rn "LUCYD_" lucyd.py channels/ tools/ | grep -v test | grep "environ\|getenv" | sed 's/.*LUCYD_/LUCYD_/' | sed 's/[^A-Z_].*//' | sort -u | wc -l)
```

---

## Phase 6: Report

Write the report to `audit/reports/7-documentation-audit-report.md`:

```markdown
# Documentation Audit Report

**Date:** [date]
**Duration:** [time]
**EXIT STATUS:** PASS / FAIL

## Source Inventory
| Category | Count |
|----------|-------|
| Tools | |
| Channels | |
| Providers | |
| Config sections | |
| Environment variables | |
| CLI utilities | |
| Features | |

## Files Audited
[List each file with line count]

## Discrepancies Found
| File | Line | Issue | Fix Applied |
|------|------|-------|-------------|

## Cross-Reference Check
| Check | Status |
|-------|--------|
| Tool counts consistent | PASS/FAIL |
| Env vars consistent | PASS/FAIL |
| Config keys consistent | PASS/FAIL |
| Features documented | PASS/FAIL |
| File references valid | PASS/FAIL |

## Fixes Applied
[Summary of changes per file]

## Missing Documentation
[Features or capabilities found in source with no documentation]

## Confidence
[Overall confidence in documentation accuracy: X%]
[Any areas of uncertainty]
```

### Exit Status Criteria

- **PASS:** All discrepancies fixed. Cross-references consistent. No missing documentation for user-facing features. All code examples are copy-pasteable.
- **FAIL:** Factual errors remain. Cross-reference inconsistencies exist. User-facing features undocumented. Blocks full audit completion.
