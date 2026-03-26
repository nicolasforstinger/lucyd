"""Consolidation — extract structured data from conversations and files.

Extracts facts (entity-attribute-value), episodes (narrative summaries),
and commitments (trackable promises) from session transcripts. Stores
in SQLite tables managed by memory_schema.py.
"""

from __future__ import annotations

import contextlib

import metrics
import hashlib
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from session import _text_from_content

log = logging.getLogger(__name__)

MAX_EXTRACTION_CHARS = 50_000  # ~12k tokens; configurable via [memory.consolidation] max_extraction_chars


# ─── State Tracking ─────────────────────────────────────────────

def get_unprocessed_range(
    session_id: str,
    messages: list,
    compaction_count: int,
    conn: sqlite3.Connection,
) -> tuple[int, int]:
    """Return (start_idx, end_idx) of messages needing consolidation.

    Uses consolidation_state with composite PK (session_id, compaction_count).
    Queries the latest row for this session to determine what's been processed.

    Handles all lifecycle states:
    - First run: process everything
    - Normal accumulation: process new messages only
    - Post-compaction: skip summary message (index 0), process rest
    - No new content: return (0, 0)
    """
    state = conn.execute(
        "SELECT last_compaction_count, last_message_count "
        "FROM consolidation_state WHERE session_id = ?",
        (session_id,),
    ).fetchone()

    if state is None:
        return (0, len(messages))

    last_compaction = state[0]
    last_end = state[1]

    if compaction_count > last_compaction:
        # Compaction happened since last consolidation.
        # Index 0 is the summary of already-processed content. Skip it.
        return (1, len(messages))

    if len(messages) > last_end:
        # New messages since last consolidation.
        return (last_end, len(messages))

    # Nothing new.
    return (0, 0)


def update_consolidation_state(
    session_id: str,
    compaction_count: int,
    message_count: int,
    conn: sqlite3.Connection,
) -> None:
    conn.execute("""
        INSERT OR REPLACE INTO consolidation_state
            (session_id, last_compaction_count, last_message_count, last_consolidated_at)
        VALUES (?, ?, ?, datetime('now'))
    """, (session_id, compaction_count, message_count))


# ─── Message Serializer ─────────────────────────────────────────

def serialize_messages(
    messages: list[dict],
    start_idx: int,
    end_idx: int,
    max_tool_output: int = 2000,
    max_chars: int = MAX_EXTRACTION_CHARS,
) -> str:
    """Serialize a range of session messages to text for extraction.

    If total output exceeds max_chars, drops oldest messages in the
    range (keeping most recent).
    """
    if start_idx >= end_idx or start_idx >= len(messages):
        return ""

    parts = []
    for msg in messages[start_idx:end_idx]:
        role = msg.get("role", "")
        if role == "user":
            content = _text_from_content(msg.get("content", ""))
            parts.append(f"Human: {content}")
        elif role == "assistant":
            text = msg.get("text", msg.get("content", ""))
            if text:
                parts.append(f"Assistant: {text}")
            for tc in msg.get("tool_calls", []):
                tc_name = tc.get("name", "unknown")
                tc_args = str(tc.get("arguments", {}))[:max_tool_output]
                parts.append(f"Tool call: {tc_name}({tc_args})")
        elif role == "tool_results":
            for r in msg.get("results", []):
                content = r.get("content", "")[:max_tool_output]
                parts.append(f"Tool result: {content}")

    if not parts:
        return ""

    # If over budget, drop oldest messages first (keep most recent)
    result = "\n\n".join(parts)
    while len(result) > max_chars and len(parts) > 1:
        parts.pop(0)
        result = "\n\n".join(parts)

    return result


# ─── Shared Helpers ──────────────────────────────────────────────

def _normalize_entity(name: str) -> str:
    return name.lower().strip().replace(" ", "_")


