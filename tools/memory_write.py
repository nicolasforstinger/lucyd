"""Structured memory tools — memory_write, memory_forget, commitment_update.

Agent-facing tools for direct fact management and commitment tracking.
Delegates to the same PostgreSQL knowledge schema used by consolidation and recall.
"""

from __future__ import annotations

import datetime
import logging

import asyncpg

import metrics

from . import ToolSpec

from consolidation import upsert_fact
from memory import _normalize_entity as _normalize, resolve_entity

log = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


def configure(
    pool: asyncpg.Pool | None = None,
    **_: object,
) -> None:
    global _pool
    _pool = pool


async def _resolve_entity(entity: str) -> str:
    """Resolve entity through alias table, falling back to normalization."""
    if _pool is None:
        return _normalize(entity)
    return await resolve_entity(entity, _pool)


async def handle_memory_write(entity: str, attribute: str, value: str) -> str:
    """Store a fact in structured memory."""
    if _pool is None:
        return "Error: Structured memory not configured in this deployment. Use memory_search for vector lookup instead."

    entity = await _resolve_entity(entity)
    attribute = _normalize(attribute)

    # Capture old value for reporting before upsert
    existing = await _pool.fetchrow(
        "SELECT value FROM knowledge.facts "
        "WHERE entity = $1 AND attribute = $2 AND invalidated_at IS NULL",
        entity, attribute,
    )
    old_value = existing["value"] if existing else None

    result = await upsert_fact(
        entity, attribute, value, _pool,
        confidence=1.0, source_session="agent",
    )

    if result == "unchanged":
        return f"Already known: {entity}.{attribute} = {value}"
    if metrics.ENABLED:
        metrics.MEMORY_OPS_TOTAL.labels(operation="fact_written").inc()
    if result == "updated":
        return f"Updated: {entity}.{attribute} = {value} (was: {old_value})"
    return f"Stored: {entity}.{attribute} = {value}"


async def handle_memory_forget(entity: str, attribute: str) -> str:
    """Mark a fact as no longer current."""
    if _pool is None:
        return "Error: Structured memory not configured in this deployment. Use memory_search for vector lookup instead."

    entity = await _resolve_entity(entity)
    attribute = _normalize(attribute)

    result: str = await _pool.execute(
        "UPDATE knowledge.facts SET invalidated_at = now() "
        "WHERE entity = $1 AND attribute = $2 AND invalidated_at IS NULL",
        entity, attribute,
    )
    updated = int(result.split()[-1]) if result else 0

    if updated > 0:
        return f"Forgotten: {entity}.{attribute}"
    return f"No current fact found for {entity}.{attribute}"


async def handle_commitment_update(
    commitment_id: int,
    status: str | None = None,
    deadline: str | None = None,
    what: str | None = None,
) -> str:
    """Change a commitment's status and/or correct its deadline or details.

    Each provided field is written; omitted fields are left untouched. Only
    open commitments are mutable — the WHERE guard refuses already-closed rows.
    """
    if _pool is None:
        return "Error: Structured memory not configured in this deployment. Use memory_search for vector lookup instead."

    # Build the SET clause from only the fields the caller actually passed, so
    # an omitted field is never overwritten with its column default.
    assignments: list[str] = []
    bind_args: list[object] = []
    if status is not None:
        bind_args.append(status)
        assignments.append(f"status = ${len(bind_args)}")
    if deadline is not None:
        # The deadline column is TEXT holding an ISO date (see schema/001_initial.sql
        # and consolidation.py). Parse to validate the format, then store the
        # normalized ISO string so it stays comparable to CURRENT_DATE::text.
        try:
            deadline_iso = datetime.date.fromisoformat(deadline).isoformat()
        except ValueError:
            return f"Error: deadline must be an ISO date like 2026-05-28, got: {deadline}"
        bind_args.append(deadline_iso)
        assignments.append(f"deadline = ${len(bind_args)}")
    if what is not None:
        bind_args.append(what)
        assignments.append(f"what = ${len(bind_args)}")

    if not assignments:
        return "Error: provide at least one of status, deadline, or what to update."

    bind_args.append(commitment_id)
    result: str = await _pool.execute(
        f"UPDATE knowledge.commitments SET {', '.join(assignments)} "
        f"WHERE id = ${len(bind_args)} AND status = 'open'",
        *bind_args,
    )
    updated = int(result.split()[-1]) if result else 0

    if updated > 0:
        if metrics.ENABLED:
            metrics.MEMORY_OPS_TOTAL.labels(operation="commitment_updated").inc()
        return f"Commitment #{commitment_id} updated"
    return f"No open commitment found with ID #{commitment_id}"


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="memory_write",
        description=(
            "Store a fact in structured memory. Use for important information "
            "you want to recall reliably later. Facts are stored as "
            "entity-attribute-value triples."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "entity": {
                    "type": "string",
                    "description": "Who or what (lowercase, underscores for spaces). Normalized and alias-resolved automatically.",
                },
                "attribute": {
                    "type": "string",
                    "description": "What about them (lowercase, descriptive)",
                },
                "value": {
                    "type": "string",
                    "description": "The fact",
                },
            },
            "required": ["entity", "attribute", "value"],
        },
        function=handle_memory_write,
    ),
    ToolSpec(
        name="memory_forget",
        description=(
            "Mark a fact as no longer current. The fact is preserved in "
            "history but won't appear in future recalls."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "Who or what (lowercase, underscores for spaces). Alias-resolved automatically."},
                "attribute": {"type": "string", "description": "What about them (lowercase, descriptive)"},
            },
            "required": ["entity", "attribute"],
        },
        function=handle_memory_forget,
    ),
    ToolSpec(
        name="commitment_update",
        description=(
            "Correct a commitment's deadline or details, or change its status. "
            "Pass `commitment_id` — the integer shown after `#` in the "
            "[Open commitments] section (e.g. 7) — plus at least one of "
            "`status`, `deadline`, or `what`. Only open commitments can be edited."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "commitment_id": {
                    "type": "integer",
                    "description": "The integer shown after `#` in the [Open commitments] section (e.g. 7).",
                },
                "status": {
                    "type": "string",
                    "enum": ["done", "expired", "cancelled"],
                    "description": "New status when closing the commitment.",
                },
                "deadline": {
                    "type": "string",
                    "description": "Corrected deadline as an ISO date, e.g. 2026-05-28.",
                },
                "what": {
                    "type": "string",
                    "description": "Corrected description of what the commitment is.",
                },
            },
            "required": ["commitment_id"],
        },
        function=handle_commitment_update,
    ),
]
