"""Knowledge-schema storage helpers + the consolidation watermark.

The persistence primitives shared by the agent's memory tools and the
maintenance harvest: fact upsert, episode storage, conversation
serialization for the harvest brief, and the per-session consolidation
watermark. Fact and episode *extraction* is the agent's own job now, done
in the maintenance pass — not a neutral LLM extractor.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

import metrics
from messages import Message
from session import _text_from_content

log = logging.getLogger(__name__)


# ─── State Tracking ─────────────────────────────────────────────

async def get_unprocessed_range(
    session_id: str,
    messages: list[Message],
    compaction_count: int,
    pool: asyncpg.Pool,
) -> tuple[int, int]:
    """Return (start_idx, end_idx) of messages needing consolidation.

    Uses consolidation_state to determine what's been processed.

    Handles all lifecycle states:
    - First run: process everything
    - Normal accumulation: process new messages only
    - Post-compaction: skip summary message (index 0), process rest
    - No new content: return (0, 0)
    """
    state = await pool.fetchrow(
        "SELECT last_compaction_count, last_message_count "
        "FROM knowledge.consolidation_state "
        "WHERE session_id = $1",
        session_id,
    )

    if state is None:
        return (0, len(messages))

    last_compaction = state["last_compaction_count"]
    last_end = state["last_message_count"]

    if compaction_count > last_compaction:
        return (1, len(messages))

    if len(messages) > last_end:
        return (last_end, len(messages))

    return (0, 0)


async def update_consolidation_state(
    session_id: str,
    compaction_count: int,
    message_count: int,
    pool: asyncpg.Pool,
) -> None:
    await pool.execute(
        """INSERT INTO knowledge.consolidation_state
           (session_id,
            last_compaction_count, last_message_count, last_consolidated_at)
           VALUES ($1, $2, $3, now())
           ON CONFLICT (session_id)
           DO UPDATE SET last_compaction_count = EXCLUDED.last_compaction_count,
                         last_message_count = EXCLUDED.last_message_count,
                         last_consolidated_at = now()""",
        session_id,
        compaction_count, message_count,
    )


# ─── Message Serializer ─────────────────────────────────────────

def serialize_messages(
    messages: list[Message],
    start_idx: int,
    end_idx: int,
    max_chars: int = 50_000,
) -> str:
    """Convert messages to a text block for LLM extraction.

    Keeps only user and assistant text content (no tool results),
    and respects a character budget.
    """
    parts: list[str] = []
    chars = 0
    for msg in messages[start_idx:end_idx]:
        role = msg.get("role", "")
        if role not in ("user", "agent"):
            continue
        if role == "agent":
            text = msg.get("text", "")
        else:
            raw_content = msg.get("content", "")
            # _text_from_content handles str, list[dict], and None
            text = _text_from_content(raw_content if isinstance(raw_content, (str, list)) else str(raw_content))
        if not text:
            continue
        line = f"{role}: {text}"
        if chars + len(line) > max_chars:
            remaining = max_chars - chars
            if remaining > 100:
                parts.append(line[:remaining])
            break
        parts.append(line)
        chars += len(line)
    return "\n".join(parts)


async def upsert_fact(
    entity: str,
    attribute: str,
    value: str,
    pool: asyncpg.Pool,
    confidence: float = 1.0,
    source_session: str = "",
) -> str:
    """Insert or update a single fact. Returns 'new', 'updated', or 'unchanged'.

    Shared by the agent tool (memory_write) and the /maintain harvest.
    Handles dedup, invalidation-on-change, and accessed_at touch.
    Caller must normalize entity/attribute and resolve aliases beforehand.
    """
    existing = await pool.fetchrow(
        "SELECT id, value FROM knowledge.facts "
        "WHERE entity = $1 AND attribute = $2 AND invalidated_at IS NULL",
        entity, attribute,
    )

    if existing:
        if existing["value"] == value:
            await pool.execute(
                "UPDATE knowledge.facts SET accessed_at = now() WHERE id = $1",
                existing["id"],
            )
            return "unchanged"
        await pool.execute(
            "UPDATE knowledge.facts SET invalidated_at = now() WHERE id = $1",
            existing["id"],
        )

    await pool.execute(
        "INSERT INTO knowledge.facts "
        "(entity, attribute, value, confidence, source_session) "
        "VALUES ($1, $2, $3, $4, $5)",
        entity, attribute, value, confidence, source_session,
    )
    return "updated" if existing else "new"


async def store_episode(
    data: dict[str, Any],
    session_id: str,
    pool: asyncpg.Pool,
) -> int | None:
    """Store extracted episode in DB. Returns episode ID or None."""
    episode = data.get("episode", {})
    topics = episode.get("topics", [])
    decisions = episode.get("decisions", [])
    summary = episode.get("summary", "")
    emotional_tone = episode.get("emotional_tone", "")

    if (not topics and not decisions
            and emotional_tone == "neutral"):
        return None

    if not summary:
        return None

    episode_id: int | None = await pool.fetchval(
        """INSERT INTO knowledge.episodes
           (session_id, topics, decisions, summary, emotional_tone)
           VALUES ($1, $2::jsonb, $3::jsonb, $4, $5)
           RETURNING id""",
        session_id,
        json.dumps(topics), json.dumps(decisions),
        summary, emotional_tone,
    )
    if metrics.ENABLED:
        metrics.MEMORY_OPS_TOTAL.labels(operation="episode_created").inc()

    return episode_id
