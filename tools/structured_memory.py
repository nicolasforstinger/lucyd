"""Structured memory tools — memory_write, memory_forget, commitment_update.

Agent-facing tools for direct fact management and commitment tracking.
Delegates to the same SQLite DB used by consolidation and recall.
"""

from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger(__name__)

_conn: sqlite3.Connection | None = None


def configure(conn: sqlite3.Connection) -> None:
    global _conn
    _conn = conn


def _normalize(name: str) -> str:
    return name.lower().strip().replace(" ", "_")


def _resolve_entity(entity: str) -> str:
    """Resolve through alias table."""
    normalized = _normalize(entity)
    if _conn is None:
        return normalized
    row = _conn.execute(
        "SELECT canonical FROM entity_aliases WHERE alias = ?",
        (normalized,),
    ).fetchone()
    return row[0] if row else normalized


async def handle_memory_write(entity: str, attribute: str, value: str) -> str:
    """Store a fact in structured memory."""
    if _conn is None:
        return "Error: structured memory not configured"

    entity = _resolve_entity(entity)
    attribute = _normalize(attribute)

    # Check existing
    existing = _conn.execute(
        "SELECT id, value FROM facts WHERE entity = ? "
        "AND attribute = ? AND invalidated_at IS NULL",
        (entity, attribute),
    ).fetchone()

    if existing:
        if existing[1] == value:
            _conn.execute(
                "UPDATE facts SET accessed_at = datetime('now') WHERE id = ?",
                (existing[0],),
            )
            _conn.commit()
            return f"Already known: {entity}.{attribute} = {value}"

        old_value = existing[1]
        _conn.execute(
            "UPDATE facts SET invalidated_at = datetime('now') WHERE id = ?",
            (existing[0],),
        )
        _conn.execute(
            "INSERT INTO facts (entity, attribute, value, confidence, source_session) "
            "VALUES (?, ?, ?, 1.0, 'agent')",
            (entity, attribute, value),
        )
        _conn.commit()
        return f"Updated: {entity}.{attribute} = {value} (was: {old_value})"

    _conn.execute(
        "INSERT INTO facts (entity, attribute, value, confidence, source_session) "
        "VALUES (?, ?, ?, 1.0, 'agent')",
        (entity, attribute, value),
    )
    _conn.commit()
    return f"Stored: {entity}.{attribute} = {value}"


async def handle_memory_forget(entity: str, attribute: str) -> str:
    """Mark a fact as no longer current."""
    if _conn is None:
        return "Error: structured memory not configured"

    entity = _resolve_entity(entity)
    attribute = _normalize(attribute)

    cursor = _conn.execute(
        "UPDATE facts SET invalidated_at = datetime('now') "
        "WHERE entity = ? AND attribute = ? AND invalidated_at IS NULL",
        (entity, attribute),
    )
    _conn.commit()

    if cursor.rowcount > 0:
        return f"Forgotten: {entity}.{attribute}"
    return f"No current fact found for {entity}.{attribute}"


async def handle_commitment_update(commitment_id: int, status: str) -> str:
    """Update a commitment's status."""
    if _conn is None:
        return "Error: structured memory not configured"

    cursor = _conn.execute(
        "UPDATE commitments SET status = ? "
        "WHERE id = ? AND status = 'open'",
        (status, commitment_id),
    )
    _conn.commit()

    if cursor.rowcount > 0:
        return f"Commitment #{commitment_id} marked as {status}"
    return f"No open commitment found with ID #{commitment_id}"


TOOLS = [
    {
        "name": "memory_write",
        "description": (
            "Store a fact in structured memory. Use for important information "
            "you want to recall reliably later. Facts are stored as "
            "entity-attribute-value triples."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {
                    "type": "string",
                    "description": "Who or what (lowercase, underscores for spaces)",
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
        "function": handle_memory_write,
    },
    {
        "name": "memory_forget",
        "description": (
            "Mark a fact as no longer current. The fact is preserved in "
            "history but won't appear in future recalls."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string"},
                "attribute": {"type": "string"},
            },
            "required": ["entity", "attribute"],
        },
        "function": handle_memory_forget,
    },
    {
        "name": "commitment_update",
        "description": (
            "Update a commitment's status. Use the commitment ID shown in "
            "the [Open commitments] section (e.g. #7)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "commitment_id": {
                    "type": "integer",
                    "description": "The commitment ID number",
                },
                "status": {
                    "type": "string",
                    "enum": ["done", "expired", "cancelled"],
                },
            },
            "required": ["commitment_id", "status"],
        },
        "function": handle_commitment_update,
    },
]