def upsert_fact(
    entity: str,
    attribute: str,
    value: str,
    conn: sqlite3.Connection,
    confidence: float = 1.0,
    source_session: str = "",
) -> str:
    """Insert or update a single fact. Returns 'new', 'updated', or 'unchanged'.

    Shared by LLM extraction (_store_facts) and agent tool (memory_write).
    Handles dedup, invalidation-on-change, and accessed_at touch.
    Caller must normalize entity/attribute and resolve aliases beforehand.
    """
    existing = conn.execute(
        "SELECT id, value FROM facts WHERE entity = ? "
        "AND attribute = ? AND invalidated_at IS NULL",
        (entity, attribute),
    ).fetchone()

    if existing:
        if existing[1] == value:
            conn.execute(
                "UPDATE facts SET accessed_at = datetime('now') WHERE id = ?",
                (existing[0],),
            )
            return "unchanged"
        conn.execute(
            "UPDATE facts SET invalidated_at = datetime('now') WHERE id = ?",
            (existing[0],),
        )

    conn.execute(
        "INSERT INTO facts (entity, attribute, value, confidence, source_session) "
        "VALUES (?, ?, ?, ?, ?)",
        (entity, attribute, value, confidence, source_session),
    )
    return "updated" if existing else "new"


def _strip_json_fences(text: str) -> str:
    """Strip markdown code fences from JSON text."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _store_facts(
    data: dict,
    session_id: str,
    conn: sqlite3.Connection,
    confidence_threshold: float,
) -> int:
    """Store extracted facts and aliases in DB. Returns count of new/updated facts."""
    # ORDERING INVARIANT: Store aliases FIRST so entity resolution works
    # for new entities extracted in the same batch.
    for alias_entry in data.get("aliases", []):
        alias = _normalize_entity(alias_entry.get("alias", ""))
        canonical = _normalize_entity(alias_entry.get("canonical", ""))
        if alias and canonical and alias != canonical:
            conn.execute(
                "INSERT OR IGNORE INTO entity_aliases (alias, canonical) VALUES (?, ?)",
                (alias, canonical),
            )

    count = 0
    for fact in data.get("facts", []):
        confidence = fact.get("confidence", 0.0)
        if confidence < confidence_threshold:
            continue

        entity = _normalize_entity(fact.get("entity", ""))
        attribute = _normalize_entity(fact.get("attribute", ""))
        value = fact.get("value", "")
        if not entity or not attribute or not value:
            continue

        from memory import resolve_entity
        entity = resolve_entity(entity, conn)

        result = upsert_fact(entity, attribute, value, conn,
                             confidence=confidence, source_session=session_id)
        if result != "unchanged":
            count += 1

    if count > 0 and metrics.ENABLED:
        metrics.MEMORY_OPS_TOTAL.labels(operation="fact_written").inc(count)
    return count


def _store_episode(
    data: dict,
    session_id: str,
    conn: sqlite3.Connection,
) -> int | None:
    """Store extracted episode in DB. Returns episode ID or None."""
    episode = data.get("episode", {})
    topics = episode.get("topics", [])
    decisions = episode.get("decisions", [])
    commitments = episode.get("commitments", [])
    summary = episode.get("summary", "")
    emotional_tone = episode.get("emotional_tone", "")

    # Skip trivial episodes
    if (not topics and not decisions and not commitments
            and emotional_tone == "neutral"):
        return None

    if not summary:
        return None

    cursor = conn.execute(
        "INSERT INTO episodes (session_id, topics, decisions, commitments, "
        "summary, emotional_tone) VALUES (?, ?, ?, ?, ?, ?)",
        (
            session_id,
            json.dumps(topics),
            json.dumps(decisions),
            json.dumps(commitments),
            summary,
            emotional_tone,
        ),
    )
    episode_id = cursor.lastrowid
    if metrics.ENABLED:
        metrics.MEMORY_OPS_TOTAL.labels(operation="episode_created").inc()

    for c in commitments:
        who = c.get("who", "")
        what = c.get("what", "")
        deadline = c.get("deadline")
        if deadline == "null":
            deadline = None
        if who and what:
            conn.execute(
                "INSERT INTO commitments (episode_id, who, what, deadline) "
                "VALUES (?, ?, ?, ?)",
                (episode_id, who, what, deadline),
            )

    return episode_id


# ─── Shared Fact Extraction Rules ─────────────────────────────────

_FACT_RULES = """\
- Only extract facts explicitly stated or strongly implied
- Entity names: use the shortest common name as the canonical entity
  (alex not alex_johnson, sam not sam_martinez, chris not chris_taylor). Lowercase, underscores for spaces.
- Attributes: lowercase, descriptive (lives_in, role, preference)
- Confidence: 1.0 = directly stated, 0.8 = strongly implied, 0.6 = weakly implied
- Below 0.6 = do not extract
- When a person or thing is referred to by multiple names, include alias entries
  mapping each alternative name to the primary entity name
