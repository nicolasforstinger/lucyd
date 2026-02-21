# Documentation Audit Report

**Date:** 2026-02-21
**Audit Cycle:** 4
**EXIT STATUS:** PASS

## Pattern Checks

| Pattern | Result | Details |
|---------|--------|---------|
| P-007 (test count drift) | PASS | README says 1158, actual `pytest --collect-only` reports 1158. No drift from Cycle 3 update. |
| P-008 (new module without docs) | PASS | All 29 source modules documented in architecture.md module map. Tool count 19 in source matches README, TOOLS.md, configuration.md, lucyd.toml.example. |
| P-011 (config-to-doc label consistency) | PASS | Model IDs consistent across providers.d/ and all docs: `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`, `text-embedding-3-small`. |

## Source Inventory

Built from source code, not from existing docs.

| Category | Count | Verified Against |
|----------|-------|------------------|
| Tools | 19 (across 11 tool-exporting modules) | `TOOLS` lists in `tools/*.py` |
| Channels | 3 (telegram, cli, http_api) | `channels/*.py` |
| Providers | 2 (anthropic-compat, openai-compat) | `providers/*.py` |
| Provider configs | 3 examples | `providers.d/*.toml.example` |
| CLI utilities | 3 (lucyd-send, lucyd-index, lucyd-consolidate) | `bin/` |
| Config sections | 14 top-level + sub-sections | `config.py` property definitions |
| Environment variables | 6 | `.env.example` + `config.py` |
| HTTP endpoints | 5 | `channels/http_api.py` route registrations |
| Test functions | 1158 | `python -m pytest tests/ --collect-only -q` |

## Files Audited

