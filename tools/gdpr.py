"""GDPR right-to-erasure tools — gdpr_search, gdpr_redact.

Allows the agent to find and remove/pseudonymize personal data
across all data stores when a data subject requests deletion.

Search scans: knowledge.facts, knowledge.episodes, knowledge.entity_aliases,
sessions.messages, sessions.events, the search index (search.chunks), workspace
files, scheduled at-jobs, and download files.

Redact supports: delete (remove row/file/job) and redact (text replacement in
JSONB/text). Erasure is complete — fact delete is a hard DELETE, chunk purge also
clears cached embeddings, and the at-spool + download dir are swept — so no PII
residue survives a delete.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg

from . import ToolSpec

if TYPE_CHECKING:
    from config import Config

log = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None
_workspace: str = ""
_download_dir: str = ""


def configure(
    pool: asyncpg.Pool | None = None,
    config: Config | None = None,
    **_: object,
) -> None:
    global _pool, _workspace, _download_dir
    _pool = pool
    if config is not None:
        _workspace = str(config.workspace)
        _download_dir = str(config.http_download_dir)


# ── at-spool helpers (scheduled reminders / self-tasks hold message text) ──


async def _at_job_ids() -> list[str]:
    """List queued at-job ids. Empty if at/atd is unavailable."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "at", "-l",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
    except (OSError, ValueError) as e:
        log.debug("at -l unavailable: %s", e)
        return []
    ids: list[str] = []
    for line in out.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if parts and parts[0].isdigit():
            ids.append(parts[0])
    return ids


async def _at_job_text(job_id: str) -> str:
    """Return the job script for an at-job id (empty on error)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "at", "-c", job_id,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
    except (OSError, ValueError):
        return ""
    return out.decode("utf-8", "replace")


async def _delete_at_job(job_id: str) -> bool:
    """Remove an at-job by id. Returns True on success."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "atrm", job_id,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        return await proc.wait() == 0
    except (OSError, ValueError):
        return False


async def handle_gdpr_search(terms: list[str]) -> str:
    """Search all data stores for personal data matching any of the given terms."""
    if _pool is None:
        return "Error: Database not configured."
    if not terms:
        return "Error: Provide at least one search term."

    results: list[str] = []

    # Build ILIKE conditions for all terms
    for term in terms:
        like = f"%{term}%"

        # ── Knowledge: facts ─────────────────────────────────────
        rows = await _pool.fetch(
            "SELECT id, entity, attribute, value FROM knowledge.facts "
            "WHERE (entity ILIKE $1 OR attribute ILIKE $1 OR value ILIKE $1)",
            like,
        )
        for r in rows:
            results.append(
                f"FACT #{r['id']}: {r['entity']}.{r['attribute']} = {r['value']}"
            )

        # ── Knowledge: entity_aliases ────────────────────────────
        rows = await _pool.fetch(
            "SELECT alias, canonical FROM knowledge.entity_aliases "
            "WHERE (alias ILIKE $1 OR canonical ILIKE $1)",
            like,
        )
        for r in rows:
            results.append(f"ALIAS: {r['alias']} -> {r['canonical']}")

        # ── Knowledge: episodes ──────────────────────────────────
        rows = await _pool.fetch(
            "SELECT id, summary, topics::text, decisions::text "
            "FROM knowledge.episodes "
            "WHERE (summary ILIKE $1 OR topics::text ILIKE $1 "
            "OR decisions::text ILIKE $1)",
            like,
        )
        for r in rows:
            results.append(
                f"EPISODE #{r['id']}: {r['summary'][:150]}"
            )

        # ── Sessions: messages ───────────────────────────────────
        rows = await _pool.fetch(
            "SELECT m.id, m.session_id, m.ordinal, m.role, "
            "left(m.content::text, 200) AS preview "
            "FROM sessions.messages m "
            "WHERE m.content::text ILIKE $1",
            like,
        )
        for r in rows:
            results.append(
                f"MESSAGE #{r['id']} (session={r['session_id'][:8]}.. "
                f"ordinal={r['ordinal']} role={r['role']}): {r['preview']}"
            )

        # ── Sessions: events ─────────────────────────────────────
        rows = await _pool.fetch(
            "SELECT id, session_id, event_type, left(payload::text, 200) AS preview "
            "FROM sessions.events "
            "WHERE payload::text ILIKE $1",
            like,
        )
        for r in rows:
            results.append(
                f"EVENT #{r['id']} (session={r['session_id'][:8]}.. "
                f"type={r['event_type']}): {r['preview']}"
            )

        # ── Search index: chunks (derived copies of workspace text) ──
        rows = await _pool.fetch(
            "SELECT id, path, left(text, 200) AS preview FROM search.chunks "
            "WHERE text ILIKE $1",
            like,
        )
        for r in rows:
            results.append(
                f"CHUNK path={r['path']} (#{r['id']}): {r['preview']}"
            )

        # ── Workspace files ──────────────────────────────────────
        if _workspace:
            ws = Path(_workspace)
            if ws.is_dir():
                for fp in ws.rglob("*"):
                    if not fp.is_file() or fp.suffix in (".pyc", ".db"):
                        continue
                    try:
                        text = fp.read_text(encoding="utf-8", errors="ignore")
                    except OSError:
                        continue
                    if term.lower() in text.lower():
                        # Find matching lines for context
                        lines = [
                            f"  L{i+1}: {line.strip()[:120]}"
                            for i, line in enumerate(text.splitlines())
                            if term.lower() in line.lower()
                        ]
                        results.append(
                            f"FILE {fp.relative_to(ws)}: "
                            f"{len(lines)} match(es)\n" + "\n".join(lines[:5])
                        )

    # ── at-spool: scheduled reminders / self-tasks (scanned once over all terms) ──
    lowered = [t.lower() for t in terms]
    for job_id in await _at_job_ids():
        body = await _at_job_text(job_id)
        if any(t in body.lower() for t in lowered):
            results.append(
                f"ATJOB #{job_id}: scheduled job script contains a match "
                "(redact target='atjob' to remove it)"
            )

    # ── Downloads: transient attachment files ───────────────────────
    if _download_dir:
        dl = Path(_download_dir)
        if dl.is_dir():
            for fp in dl.iterdir():
                if not fp.is_file():
                    continue
                hit = any(t in fp.name.lower() for t in lowered)
                if not hit:
                    try:
                        body = fp.read_text(encoding="utf-8", errors="ignore")
                        hit = any(t in body.lower() for t in lowered)
                    except OSError:
                        continue
                if hit:
                    results.append(
                        f"DOWNLOAD {fp.name}: file in download dir matches "
                        "(redact target='download' to delete it)"
                    )

    # Deduplicate (same row might match multiple terms)
    seen: set[str] = set()
    unique: list[str] = []
    for r in results:
        if r not in seen:
            seen.add(r)
            unique.append(r)

    if not unique:
        return f"No matches found for: {', '.join(terms)}"

    return f"Found {len(unique)} match(es):\n\n" + "\n\n".join(unique)