- Also include component-word aliases for multi-word entities:
  e.g. entity "uncle_charles" gets aliases "uncle" and "charles"
  pointing to "uncle_charles"\
"""


# ─── Fact-Only Extraction (for files) ────────────────────────────

FACT_EXTRACTION_PROMPT = """Extract factual information from this text as JSON.
Return ONLY valid JSON, no markdown fences, no preamble.

Schema:
{"facts": [
  {"entity": "lowercase_name", "attribute": "lowercase_attr",
   "value": "the fact", "confidence": 0.0-1.0}
],
"aliases": [
  {"alias": "alternative_name", "canonical": "primary_entity_name"}
]}

Rules:
""" + _FACT_RULES + """
- If nothing worth extracting, return {"facts": [], "aliases": []}
"""


async def _llm_extract_json(
    provider,
    system_blocks: list[dict],
    prompt_text: str,
    label: str = "extraction",
) -> tuple[dict | None, Any]:
    """Shared helper: format -> call -> strip fences -> parse JSON.

    Returns (parsed_dict_or_None, usage_or_None).
    """
    fmt_system = provider.format_system(system_blocks)
    fmt_messages = provider.format_messages(
        [{"role": "user", "content": prompt_text}],
    )

    try:
        response = await provider.complete(fmt_system, fmt_messages, [])
    except Exception:
        log.exception("%s LLM call failed", label.capitalize())
        return None, None

    raw = response.text or ""
    raw = _strip_json_fences(raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("%s returned invalid JSON: %s", label.capitalize(), raw[:200])
        return None, response.usage

    return data, response.usage


async def extract_facts(
    text: str,
    session_id: str,
    provider,
    conn: sqlite3.Connection,
    confidence_threshold: float = 0.6,
) -> tuple[int, Any]:
    """Extract facts from text and store in DB (facts-only path for files).

    Returns (count of new/updated facts, usage object or None).
    """
    system_blocks = [{"text": FACT_EXTRACTION_PROMPT, "tier": "stable"}]
    data, usage = await _llm_extract_json(provider, system_blocks, text, "Fact extraction")
    if data is None:
        return 0, usage

    count = _store_facts(data, session_id, conn, confidence_threshold)
    return count, usage


# ─── Combined Extraction (for sessions) ──────────────────────────

COMBINED_EXTRACTION_PROMPT = """You are performing a structured data extraction task.
You MUST respond with ONLY valid JSON. No prose, no roleplay, no conversation, no markdown fences.

The following persona context describes the agent whose perspective
to use when writing the episode summary. Use it for voice and tone
only — do not adopt this identity or respond in character:

---
{persona_context}
---

Extract BOTH factual information AND an episode summary from the conversation text the user provides.

Return ONLY valid JSON matching this schema:

{{"facts": [
  {{"entity": "lowercase_name", "attribute": "lowercase_attr",
   "value": "the fact", "confidence": 0.0-1.0}}
],
"aliases": [
  {{"alias": "alternative_name", "canonical": "primary_entity_name"}}
],
"episode": {{
  "topics": ["topic1", "topic2"],
  "decisions": ["decision made"],
  "commitments": [
    {{"who": "name", "what": "the commitment", "deadline": "YYYY-MM-DD or null"}}
  ],
  "summary": "2-3 sentences describing what happened, written from the agent's perspective",
  "emotional_tone": "one word or short phrase"
}}}}

Fact extraction rules:
""" + _FACT_RULES + """

