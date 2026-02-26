# GROWERS.md — Non-Critical Storage Growth Vectors

Identified 2026-02-26. None of these are stability or corruption risks.
All are within acceptable bounds ("few MB/year" territory) for current
and near-term production loads. Revisit for rotation/pruning when
deploying at scale (10+ agents, multi-year retention).

---

## 1. Embedding Cache Table (`memory/main.sqlite → embedding_cache`)

**Growth:** ~12 KB per unique text chunk. ~60 MB/year at 5000 chunks.
**Why it grows:** Every indexed chunk and every unique query gets cached. No TTL, no eviction.
**Rotation plan:** `DELETE FROM embedding_cache WHERE updated_at < strftime('%s','now','-90 days') * 1000`. Add to `lucyd-consolidate --maintain` when cache exceeds 100 MB.

## 2. Session Archive (`sessions/.archive/`)

**Growth:** ~3 files per reset (JSONL + state + dated JSONL). ~150 files/year for Lucy. Belladonna agents: up to 10K files/year.
**Why it grows:** Archived sessions are never deleted. Intentional — audit trail.
**Rotation plan:** Compress archives older than 90 days (`gzip`). Delete archives older than 2 years. Add `build_recall()` index to avoid O(N) glob scan when archive exceeds 1000 files.

## 3. JSONL Audit Trail (`sessions/*.YYYY-MM-DD.jsonl`)

**Growth:** ~250 KB/day for Lucy. ~2 MB/day for high-traffic Belladonna agents. ~90 MB–730 MB/year per agent.
**Why it grows:** Append-only by design. Compaction reduces state file but never touches JSONL. Date-split prevents single-file runaway.
**Rotation plan:** Compress JSONL files older than 30 days. Active sessions reference state file, not old JSONL. Consider annual archival to cold storage.

## 4. Facts & Episodes Tables (`memory/main.sqlite`)

**Growth:** ~1.8 MB/year (facts), ~500 KB/year (episodes). Invalidated facts soft-deleted but rows persist.
**Why it grows:** Consolidation extracts continuously. Maintenance logs stale facts but doesn't delete them.
**Rotation plan:** Add `DELETE FROM facts WHERE invalidated_at IS NOT NULL AND julianday('now') - julianday(invalidated_at) > 90` to `--maintain`. Add episode archival for entries older than 2 years.

## 5. State Files (`sessions/*.state.json`)

**Growth:** Proportional to message count between compactions. Typically 50–300 KB.
**Why it grows:** Full message list serialized. Token-based compaction threshold may miss sessions with many short messages.
**Rotation plan:** Add message-count compaction trigger (`len(messages) > 500`) as secondary threshold. Not urgent — token threshold catches it eventually.

## 6. Index/Consolidate Log Files (`~/.lucyd/lucyd-index.log`, `lucyd-consolidate.log`)

**Growth:** ~24 KB/day under normal operation. Could spike on errors.
**Why it grows:** Cron appends with `>>`, no logrotate.
**Rotation plan:** Add logrotate config or switch to Python `RotatingFileHandler` in the scripts.

## 7. Telegram/HTTP Download Temp Files (`/tmp/lucyd-telegram/`, `/tmp/lucyd-http/`)

**Growth:** ~60 MB/month for Lucy. ~600 MB/month for attachment-heavy agents.
**Why it grows:** Downloaded per-message, cleaned only on daemon shutdown. OS cleans `/tmp/` on reboot.
**Rotation plan:** Add per-message cleanup after processing, or add periodic `/tmp/lucyd-*` cleanup via cron. Not urgent on servers that reboot monthly.

## 8. Cost Database (`cost.db`)

**Growth:** ~5 MB/year per agent. Intentionally kept — needed for customer usage reports.
**Why it grows:** One row per API call, no pruning. This is correct behavior for billing.
**Rotation plan:** None — retain indefinitely (minimum 1 year for reporting). Indexes added for query performance. Consider partitioning by year if DB exceeds 100 MB.

---

## Review Schedule

- **Quarterly:** Check `du -sh ~/.lucyd/memory/main.sqlite` and `ls ~/.lucyd/sessions/.archive/ | wc -l`
- **Annually:** Evaluate rotation policies. Compress old JSONL. Prune invalidated facts.
- **At scale (10+ agents):** Implement automated cleanup in `lucyd-consolidate --maintain`.