async def handle_gdpr_redact(
    target: str, id: int, action: str,
    old: str = "", new: str = "[REDACTED]",
) -> str:
    """Delete or redact a specific record found by gdpr_search."""
    if _pool is None:
        return "Error: Database not configured."

    if action not in ("delete", "redact"):
        return "Error: action must be 'delete' or 'redact'."

    if action == "redact" and not old:
        return "Error: 'old' parameter required for redact action."

    if target == "fact":
        if action == "delete":
            # Erasure is a hard DELETE — not the soft invalidate used for normal
            # fact superseding (memory_forget) — so the PII value text leaves the
            # row entirely, not just gets flagged inactive.
            result: str = await _pool.execute(
                "DELETE FROM knowledge.facts WHERE id = $1",
                id,
            )
            affected = int(result.split()[-1]) if result else 0
            return f"Fact #{id} deleted." if affected else f"Fact #{id} not found."
        else:
            # Redact: replace text in entity, attribute, and value
            for col in ("entity", "attribute", "value"):
                await _pool.execute(
                    f"UPDATE knowledge.facts SET {col} = REPLACE({col}, $1, $2), "  # noqa: S608  # col is from a fixed set, not user input
                    f"updated_at = now() "
                    f"WHERE id = $3",
                    old, new, id,
                )
            return f"Fact #{id} redacted: '{old}' -> '{new}'."

    elif target == "alias":
        result = await _pool.execute(
            "DELETE FROM knowledge.entity_aliases "
            "WHERE (alias = $1 OR canonical = $1)",
            old or str(id),
        )
        affected = int(result.split()[-1]) if result else 0
        return f"Deleted {affected} alias(es)."

    elif target == "episode":
        if action == "delete":
            result = await _pool.execute(
                "DELETE FROM knowledge.episodes WHERE id = $1",
                id,
            )
            affected = int(result.split()[-1]) if result else 0
            return f"Episode #{id} deleted." if affected else f"Episode #{id} not found."
        else:
            await _pool.execute(
                "UPDATE knowledge.episodes SET "
                "summary = REPLACE(summary, $1, $2), "
                "topics = REPLACE(topics::text, $1, $2)::jsonb, "
                "decisions = REPLACE(decisions::text, $1, $2)::jsonb "
                "WHERE id = $3",
                old, new, id,
            )
            return f"Episode #{id} redacted: '{old}' -> '{new}'."

    elif target == "message":
        if action == "delete":
            return "Error: Messages cannot be deleted (breaks session structure). Use action='redact' instead."
        result = await _pool.execute(
            "UPDATE sessions.messages SET "
            "content = REPLACE(content::text, $1, $2)::jsonb "
            "WHERE id = $3",
            old, new, id,
        )
        affected = int(result.split()[-1]) if result else 0
        return f"Message #{id} redacted: '{old}' -> '{new}'." if affected else f"Message #{id} not found."

    elif target == "event":
        if action == "delete":
            result = await _pool.execute(
                "DELETE FROM sessions.events WHERE id = $1",
                id,
            )
            affected = int(result.split()[-1]) if result else 0
            return f"Event #{id} deleted." if affected else f"Event #{id} not found."
        else:
            result = await _pool.execute(
                "UPDATE sessions.events SET "
                "payload = REPLACE(payload::text, $1, $2)::jsonb "
                "WHERE id = $3",
                old, new, id,
            )
            affected = int(result.split()[-1]) if result else 0
            return f"Event #{id} redacted: '{old}' -> '{new}'." if affected else f"Event #{id} not found."

    elif target == "chunk":
        # Index chunks are derived copies of workspace text. Purge by the file
        # PATH (passed in `old`): the chunks rebuild clean from the (already-
        # redacted) source on the next index pass, so there is no continuity
        # cost — delete is the only sensible action for the index. Also purge the
        # embedding_cache rows for those chunks (same sha256(text) hash), so the
        # derived vectors don't survive the erasure.
        if not old:
            return "Error: for target='chunk', 'old' must be the file path whose index chunks to purge."
        async with _pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM search.embedding_cache WHERE hash IN "
                    "(SELECT hash FROM search.chunks WHERE path = $1)",
                    old,
                )
                result = await conn.execute(
                    "DELETE FROM search.chunks WHERE path = $1", old,
                )
        affected = int(result.split()[-1]) if result else 0
        return f"Purged {affected} index chunk(s) + cached embeddings for path '{old}'."

    elif target == "atjob":
        # at-jobs are immutable once queued — removal is the only erasure.
        if action != "delete":
            return "Error: at-jobs are immutable; use action='delete' to remove the job."
        job_id = str(id) if id else old
        if not job_id:
            return "Error: for target='atjob', provide the job id (from gdpr_search)."
        ok = await _delete_at_job(job_id)
        return f"Removed at-job {job_id}." if ok else f"at-job {job_id}: removal failed (not found or atrm unavailable)."

    elif target == "download":
        if action != "delete":
            return "Error: downloads support action='delete' only."
        if not _download_dir:
            return "Error: no download dir configured."
        if not old:
            return "Error: for target='download', 'old' must be the filename to delete."
        base = Path(_download_dir).resolve()
        # Strip any directory components from `old` so a crafted name can't escape.
        target_path = (base / Path(old).name).resolve()
        if target_path.parent != base:
            return "Error: path escapes the download dir."
        if not target_path.is_file():
            return f"Download '{old}' not found."
        target_path.unlink()
        return f"Deleted download '{target_path.name}'."

    else:
        return (f"Error: Unknown target '{target}'. Use: fact, alias, episode, "
                "message, event, chunk, atjob, download.")


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="gdpr_search",
        description=(
            "Search all data stores for personal data matching any of the given terms. "
            "Use when a data subject requests deletion under GDPR Article 17. "
            "Searches: facts, episodes, aliases, messages, events, the search "
            "index (chunks), workspace files, scheduled at-jobs, and download "
            "files. Case-insensitive. Provide all known identifiers "
            "(name, email, phone, address) for a thorough sweep.\n\n"
            "CRITICAL PROTOCOL:\n"
            "1. Search with ALL known identifiers in one call\n"
            "2. Review each hit, decide delete/redact/skip\n"
            "3. After all redactions, search AGAIN with the same terms — "
            "this catches YOUR OWN messages about the deletion request\n"
            "4. Redact those too — the request itself contains PII\n"
            "5. Reply with ONLY a generic confirmation: "
            "'Data subject erasure complete. N records processed.' "
            "NEVER repeat the person's name, email, or any identifier in your response\n"
            "6. Do NOT write any facts about the deletion to memory"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "terms": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of search terms (name, email, phone, address, etc.)",
                },
            },
            "required": ["terms"],
        },
        function=handle_gdpr_search,
    ),
    ToolSpec(
        name="gdpr_redact",
        description=(
            "Delete or redact a specific record found by gdpr_search. "
            "action='delete' removes the record (facts are hard-deleted for "
            "erasure; messages cannot be deleted — use redact). "
            "action='redact' replaces 'old' text with 'new' (default: [REDACTED]). "
            "For target='chunk', pass the file path in 'old' to purge that file's "
            "search-index chunks AND their cached embeddings (they rebuild from "
            "the redacted source). For target='atjob', pass the job id to remove "
            "a scheduled reminder/task (immutable — delete only). For "
            "target='download', pass the filename in 'old' to delete a download "
            "file. For workspace files, use the edit tool directly, then purge "
            "their chunks."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "enum": ["fact", "alias", "episode", "message", "event",
                             "chunk", "atjob", "download"],
                    "description": "The data store containing the record",
                },
                "id": {
                    "type": "integer",
                    "description": "Record ID from gdpr_search results",
                },
                "action": {
                    "type": "string",
                    "enum": ["delete", "redact"],
                    "description": "delete = remove record, redact = replace PII text",
                },
                "old": {
                    "type": "string",
                    "description": "Text to replace (required for redact action)",
                },
                "new": {
                    "type": "string",
                    "description": "Replacement text (default: [REDACTED])",
                    "default": "[REDACTED]",
                },
            },
            "required": ["target", "id", "action"],
        },
        function=handle_gdpr_redact,
    ),
]
