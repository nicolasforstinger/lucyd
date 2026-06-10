"""Structured memory tools — memory_write, memory_forget, record_episode.

Agent-facing tools for direct fact management and episode recording.
Delegates to the same PostgreSQL knowledge schema used by consolidation and recall.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any  # Any justified: episode payload is JSON-shaped (lists of strings)

import asyncpg

import metrics

from . import ToolSpec

from consolidation import store_episode, upsert_fact
from memory import _normalize_entity as _normalize, resolve_entity

if TYPE_CHECKING:
    from config import Config

log = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None
# The configured user's canonical entity key (normalized config.user.name).
# References to it are pinned to this key, never routed through the alias
# table — a stray alice <-> alice_smith cycle must not be able to
# redirect a memory_write/forget on the user away from their own facts
# (entity-alias-cycle-corruption rec-2: a brief-verbatim forget was defeated
# this way). Empty when no config is wired (standalone/test default).
_user_entity: str = ""
# The configured user's contact session key — provenance label for episodes
# recorded during the maintenance harvest (episodes.session_id is TEXT, no FK).
_user_session: str = ""


def configure(
    pool: asyncpg.Pool | None = None,
    config: Config | None = None,
    **_: object,
) -> None:
    global _pool, _user_entity, _user_session
    _pool = pool
    _user_entity = _normalize(config.user_name) if config is not None else ""
    _user_session = f"user:{config.user_name}" if config is not None else ""


async def _resolve_entity(entity: str) -> str:
    """Resolve entity through alias table, falling back to normalization.

    The configured user entity is pinned: a reference that normalizes to it
    resolves to itself without consulting the alias table, so a cyclic alias
    edge can never redirect the user's own facts to a parallel key.
    """
    normalized = _normalize(entity)
    if _user_entity and normalized == _user_entity:
        return _user_entity
    if _pool is None:
        return normalized
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


async def handle_record_episode(
    summary: str,
    topics: list[str] | None = None,
    decisions: list[str] | None = None,
    emotional_tone: str = "",
) -> str:
    """Record an episode (a summary of recent conversation).

    Wraps store_episode so the maintenance harvest can capture conversational
    continuity — the summary and tone are what let a fresh session resume the
    thread and mood instead of starting cold.
    """
    if _pool is None:
        return "Error: Structured memory not configured in this deployment."

    data: dict[str, Any] = {
        "episode": {
            "topics": topics or [],
            "decisions": decisions or [],
            "summary": summary,
            "emotional_tone": emotional_tone or "neutral",
        }
    }
    episode_id = await store_episode(data, _user_session or "maintain", _pool)
    if episode_id is None:
        return (
            "Episode not recorded — needs a summary plus at least one of "
            "topics, decisions, or a non-neutral tone."
        )
    return f"Episode #{episode_id} recorded."


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
        name="record_episode",
        description=(
            "Record an episode: a short summary, in your own voice, of a stretch "
            "of recent conversation — so you carry its thread and mood into a "
            "fresh session instead of waking up cold. Include the emotional tone. "
            "Use this in your maintenance "
            "pass when consolidating what's happened since your last one."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "2-3 sentences in your voice: what happened and what it was about.",
                },
                "topics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Short lowercase topic tags, for later keyword recall.",
                },
                "decisions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Any decisions reached.",
                },
                "emotional_tone": {
                    "type": "string",
                    "description": "One word or short phrase for the mood of the exchange.",
                },
            },
            "required": ["summary"],
        },
        function=handle_record_episode,
    ),
]
