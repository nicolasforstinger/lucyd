"""Consolidation — extract structured data from conversations and files.

Extracts facts (entity-attribute-value), episodes (narrative summaries),
and commitments (trackable promises) from session transcripts. Stores
in PostgreSQL knowledge schema tables.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

import metrics
from messages import Message
from session import _text_from_content

log = logging.getLogger(__name__)

MAX_EXTRACTION_CHARS = 50_000  # ~12k tokens; configurable via [memory.consolidation] max_extraction_chars


# ─── State Tracking ─────────────────────────────────────────────

async def get_unprocessed_range(
    session_id: str,
    messages: list[Message],
    compaction_count: int,
    pool: Any,
    client_id: str,
    agent_id: str,
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
        "WHERE client_id = $1 AND agent_id = $2 AND session_id = $3",
        client_id, agent_id, session_id,
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
    pool: Any,
    client_id: str,
    agent_id: str,
) -> None:
    await pool.execute(
        """INSERT INTO knowledge.consolidation_state
           (client_id, agent_id, session_id,
            last_compaction_count, last_message_count, last_consolidated_at)
           VALUES ($1, $2, $3, $4, $5, now())
           ON CONFLICT (client_id, agent_id, session_id)
           DO UPDATE SET last_compaction_count = EXCLUDED.last_compaction_count,
                         last_message_count = EXCLUDED.last_message_count,
                         last_consolidated_at = now()""",
        client_id, agent_id, session_id,
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
            text = _text_from_content(msg.get("content", ""))
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


# ─── Shared Helpers ──────────────────────────────────────────────

def _normalize_entity(name: str) -> str:
    return name.lower().strip().replace(" ", "_")


async def upsert_fact(
    entity: str,
    attribute: str,
    value: str,
    pool: Any,
    client_id: str,
    agent_id: str,
    confidence: float = 1.0,
    source_session: str = "",
) -> str:
    """Insert or update a single fact. Returns 'new', 'updated', or 'unchanged'.

    Shared by LLM extraction (_store_facts) and agent tool (memory_write).
    Handles dedup, invalidation-on-change, and accessed_at touch.
    Caller must normalize entity/attribute and resolve aliases beforehand.
    """
    existing = await pool.fetchrow(
        "SELECT id, value FROM knowledge.facts "
        "WHERE client_id = $1 AND agent_id = $2 "
        "AND entity = $3 AND attribute = $4 AND invalidated_at IS NULL",
        client_id, agent_id, entity, attribute,
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
        "(client_id, agent_id, entity, attribute, value, confidence, source_session) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7)",
        client_id, agent_id, entity, attribute, value, confidence, source_session,
    )
    return "updated" if existing else "new"


def _strip_json_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


async def _store_facts(
    data: dict[str, Any],
    session_id: str,
    pool: Any,
    client_id: str,
    agent_id: str,
    confidence_threshold: float,
) -> int:
    """Store extracted facts and aliases in DB. Returns count of new/updated facts."""
    # ORDERING INVARIANT: Store aliases FIRST so entity resolution works
    # for new entities extracted in the same batch.
    for alias_entry in data.get("aliases", []):
        alias = _normalize_entity(alias_entry.get("alias", ""))
        canonical = _normalize_entity(alias_entry.get("canonical", ""))
        if alias and canonical and alias != canonical:
            await pool.execute(
                """INSERT INTO knowledge.entity_aliases
                   (client_id, agent_id, alias, canonical)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT (client_id, agent_id, alias) DO NOTHING""",
                client_id, agent_id, alias, canonical,
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
        entity = await resolve_entity(entity, pool, client_id, agent_id)

        result = await upsert_fact(
            entity, attribute, value, pool, client_id, agent_id,
            confidence=confidence, source_session=session_id,
        )
        if result != "unchanged":
            count += 1

    if count > 0 and metrics.ENABLED:
        metrics.MEMORY_OPS_TOTAL.labels(operation="fact_written").inc(count)
    return count


async def _store_episode(
    data: dict[str, Any],
    session_id: str,
    pool: Any,
    client_id: str,
    agent_id: str,
) -> int | None:
    """Store extracted episode in DB. Returns episode ID or None."""
    episode = data.get("episode", {})
    topics = episode.get("topics", [])
    decisions = episode.get("decisions", [])
    commitments = episode.get("commitments", [])
    summary = episode.get("summary", "")
    emotional_tone = episode.get("emotional_tone", "")

    if (not topics and not decisions and not commitments
            and emotional_tone == "neutral"):
        return None

    if not summary:
        return None

    episode_id: int | None = await pool.fetchval(
        """INSERT INTO knowledge.episodes
           (client_id, agent_id, session_id, topics, decisions,
            commitments, summary, emotional_tone)
           VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6::jsonb, $7, $8)
           RETURNING id""",
        client_id, agent_id, session_id,
        json.dumps(topics), json.dumps(decisions),
        json.dumps(commitments), summary, emotional_tone,
    )
    if metrics.ENABLED:
        metrics.MEMORY_OPS_TOTAL.labels(operation="episode_created").inc()

    for c in commitments:
        who = c.get("who", "")
        what = c.get("what", "")
        deadline = c.get("deadline")
        if deadline == "null":
            deadline = None
        if who and what:
            await pool.execute(
                "INSERT INTO knowledge.commitments "
                "(client_id, agent_id, episode_id, who, what, deadline) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                client_id, agent_id, episode_id, who, what, deadline,
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
    provider: Any,
    system_blocks: list[dict[str, str]],
    prompt_text: str,
    label: str = "extraction",
) -> tuple[dict[str, Any] | None, Any]:
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
        log.warning("%s LLM call failed", label.capitalize(), exc_info=True)
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
    provider: Any,
    pool: Any,
    client_id: str,
    agent_id: str,
    confidence_threshold: float = 0.6,
) -> tuple[int, Any]:
    """Extract facts from text and store in DB (facts-only path for files).

    Returns (count of new/updated facts, usage object or None).
    """
    system_blocks = [{"text": FACT_EXTRACTION_PROMPT, "tier": "stable"}]
    data, usage = await _llm_extract_json(provider, system_blocks, text, "Fact extraction")
    if data is None:
        return 0, usage

    count = await _store_facts(data, session_id, pool, client_id, agent_id, confidence_threshold)
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
    provider: Any,
    system_blocks: list[dict[str, str]],
    pool: Any,
    client_id: str,
    agent_id: str,
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

    facts_added = await _store_facts(data, session_id, pool, client_id, agent_id, confidence_threshold)
    episode_id = await _store_episode(data, session_id, pool, client_id, agent_id)

    return facts_added, episode_id, usage


# ─── Cost Recording ──────────────────────────────────────────────

async def _record_extraction_cost(
    usage: Any,
    *,
    metering: Any = None,
    session_id: str = "",
    model_name: str = "",
    provider_name: str = "",
    cost_rates: list[float] | None = None,
    trace_id: str = "",
    converter: Any = None,
    currency: str = "EUR",
) -> None:
    """Record extraction cost via metering."""
    if not usage or not cost_rates:
        return
    if metering:
        await metering.record(
            session_id=session_id,
            model=model_name, provider=provider_name,
            usage=usage, cost_rates=cost_rates,
            call_type="consolidation", trace_id=trace_id,
            converter=converter, currency=currency,
        )


# ─── Main Entry Point ────────────────────────────────────────────

async def consolidate_session(
    session_id: str,
    messages: list[Message],
    compaction_count: int,
    config: Any,
    provider: Any,
    context_builder: Any,
    pool: Any,
    client_id: str,
    agent_id: str,
    trace_id: str = "",
    metering: Any = None,
    converter: Any = None,
) -> dict[str, Any]:
    """Run full consolidation on a session's messages.

    Uses a single LLM call to extract both facts and episode.
    Returns {"facts_added": int, "episode_id": int | None}
    """
    start_idx, end_idx = await get_unprocessed_range(
        session_id, messages, compaction_count, pool, client_id, agent_id,
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

    persona_blocks = context_builder.build_stable()
    _cons_start = time.time()
    facts_added, episode_id, usage = await extract_structured_data(
        text, session_id, provider, persona_blocks,
        pool, client_id, agent_id, threshold,
    )
    if metrics.ENABLED:
        metrics.CONSOLIDATION_DURATION.observe(time.time() - _cons_start)

    await update_consolidation_state(
        session_id, compaction_count, len(messages),
        pool, client_id, agent_id,
    )

    # Record consolidation cost (non-critical)
    model_role = "primary"
    model_cfg = config.model_config(model_role) if hasattr(config, "model_config") else {}
    cost_rates = model_cfg.get("cost_per_mtok")
    display_name = model_cfg.get("model", model_role)
    provider = model_cfg.get("provider", "")
    currency = model_cfg.get("currency", "EUR")
    await _record_extraction_cost(
        usage, metering=metering, session_id=session_id,
        model_name=display_name, provider_name=provider,
        cost_rates=cost_rates, trace_id=trace_id,
        converter=converter, currency=currency,
    )

    return {"facts_added": facts_added, "episode_id": episode_id}


# ─── Markdown File Extraction ────────────────────────────────────

async def extract_from_file(
    file_path: str,
    provider: Any,
    pool: Any,
    client_id: str,
    agent_id: str,
    confidence_threshold: float = 0.6,
    model_name: str = "",
    provider_name: str = "",
    cost_rates: list[float] | None = None,
    metering: Any = None,
    converter: Any = None,
    currency: str = "EUR",
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
    existing = await pool.fetchrow(
        "SELECT content_hash FROM knowledge.consolidation_file_hashes "
        "WHERE client_id = $1 AND agent_id = $2 AND file_path = $3",
        client_id, agent_id, file_path,
    )

    if existing and existing["content_hash"] == content_hash:
        return 0

    count, usage = await extract_facts(
        content, f"file:{file_path}", provider,
        pool, client_id, agent_id, confidence_threshold,
    )

    await pool.execute(
        """INSERT INTO knowledge.consolidation_file_hashes
           (client_id, agent_id, file_path, content_hash, last_processed_at)
           VALUES ($1, $2, $3, $4, now())
           ON CONFLICT (client_id, agent_id, file_path)
           DO UPDATE SET content_hash = EXCLUDED.content_hash,
                         last_processed_at = now()""",
        client_id, agent_id, file_path, content_hash,
    )

    await _record_extraction_cost(
        usage, metering=metering,
        session_id=f"file:{file_path}",
        model_name=model_name, provider_name=provider_name,
        cost_rates=cost_rates,
        converter=converter, currency=currency,
    )

    return count