If the conversation was trivial or purely mechanical, return empty facts/aliases and a neutral episode:
{{"facts": [], "aliases": [],
  "episode": {{"topics": [], "decisions": [], "commitments": [],
  "summary": "Brief mechanical exchange.", "emotional_tone": "neutral"}}}}"""


async def extract_structured_data(
    text: str,
    session_id: str,
    provider,
    system_blocks: list[dict],
    conn: sqlite3.Connection,
    confidence_threshold: float = 0.6,
) -> tuple[int, int | None, Any]:
    """Extract facts and episode from text in a single LLM call.

    Returns (facts_added, episode_id_or_None, usage_or_None).
    """
    persona_text = "\n\n".join(
        block["text"] if isinstance(block, dict) else str(block)
        for block in system_blocks
    )
    system = COMBINED_EXTRACTION_PROMPT.format(persona_context=persona_text)

    data, usage = await _llm_extract_json(
        provider, [{"text": system, "tier": "stable"}], text, "Combined extraction",
    )
    if data is None:
        return 0, None, usage

    facts_added = _store_facts(data, session_id, conn, confidence_threshold)
    episode_id = _store_episode(data, session_id, conn)

    return facts_added, episode_id, usage



# ─── Cost Recording ──────────────────────────────────────────────

def _record_extraction_cost(
    usage,
    *,
    metering=None,
    session_id: str = "",
    model_name: str = "",
    cost_rates: list[float] | None = None,
    trace_id: str = "",
) -> None:
    """Record extraction cost via metering."""
    if not usage or not cost_rates:
        return
    if metering:
        metering.record(
            session_id=session_id,
            model=model_name, provider="",
            usage=usage, cost_rates=cost_rates,
            call_type="consolidation", trace_id=trace_id,
        )


# ─── Main Entry Point ────────────────────────────────────────────

async def consolidate_session(
    session_id: str,
    messages: list[dict],
    compaction_count: int,
    config,
    provider,
    context_builder,
    conn: sqlite3.Connection,
    trace_id: str = "",
    metering=None,
) -> dict:
    """Run full consolidation on a session's messages.

    Uses a single LLM call to extract both facts and episode.
    Returns {"facts_added": int, "episode_id": int | None}
    """
    start_idx, end_idx = get_unprocessed_range(
        session_id, messages, compaction_count, conn,
    )
    if end_idx <= start_idx:
        return {"facts_added": 0, "episode_id": None}

    if (end_idx - start_idx) < 4:
        return {"facts_added": 0, "episode_id": None}

    max_chars = MAX_EXTRACTION_CHARS
    text = serialize_messages(messages, start_idx, end_idx, max_chars=max_chars)
    if not text.strip():
        return {"facts_added": 0, "episode_id": None}

    threshold = getattr(config, "consolidation_confidence_threshold", 0.6)

    try:
        persona_blocks = context_builder.build_stable()
        facts_added, episode_id, usage = await extract_structured_data(
            text, session_id, provider, persona_blocks, conn, threshold,
        )

        update_consolidation_state(
            session_id, compaction_count, len(messages), conn,
        )

        conn.commit()
    except Exception:
        with contextlib.suppress(Exception):
            conn.rollback()
        raise

    # Record consolidation cost (after commit — non-critical)
    model_role = "primary"
    model_cfg = config.model_config(model_role) if hasattr(config, "model_config") else {}
    cost_rates = model_cfg.get("cost_per_mtok")
    display_name = model_cfg.get("model", model_role)
    _record_extraction_cost(
        usage, metering=metering, session_id=session_id,
        model_name=display_name, cost_rates=cost_rates, trace_id=trace_id,
    )

    return {"facts_added": facts_added, "episode_id": episode_id}


# ─── Markdown File Extraction ────────────────────────────────────

async def extract_from_file(
    file_path: str,
    provider,
    conn: sqlite3.Connection,
    confidence_threshold: float = 0.6,
    model_name: str = "",
    cost_rates: list[float] | None = None,
    metering=None,
) -> int:
    """Extract facts from a workspace markdown file.

    Checks content hash to avoid reprocessing unchanged files.
    Only extracts facts (no episodes — those come from conversations).

    Returns count of new/updated facts.
    """
    path = Path(file_path)
    if not path.exists():
        return 0

    content = path.read_text(encoding="utf-8")
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    # Check if already processed this version
    existing = conn.execute(
        "SELECT content_hash FROM consolidation_file_hashes WHERE file_path = ?",
        (file_path,),
    ).fetchone()

    if existing and existing[0] == content_hash:
        return 0

    try:
        count, usage = await extract_facts(
            content, f"file:{file_path}", provider, conn, confidence_threshold,
        )

        conn.execute(
            "INSERT OR REPLACE INTO consolidation_file_hashes "
            "(file_path, content_hash, last_processed_at) "
            "VALUES (?, ?, datetime('now'))",
            (file_path, content_hash),
        )
        conn.commit()
    except Exception:
        with contextlib.suppress(Exception):
            conn.rollback()
        raise

    _record_extraction_cost(
        usage, metering=metering,
        session_id=f"file:{file_path}",
        model_name=model_name, cost_rates=cost_rates,
    )

    return count
