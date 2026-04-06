"""Structured memory tools — memory_write, memory_forget, commitment_update.

Agent-facing tools for direct fact management and commitment tracking.
Delegates to the same PostgreSQL knowledge schema used by consolidation and recall.
"""

from __future__ import annotations

import logging
from typing import Any

import metrics

from . import ToolSpec

from consolidation import _normalize_entity as _normalize
from consolidation import upsert_fact

log = logging.getLogger(__name__)

_pool: Any = None
_client_id: str = ""
_agent_id: str = ""


def configure(
    pool: Any = None,
    client_id: str = "",
    agent_id: str = "",
    **_: Any,
) -> None:
    global _pool, _client_id, _agent_id
    _pool = pool
    _client_id = client_id
    _agent_id = agent_id


async def _resolve_entity(entity: str) -> str:
    """Resolve entity through alias table, falling back to normalization."""
    from memory import resolve_entity
    if _pool is None:
        return _normalize(entity)
    return await resolve_entity(entity, _pool, _client_id, _agent_id)


async def handle_memory_write(entity: str, attribute: str, value: str) -> str:
    """Store a fact in structured memory."""
    if _pool is None:
        return "Error: Structured memory not configured in this deployment. Use memory_search for vector lookup instead."

    entity = await _resolve_entity(entity)
    attribute = _normalize(attribute)

    # Capture old value for reporting before upsert
    existing = await _pool.fetchrow(
        "SELECT value FROM knowledge.facts "
        "WHERE client_id = $1 AND agent_id = $2 "
        "AND entity = $3 AND attribute = $4 AND invalidated_at IS NULL",
        _client_id, _agent_id, entity, attribute,
    )
    old_value = existing["value"] if existing else None

    result = await upsert_fact(
        entity, attribute, value, _pool, _client_id, _agent_id,
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
        "WHERE client_id = $1 AND agent_id = $2 "
        "AND entity = $3 AND attribute = $4 AND invalidated_at IS NULL",
        _client_id, _agent_id, entity, attribute,
    )
    updated = int(result.split()[-1]) if result else 0

    if updated > 0:
        return f"Forgotten: {entity}.{attribute}"
    return f"No current fact found for {entity}.{attribute}"


async def handle_commitment_update(commitment_id: int, status: str) -> str:
    """Update a commitment's status."""
    if _pool is None:
        return "Error: Structured memory not configured in this deployment. Use memory_search for vector lookup instead."

    result: str = await _pool.execute(
        "UPDATE knowledge.commitments SET status = $1 "
        "WHERE client_id = $2 AND agent_id = $3 "
        "AND id = $4 AND status = 'open'",
        status, _client_id, _agent_id, commitment_id,
    )
    updated = int(result.split()[-1]) if result else 0

    if updated > 0:
        if metrics.ENABLED:
            metrics.MEMORY_OPS_TOTAL.labels(operation="commitment_updated").inc()
        return f"Commitment #{commitment_id} marked as {status}"
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
            "Update a commitment's status. Use the commitment ID shown in "
            "the [Open commitments] section (e.g. #7)."
        ),
        input_schema={
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
        function=handle_commitment_update,
    ),
]
