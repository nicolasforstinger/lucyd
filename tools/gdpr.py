"""GDPR right-to-erasure tools — gdpr_search, gdpr_redact.

Allows the agent to find and remove/pseudonymize personal data
across all data stores when a data subject requests deletion.

Search scans: knowledge.facts, knowledge.episodes, knowledge.entity_aliases,
knowledge.commitments, sessions.messages, sessions.events, and workspace files.

Redact supports: delete (remove row) and redact (text replacement in JSONB/text).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg

from . import ToolSpec

if TYPE_CHECKING:
    from config import Config

log = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None
_client_id: str = ""
_agent_id: str = ""
_workspace: str = ""


def configure(
    pool: asyncpg.Pool | None = None,
    client_id: str = "",
    agent_id: str = "",
    config: Config | None = None,
    **_: object,
) -> None:
    global _pool, _client_id, _agent_id, _workspace
    _pool = pool
    _client_id = client_id
    _agent_id = agent_id
    if config is not None:
        _workspace = str(config.workspace)


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
            "WHERE client_id = $1 AND agent_id = $2 "
            "AND (entity ILIKE $3 OR attribute ILIKE $3 OR value ILIKE $3)",
            _client_id, _agent_id, like,
        )
        for r in rows:
            results.append(
                f"FACT #{r['id']}: {r['entity']}.{r['attribute']} = {r['value']}"
            )

        # ── Knowledge: entity_aliases ────────────────────────────
        rows = await _pool.fetch(
            "SELECT alias, canonical FROM knowledge.entity_aliases "
            "WHERE client_id = $1 AND agent_id = $2 "
            "AND (alias ILIKE $3 OR canonical ILIKE $3)",
            _client_id, _agent_id, like,
        )
        for r in rows:
            results.append(f"ALIAS: {r['alias']} -> {r['canonical']}")

        # ── Knowledge: episodes ──────────────────────────────────
        rows = await _pool.fetch(
            "SELECT id, summary, topics::text, decisions::text, commitments::text "
            "FROM knowledge.episodes "
            "WHERE client_id = $1 AND agent_id = $2 "
            "AND (summary ILIKE $3 OR topics::text ILIKE $3 "
            "OR decisions::text ILIKE $3 OR commitments::text ILIKE $3)",
            _client_id, _agent_id, like,
        )
        for r in rows:
            results.append(
                f"EPISODE #{r['id']}: {r['summary'][:150]}"
            )

        # ── Knowledge: commitments ───────────────────────────────
        rows = await _pool.fetch(
            "SELECT id, description, status FROM knowledge.commitments "
            "WHERE client_id = $1 AND agent_id = $2 "
            "AND description ILIKE $3",
            _client_id, _agent_id, like,
        )
        for r in rows:
            results.append(
                f"COMMITMENT #{r['id']} ({r['status']}): {r['description'][:150]}"
            )

        # ── Sessions: messages ───────────────────────────────────
        rows = await _pool.fetch(
            "SELECT m.id, m.session_id, m.ordinal, m.role, "
            "left(m.content::text, 200) AS preview "
            "FROM sessions.messages m "
            "WHERE m.client_id = $1 AND m.agent_id = $2 "
            "AND m.content::text ILIKE $3",
            _client_id, _agent_id, like,
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
            "WHERE client_id = $1 AND agent_id = $2 "
            "AND payload::text ILIKE $3",
            _client_id, _agent_id, like,
        )
        for r in rows:
            results.append(
                f"EVENT #{r['id']} (session={r['session_id'][:8]}.. "
                f"type={r['event_type']}): {r['preview']}"
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
            result: str = await _pool.execute(
                "UPDATE knowledge.facts SET invalidated_at = now() "
                "WHERE client_id = $1 AND agent_id = $2 AND id = $3",
                _client_id, _agent_id, id,
            )
            affected = int(result.split()[-1]) if result else 0
            return f"Fact #{id} invalidated." if affected else f"Fact #{id} not found."
        else:
            # Redact: replace text in entity, attribute, and value
            for col in ("entity", "attribute", "value"):
                await _pool.execute(
                    f"UPDATE knowledge.facts SET {col} = REPLACE({col}, $3, $4), "  # noqa: S608  # col is from a fixed set, not user input
                    f"updated_at = now() "
                    f"WHERE client_id = $1 AND agent_id = $2 AND id = $5",
                    _client_id, _agent_id, old, new, id,
                )
            return f"Fact #{id} redacted: '{old}' -> '{new}'."

    elif target == "alias":
        result = await _pool.execute(
            "DELETE FROM knowledge.entity_aliases "
            "WHERE client_id = $1 AND agent_id = $2 "
            "AND (alias = $3 OR canonical = $3)",
            _client_id, _agent_id, old or str(id),
        )
        affected = int(result.split()[-1]) if result else 0
        return f"Deleted {affected} alias(es)."

    elif target == "episode":
        if action == "delete":
            result = await _pool.execute(
                "DELETE FROM knowledge.episodes "
                "WHERE client_id = $1 AND agent_id = $2 AND id = $3",
                _client_id, _agent_id, id,
            )
            affected = int(result.split()[-1]) if result else 0
            return f"Episode #{id} deleted." if affected else f"Episode #{id} not found."
        else:
            await _pool.execute(
                "UPDATE knowledge.episodes SET "
                "summary = REPLACE(summary, $3, $4), "
                "topics = REPLACE(topics::text, $3, $4)::jsonb, "
                "decisions = REPLACE(decisions::text, $3, $4)::jsonb, "
                "commitments = REPLACE(commitments::text, $3, $4)::jsonb "
                "WHERE client_id = $1 AND agent_id = $2 AND id = $5",
                _client_id, _agent_id, old, new, id,
            )
            return f"Episode #{id} redacted: '{old}' -> '{new}'."

    elif target == "commitment":
        if action == "delete":
            result = await _pool.execute(
                "DELETE FROM knowledge.commitments "
                "WHERE client_id = $1 AND agent_id = $2 AND id = $3",
                _client_id, _agent_id, id,
            )
            affected = int(result.split()[-1]) if result else 0
            return f"Commitment #{id} deleted." if affected else f"Commitment #{id} not found."
        else:
            await _pool.execute(
                "UPDATE knowledge.commitments SET "
                "description = REPLACE(description, $3, $4) "
                "WHERE client_id = $1 AND agent_id = $2 AND id = $5",
                _client_id, _agent_id, old, new, id,
            )
            return f"Commitment #{id} redacted: '{old}' -> '{new}'."

    elif target == "message":
        if action == "delete":
            return "Error: Messages cannot be deleted (breaks session structure). Use action='redact' instead."
        result = await _pool.execute(
            "UPDATE sessions.messages SET "
            "content = REPLACE(content::text, $3, $4)::jsonb "
            "WHERE client_id = $1 AND agent_id = $2 AND id = $5",
            _client_id, _agent_id, old, new, id,
        )
        affected = int(result.split()[-1]) if result else 0
        return f"Message #{id} redacted: '{old}' -> '{new}'." if affected else f"Message #{id} not found."

    elif target == "event":
        if action == "delete":
            result = await _pool.execute(
                "DELETE FROM sessions.events "
                "WHERE client_id = $1 AND agent_id = $2 AND id = $3",
                _client_id, _agent_id, id,
            )
            affected = int(result.split()[-1]) if result else 0
            return f"Event #{id} deleted." if affected else f"Event #{id} not found."
        else:
            result = await _pool.execute(
                "UPDATE sessions.events SET "
                "payload = REPLACE(payload::text, $3, $4)::jsonb "
                "WHERE client_id = $1 AND agent_id = $2 AND id = $5",
                _client_id, _agent_id, old, new, id,
            )
            affected = int(result.split()[-1]) if result else 0
            return f"Event #{id} redacted: '{old}' -> '{new}'." if affected else f"Event #{id} not found."

    else:
        return f"Error: Unknown target '{target}'. Use: fact, alias, episode, commitment, message, event."


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="gdpr_search",
        description=(
            "Search all data stores for personal data matching any of the given terms. "
            "Use when a data subject requests deletion under GDPR Article 17. "
            "Searches: facts, episodes, aliases, commitments, messages, events, "
            "and workspace files. Case-insensitive. Provide all known identifiers "
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
            "action='delete' removes the record (facts are soft-deleted, "
            "messages cannot be deleted — use redact). "
            "action='redact' replaces 'old' text with 'new' (default: [REDACTED]). "
            "For workspace files, use the edit tool directly."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "enum": ["fact", "alias", "episode", "commitment", "message", "event"],
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