| File | Lines | Issues Found |
|------|-------|--------------|
| README.md | 149 | 0 |
| docs/architecture.md | 342 | 2 (HTTP endpoint table missing 2 endpoints, http_api.py description incomplete) |
| docs/configuration.md | 420 | 3 (missing `callback_url`/`callback_token_env`, missing `subagent_deny`, `[stt]` and `[memory.*]` misplaced under `## [tools]`) |
| docs/operations.md | 385 | 3 (/notify curl example wrong fields, /notify field table wrong, systemd copy command wrong filename) |
| .env.example | 10 | 0 |
| lucyd.toml.example | 180 | 2 (missing callback options, missing subagent_deny) |
| workspace.example/TOOLS.md | 31 | 0 |
| lucyd.service.example | — | 0 |
| providers.d/*.toml.example | — | 0 |
| LICENSE | — | 0 |

## Discrepancies Found & Fixed

| # | File | Line(s) | Issue | Fix Applied |
|---|------|---------|-------|-------------|
| 1 | operations.md | 167 | `/notify` curl example used `{"event": ..., "data": ...}` — source requires `{"message": ..., "source": ..., "ref": ...}` | Replaced curl example with correct fields |
| 2 | operations.md | 179-185 | `/notify` field table listed nonexistent `event` (required) and `priority` fields | Replaced with actual fields: `message`, `source`, `ref`, `data`, `sender` |
| 3 | operations.md | 153-168 | Missing `/sessions` and `/cost` endpoint examples | Added curl examples for both endpoints |
| 4 | operations.md | — | Webhook callback feature entirely undocumented | Added "Webhook Callback" section with payload format, config, and behavior |
| 5 | operations.md | 29 | `sudo cp ~/lucyd/lucyd.service` — file doesn't exist (should be `lucyd.service.example`) | Fixed to `sudo cp ~/lucyd/lucyd.service.example /etc/systemd/system/lucyd.service` |
| 6 | architecture.md | 147-151 | HTTP API table listed 3 endpoints — source has 5 (missing `/sessions`, `/cost`) | Added 2 rows: `/api/v1/sessions` (GET), `/api/v1/cost` (GET) |
| 7 | architecture.md | 21 | `channels/http_api.py` description: "chat, notify, status" | Updated to "chat, notify, status, sessions, cost" |
| 8 | configuration.md | 87-98 | Missing `[http] callback_url` and `callback_token_env` options | Added both with defaults and description |
| 9 | configuration.md | 207-230 | Missing `[tools] subagent_deny` config option | Added commented example + description of default deny-list |
| 10 | configuration.md | 246-362 | `### [stt]` nested under `## [tools]`; `### [memory.*]` subsections also misplaced under `## [tools]` | Restructured: moved `[memory.*]` under `## [memory]`, made `[stt]` a top-level `##` section |
| 11 | lucyd.toml.example | 54-57 | Missing `callback_url` and `callback_token_env` examples | Added commented-out callback options |
| 12 | lucyd.toml.example | 108-121 | Missing `subagent_deny` example | Added commented-out option with default list |

**Root cause analysis:**
- Findings 1-4: The `/notify` endpoint was redesigned at some point (from `event`/`priority` to `message`/`source`/`ref`/`data`) and the docs were never updated. The webhook callback was added without updating public docs (only CLAUDE.md had it).
- Finding 5: Template filename omitted in copy command.
- Findings 6-7: `/sessions` and `/cost` endpoints were added after the original HTTP docs were written.
- Findings 8-9: Config options `callback_url`, `callback_token_env`, and `subagent_deny` were added to `config.py` without corresponding documentation.
- Finding 10: Original configuration.md was written with flat subsection ordering; as sections grew, `[stt]` and `[memory.*]` ended up visually nested under the wrong parent.

## Cross-Reference Check

| Check | Status | Details |
|-------|--------|---------|
| Tool counts consistent | PASS | 19 in README, TOOLS.md, configuration.md, lucyd.toml.example |
| Tool names consistent | PASS | All 19 names match across TOOLS.md, configuration.md, lucyd.toml.example |
| Env vars consistent | PASS | 6 vars in .env.example = 6 in configuration.md |
| Config sections consistent | PASS | lucyd.toml.example sections match configuration.md (after restructuring) |
| HTTP endpoints consistent | PASS | 5 endpoints in architecture.md = 5 in operations.md = 5 in source |
| Feature list consistent | PASS | All README features exist in source |
| File references valid | PASS | All file/directory references in docs exist in repo |
| Model names consistent | PASS | All model IDs match across providers.d/ and docs |
| /notify field tables | PASS | operations.md matches source (http_api.py:200-209) after fix |
| Webhook callback documented | PASS | operations.md + configuration.md + lucyd.toml.example all consistent |

## Fixes Applied

**operations.md** (5 edits):
- /notify curl example: replaced `event`/`data` with `message`/`source`/`ref`
- /notify field table: replaced wrong fields with actual schema
- Added /sessions and /cost endpoint descriptions
- Added "Webhook Callback" section
- Fixed systemd copy command filename

**architecture.md** (2 edits):
- HTTP API table: added /sessions and /cost rows
- http_api.py description: added "sessions, cost"

**configuration.md** (3 edits):
- Added `callback_url` and `callback_token_env` to `[http]` section
- Added `subagent_deny` to `[tools]` section
- Restructured: moved `[memory.*]` under `## [memory]`, elevated `[stt]` to `##` level

**lucyd.toml.example** (2 edits):
- Added commented callback options to `[http]`
- Added commented `subagent_deny` to `[tools]`

## Missing Documentation

| Feature | Documented? | Notes |
|---------|-------------|-------|
| Plugin system (`plugins.d/`) | CLAUDE.md only | Code exists (`lucyd.py:_init_plugins`), documented in CLAUDE.md. Not in public docs (architecture.md, configuration.md, operations.md). The `plugins.d/` directory does not exist in the repo. Low priority — feature is functional but opt-in for deployers. |

## Verification

All 1158 tests pass after documentation changes (14.01s). No source code was modified in this stage — only `.md` and `.toml.example` files.

## Confidence

Overall confidence: 96%

- Tool counts: each tool traced to `TOOLS` list in source (19/19)
- Config keys: verified against `config.py` property definitions
- HTTP endpoints: verified against `http_api.py` route registrations (5/5)
- /notify schema: verified line-by-line against `_handle_notify()` (http_api.py:185-243)
- Webhook callback: verified against `_fire_webhook()` (lucyd.py:963-1000) and config.py:207-215
- Cross-reference: all 10 consistency checks pass after fixes
- Structure reorganization: verified section headings via grep
- Minor uncertainty: plugin system docs gap noted but not fixed (Low severity, CLAUDE.md covers it)
