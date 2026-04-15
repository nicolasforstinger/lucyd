"""Memory interface — PostgreSQL tsvector + pgvector search + structured recall.

FTS-first, vector fallback. Keyword search handles ~80% of queries
without an API call. Vector is the fallback for semantic gaps.

Structured recall (v2): entity-attribute-value facts, episodes,
commitments. Budget-aware context injection via RecallBlock.
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from dataclasses import dataclass
from typing import Any

import httpx

import metrics
from async_utils import run_blocking
from context import _estimate_tokens

log = logging.getLogger(__name__)


class MemoryInterface:
    """PostgreSQL-backed long-term memory with tsvector FTS and pgvector search."""

    def __init__(
        self,
        pool: Any,  # asyncpg.Pool — no stubs available
        client_id: str,
        agent_id: str,
        embedding_api_key: str = "",
        embedding_model: str = "",
        embedding_base_url: str = "",
        embedding_provider: str = "",
        *,
        embedding_timeout: int,
        embedding_cost_rates: list[float] | None = None,
        embedding_currency: str = "EUR",
        top_k: int,
        vector_search_limit: int,
        fts_min_results: int,
    ) -> None:
        self._pool = pool
        self._client_id = client_id
        self._agent_id = agent_id
        self.api_key = embedding_api_key
        self.model = embedding_model
        self.base_url = embedding_base_url.rstrip("/")
        self.provider = embedding_provider
        self.embedding_timeout = embedding_timeout
        self.cost_rates: list[float] = embedding_cost_rates or []
        self.currency: str = embedding_currency
        self.metering: Any = None  # Set externally for embedding cost tracking
        self.converter: Any = None  # Set externally for FX conversion
        self.top_k = top_k
        self.vector_search_limit = vector_search_limit
        self.fts_min_results = fts_min_results

    async def search(self, query: str, top_k: int | None = None) -> list[dict[str, Any]]:
        """Search memory: FTS first, vector fallback."""
        k = top_k or self.top_k
        _search_start = time.time()
        _search_type = "fts"

        try:
            fts_results = await self._fts_search(query, k)
            if len(fts_results) >= self.fts_min_results:
                return fts_results

            # Vector fallback
            if self.api_key:
                _search_type = "combined"
                vector_results = await self._vector_search(query, k)
                seen = {r["id"] for r in fts_results}
                merged = list(fts_results)
                for vr in vector_results:
                    if vr["id"] not in seen:
                        merged.append(vr)
                return sorted(merged, key=lambda x: x.get("score", 0), reverse=True)[:k]

            return fts_results
        finally:
            if metrics.ENABLED:
                metrics.MEMORY_SEARCH_DURATION.labels(search_type=_search_type).observe(
                    time.time() - _search_start,
                )

    async def _fts_search(self, query: str, top_k: int) -> list[dict[str, Any]]:
        """Full-text search via PostgreSQL tsvector.

        ts_rank returns a positive score (higher = more relevant).
        """
        if not query.strip():
            return []
        try:
            rows = await self._pool.fetch(
                """
                SELECT id, path, source, text,
                       ts_rank(search_vector, plainto_tsquery('english', $3)) AS score
                FROM search.chunks
                WHERE client_id = $1 AND agent_id = $2
                  AND search_vector @@ plainto_tsquery('english', $3)
                ORDER BY score DESC
                LIMIT $4
                """,
                self._client_id, self._agent_id, query, top_k,
            )
            return [dict(r) for r in rows]
        except Exception as e:
            log.warning("FTS query failed: %s", e, exc_info=True)
            return []

    async def _vector_search(self, query: str, top_k: int) -> list[dict[str, Any]]:
        """Embed query, search stored embeddings via pgvector cosine distance."""
        query_embedding = await self._embed(query)
        if not query_embedding:
            return []

        # pgvector: cast embedding list to text for the <=> operator.
        embedding_str = "[" + ",".join(str(f) for f in query_embedding) + "]"
        try:
            rows = await self._pool.fetch(
                """
                SELECT id, path, source, text,
                       1 - (embedding <=> $3::vector) AS score
                FROM search.chunks
                WHERE client_id = $1 AND agent_id = $2
                  AND embedding IS NOT NULL
                ORDER BY embedding <=> $3::vector
                LIMIT $4
                """,
                self._client_id, self._agent_id, embedding_str, top_k,
            )
            return [dict(r) for r in rows]
        except Exception as e:
            log.warning("Vector search failed: %s", e, exc_info=True)
            return []

    async def _embed(self, text: str) -> list[float]:
        """Get embedding via OpenAI-compatible API."""
        if not self.base_url or not self.model:
            return []

        cached = await self._get_cached_embedding(text)
        if cached:
            return cached

        url = f"{self.base_url}/embeddings"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = {"model": self.model, "input": text}

        t0 = time.time()
        try:
            def _request() -> dict[str, Any]:
                resp = httpx.post(url, json=payload, headers=headers,
                                  timeout=self.embedding_timeout)
                resp.raise_for_status()
                return resp.json()  # type: ignore[no-any-return]  # httpx.Response.json() returns Any

            data: dict[str, Any] = await run_blocking(_request)
            embedding = data["data"][0]["embedding"]
            latency_ms = int((time.time() - t0) * 1000)

            usage_data = data.get("usage", {})
            from providers import Usage
            usage = Usage(
                input_tokens=usage_data.get("prompt_tokens", 0),
            )
            metrics.record_api_call(
                self.model, self.provider, usage, latency_ms=latency_ms,
            )
            if self.metering:
                await self.metering.record(
                    session_id="embedding",
                    model=self.model, provider=self.provider,
                    usage=usage, cost_rates=self.cost_rates,
                    call_type="embedding", latency_ms=latency_ms,
                    converter=self.converter, currency=self.currency,
                )

            await self._cache_embedding(text, embedding)
            return list(embedding)
        except Exception as e:
            log.error("Embedding failed: %s", e, exc_info=True)
            if metrics.ENABLED:
                metrics.API_CALLS_TOTAL.labels(
                    model=self.model, provider=self.provider, status="error",
                ).inc()
            return []

    async def _get_cached_embedding(self, text: str) -> list[float]:
        """Check embedding cache in PostgreSQL."""
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        try:
            row = await self._pool.fetchrow(
                """SELECT embedding::text FROM search.embedding_cache
                   WHERE client_id = $1 AND agent_id = $2
                   AND hash = $3 AND model = $4""",
                self._client_id, self._agent_id, text_hash, self.model,
            )
            if row:
                # pgvector returns text like '[0.1,0.2,...]'
                raw: str = row["embedding"]
                return [float(x) for x in raw.strip("[]").split(",")]
        except Exception as e:
            log.warning("Embedding cache lookup failed: %s", e, exc_info=True)
        return []

    async def _cache_embedding(self, text: str, embedding: list[float]) -> None:
        """Store embedding in PostgreSQL cache."""
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        embedding_str = "[" + ",".join(str(f) for f in embedding) + "]"
        try:
            await self._pool.execute(
                """INSERT INTO search.embedding_cache
                   (client_id, agent_id, provider, model, provider_key,
                    hash, embedding, dims, updated_at)
                   VALUES ($1, $2, $3, $4, '', $5, $6::vector, $7, now())
                   ON CONFLICT (client_id, agent_id, provider, model, provider_key, hash)
                   DO UPDATE SET embedding = EXCLUDED.embedding,
                                 dims = EXCLUDED.dims,
                                 updated_at = now()""",
                self._client_id, self._agent_id, self.provider, self.model,
                text_hash, embedding_str, len(embedding),
            )
        except Exception as e:
            log.warning("Failed to cache embedding: %s", e, exc_info=True)

    async def get_file_snippet(self, file_path: str,
                               start_line: int = 0, end_line: int = 50) -> str:
        """Retrieve file content from chunks by path and line range."""
        rows = await self._pool.fetch(
            """SELECT text, start_line, end_line FROM search.chunks
               WHERE client_id = $1 AND agent_id = $2
               AND path = $3 AND start_line < $4 AND end_line > $5
               ORDER BY start_line""",
            self._client_id, self._agent_id, file_path, end_line, start_line,
        )
        if not rows:
            return f"No chunks found for {file_path} lines {start_line}-{end_line}"
        return "\n".join(row["text"] for row in rows)


# ─── Structured Recall (Memory v2) ──────────────────────────────

RECALL_PRIORITY_VECTOR = 35
RECALL_PRIORITY_EPISODES = 25
RECALL_PRIORITY_FACTS = 15
RECALL_PRIORITY_COMMITMENTS = 40
RECALL_FACT_FORMAT = "natural"
RECALL_SHOW_EMOTIONAL_TONE = True
RECALL_EPISODE_SECTION_HEADER = "Recent conversations"


@dataclass
class RecallBlock:
    priority: int   # higher = keep longer
    section: str    # e.g. "[Known facts]"
    text: str       # formatted content
    est_tokens: int # from context._estimate_tokens()


def _format_fact(f: Any, fmt: str = "natural") -> str:
    """Format a fact from a Record, dict, tuple, or Row.

    Normalizes input to (entity, attribute, value) then formats.
    """
    if isinstance(f, dict) or hasattr(f, "keys"):
        entity, attr, value = f["entity"], f["attribute"], f["value"]
    elif isinstance(f, (tuple, list)):
        entity, attr, value = f[0], f[1], f[2]
    else:
        raise TypeError(f"_format_fact: expected dict, Record, or tuple, got {type(f).__name__}")
    if fmt == "compact":
        return f"  {entity}.{attr}: {value}"
    return f"  {entity.replace('_', ' ')} — {attr.replace('_', ' ')}: {value}"


async def _build_commitment_block(
    pool: Any,
    client_id: str,
    agent_id: str,
    priority: int,
) -> RecallBlock | None:
    """Build a RecallBlock for open commitments, or None if empty."""
    commitments = await get_open_commitments(pool, client_id, agent_id)
    if not commitments:
        return None
    lines = []
    for c in commitments:
        deadline = f" (by {c['deadline']})" if c["deadline"] else ""
        lines.append(f"  #{c['id']} - {c['who']}: {c['what']}{deadline}")
    text = "\n".join(lines)
    return RecallBlock(
        priority=priority,
        section="[Open commitments]",
        text=text,
        est_tokens=_estimate_tokens(text),
    )


def _format_episode(e: Any, show_tone: bool = True) -> str:
    """Format an episode for recall display.

    Accepts asyncpg.Record (dict-like) or tuple (date, summary, emotional_tone).
    """
    if isinstance(e, (tuple, list)):
        date, summary, tone = e[0], e[1], e[2]
    else:
        date, summary, tone = e["date"], e["summary"], e["emotional_tone"]
    if show_tone and tone and str(tone).lower() != "neutral":
        return f"  [{date}] {summary} (tone: {tone})"
    return f"  [{date}] {summary}"


async def resolve_entity(name: str, pool: Any, client_id: str, agent_id: str) -> str:
    """Resolve an entity name through the alias table."""
    from consolidation import _normalize_entity
    normalized = _normalize_entity(name)
    row = await pool.fetchrow(
        "SELECT canonical FROM knowledge.entity_aliases "
        "WHERE client_id = $1 AND agent_id = $2 AND alias = $3",
        client_id, agent_id, normalized,
    )
    return str(row["canonical"]) if row else normalized


async def extract_query_entities(
    query: str, pool: Any, client_id: str, agent_id: str,
) -> set[str]:
    """Extract known entity names from a natural language query.

    Checks individual words, bigrams, and trigrams against both
    the facts table and the alias table.
    """
    words = [w.strip("?.,!\"'()") for w in query.lower().replace("'s", "").split()]
    candidates = list(words)
    candidates.extend(f"{words[i]}_{words[i+1]}" for i in range(len(words) - 1))
    candidates.extend(f"{words[i]}_{words[i+1]}_{words[i+2]}" for i in range(len(words) - 2))

    entities: set[str] = set()
    for candidate in candidates:
        if not candidate:
            continue

        exists = await pool.fetchval(
            "SELECT 1 FROM knowledge.facts "
            "WHERE client_id = $1 AND agent_id = $2 AND entity = $3 "
            "AND invalidated_at IS NULL LIMIT 1",
            client_id, agent_id, candidate,
        )
        if exists:
            entities.add(candidate)

        canonical = await resolve_entity(candidate, pool, client_id, agent_id)
        if canonical != candidate:
            entities.add(canonical)

    return entities


async def lookup_facts(
    entities: set[str],
    pool: Any,
    client_id: str,
    agent_id: str,
    max_results: int = 20,
) -> list[Any]:
    """Direct fact lookup by entity names.

    Returns current (non-invalidated) facts. Updates accessed_at.
    """
    if not entities:
        return []

    # Build numbered params for the IN clause: $3, $4, $5, ...
    entity_list = list(entities)
    placeholders = ", ".join(f"${i + 3}" for i in range(len(entity_list)))
    limit_idx = len(entity_list) + 3

    rows: list[Any] = await pool.fetch(
        f"SELECT id, entity, attribute, value, confidence "  # noqa: S608 — parameterized
        f"FROM knowledge.facts "
        f"WHERE client_id = $1 AND agent_id = $2 "
        f"AND entity IN ({placeholders}) "
        f"AND invalidated_at IS NULL "
        f"ORDER BY confidence DESC LIMIT ${limit_idx}",
        client_id, agent_id, *entity_list, max_results,
    )

    if rows:
        ids = [r["id"] for r in rows]
        id_placeholders = ", ".join(f"${i + 3}" for i in range(len(ids)))
        await pool.execute(
            f"UPDATE knowledge.facts SET accessed_at = now() "  # noqa: S608 — parameterized
            f"WHERE client_id = $1 AND agent_id = $2 "
            f"AND id IN ({id_placeholders})",
            client_id, agent_id, *ids,
        )

    return rows


async def search_episodes(
    keywords: list[str],
    pool: Any,
    client_id: str,
    agent_id: str,
    max_results: int = 3,
    days_back: int | None = None,
) -> list[Any]:
    """Search episodes by topic keywords and optional date range."""
    conditions: list[str] = ["client_id = $1", "agent_id = $2"]
    params: list[Any] = [client_id, agent_id]
    idx = 3

    if days_back:
        conditions.append(f"date >= CURRENT_DATE - ${idx}::int")
        params.append(days_back)
        idx += 1

    keyword_conditions = []
    for kw in keywords:
        keyword_conditions.append(f"(topics::text ILIKE ${idx} OR summary ILIKE ${idx + 1})")
        params.extend([f"%{kw}%", f"%{kw}%"])
        idx += 2
    if keyword_conditions:
        conditions.append(f"({' OR '.join(keyword_conditions)})")

    where = " AND ".join(conditions)
    params.append(max_results)

    rows: list[Any] = await pool.fetch(
        f"SELECT id, session_id, date, topics, decisions, summary, emotional_tone "  # noqa: S608 — parameterized
        f"FROM knowledge.episodes "
        f"WHERE {where} "
        f"ORDER BY date DESC LIMIT ${idx}",
        *params,
    )
    return rows


async def get_open_commitments(pool: Any, client_id: str, agent_id: str) -> list[Any]:
    """Get all open commitments, ordered by deadline."""
    rows: list[Any] = await pool.fetch(
        """SELECT id, who, what, deadline, created_at
           FROM knowledge.commitments
           WHERE client_id = $1 AND agent_id = $2 AND status = 'open'
           ORDER BY deadline IS NULL, deadline ASC, created_at DESC""",
        client_id, agent_id,
    )
    return rows


async def recall(
    query: str,
    pool: Any,
    client_id: str,
    agent_id: str,
    memory_interface: MemoryInterface,
    config: Any,
    top_k: int = 5,
) -> list[RecallBlock]:
    """Three-stage recall: facts -> episodes -> vector fallback.

    Returns list of RecallBlocks ordered by priority (highest first).
    """
    blocks: list[RecallBlock] = []
    max_facts = getattr(config, "recall_max_facts", 20)
    decay_rate = getattr(config, "recall_decay_rate", 0.03)

    # Stage 1: Structured fact lookup
    entities = await extract_query_entities(query, pool, client_id, agent_id)
    if entities:
        facts = await lookup_facts(entities, pool, client_id, agent_id, max_results=max_facts)
        if facts:
            lines = [_format_fact(f, RECALL_FACT_FORMAT) for f in facts]
            text = "\n".join(lines)
            blocks.append(RecallBlock(
                priority=RECALL_PRIORITY_FACTS,
                section="[Known facts]",
                text=text,
                est_tokens=_estimate_tokens(text),
            ))

    # Stage 2: Episode search
    max_ep = getattr(config, "recall_max_episodes_at_start", 3)
    keywords = [w for w in query.lower().split() if len(w) > 3]
    if keywords:
        episodes = await search_episodes(keywords, pool, client_id, agent_id, max_results=max_ep)
        if episodes:
            lines = [_format_episode(e, RECALL_SHOW_EMOTIONAL_TONE) for e in episodes]
            text = "\n".join(lines)
            blocks.append(RecallBlock(
                priority=RECALL_PRIORITY_EPISODES,
                section=f"[{RECALL_EPISODE_SECTION_HEADER}]",
                text=text,
                est_tokens=_estimate_tokens(text),
            ))

    # Stage 3: Vector search with decay
    vector_results = await memory_interface.search(query, top_k=top_k)
    if vector_results:
        for r in vector_results:
            days_old = r.get("days_old", 0)
            r["decayed_score"] = r["score"] * math.exp(
                -decay_rate * days_old,
            )
        vector_results.sort(key=lambda r: r["decayed_score"], reverse=True)
        lines = [f"  {r['text'][:200]}" for r in vector_results[:top_k]]
        text = "\n".join(lines)
        blocks.append(RecallBlock(
            priority=RECALL_PRIORITY_VECTOR,
            section="[Memory search]",
            text=text,
            est_tokens=_estimate_tokens(text),
        ))

    # Stage 4: Open commitments (always included)
    cb = await _build_commitment_block(pool, client_id, agent_id, RECALL_PRIORITY_COMMITMENTS)
    if cb:
        blocks.append(cb)

    blocks.sort(key=lambda b: b.priority, reverse=True)
    return blocks


def inject_recall(blocks: list[RecallBlock], max_tokens: int) -> str:
    """Apply token budget to recall blocks.

    Sorts blocks by priority (highest first), then adds blocks
    until budget exhausted, dropping lowest-priority blocks.
    Appends a footer showing what was loaded vs. budget, plus
    any dropped sections so the agent knows what it's missing.

    When max_tokens is 0, all blocks are included (unlimited budget).
    """
    blocks = sorted(blocks, key=lambda b: b.priority, reverse=True)
    unlimited = max_tokens == 0
    result = []
    included_sections: list[str] = []
    dropped_sections: list[str] = []
    remaining = float("inf") if unlimited else max_tokens
    for block in blocks:
        if block.est_tokens <= remaining:
            result.append(f"{block.section}\n{block.text}")
            included_sections.append(block.section.strip("[]"))
            remaining -= block.est_tokens
        else:
            dropped_sections.append(block.section.strip("[]"))

    if not result:
        log.debug("Recall budget: no blocks included (0/%s tokens)",
                  "unlimited" if unlimited else max_tokens)
        return ""

    used = sum(b.est_tokens for b in blocks
               if b.section.strip("[]") in included_sections)
    sections_str = ", ".join(included_sections)

    if unlimited:
        log.debug("Recall budget: included=[%s] (%d tokens, no limit)",
                  sections_str, used)
        footer = f"[Memory loaded: {sections_str} | {used} tokens loaded (no budget limit)]"
    else:
        log.debug("Recall budget: included=[%s] (%d/%d tokens), dropped=[%s]",
                  sections_str, used, max_tokens,
                  ", ".join(dropped_sections) if dropped_sections else "none")
        footer = f"[Memory loaded: {sections_str} | {used}/{max_tokens} tokens used]"
    if dropped_sections:
        dropped_str = ", ".join(dropped_sections)
        footer += f"\n[Dropped (over budget): {dropped_str} — use memory_search to access]"
    result.append(footer)
    return "\n\n".join(result)


EMPTY_RECALL_FALLBACK = (
    "No results found in structured memory or vector search. "
    "Try memory_get with workspace-relative paths (e.g., 'memory/YYYY-MM-DD.md', 'MEMORY.md') "
    "to check memory files directly."
)


async def get_session_start_context(
    pool: Any,
    client_id: str,
    agent_id: str,
    config: Any = None,
    max_facts: int = 20,
    max_episodes: int = 3,
    max_tokens: int = 0,
) -> str:
    """Build structured context for the first message of a session.

    Includes facts, recent episodes, and open commitments.
    """
    blocks: list[RecallBlock] = []

    facts = await pool.fetch(
        """SELECT entity, attribute, value
           FROM knowledge.facts
           WHERE client_id = $1 AND agent_id = $2
           AND invalidated_at IS NULL
           ORDER BY accessed_at DESC LIMIT $3""",
        client_id, agent_id, max_facts,
    )

    if facts:
        lines = [_format_fact(f, RECALL_FACT_FORMAT) for f in facts]
        text = "\n".join(lines)
        blocks.append(RecallBlock(
            priority=RECALL_PRIORITY_FACTS,
            section="[Known facts]",
            text=text,
            est_tokens=_estimate_tokens(text),
        ))

    episodes = await pool.fetch(
        """SELECT date, summary, emotional_tone
           FROM knowledge.episodes
           WHERE client_id = $1 AND agent_id = $2
           ORDER BY date DESC LIMIT $3""",
        client_id, agent_id, max_episodes,
    )

    if episodes:
        lines = [_format_episode(e, RECALL_SHOW_EMOTIONAL_TONE) for e in episodes]
        text = "\n".join(lines)
        blocks.append(RecallBlock(
            priority=RECALL_PRIORITY_EPISODES,
            section=f"[{RECALL_EPISODE_SECTION_HEADER}]",
            text=text,
            est_tokens=_estimate_tokens(text),
        ))

    cb = await _build_commitment_block(pool, client_id, agent_id, RECALL_PRIORITY_COMMITMENTS)
    if cb:
        blocks.append(cb)

    return inject_recall(blocks, max_tokens)


async def run_maintenance(
    pool: Any,
    client_id: str,
    agent_id: str,
    stale_threshold_days: int,
) -> dict[str, Any]:
    """Run memory maintenance: stale facts, expired commitments, conflict detection.

    Returns a stats dict with counts for facts, episodes, open_commitments,
    stale, expired, and conflicts.
    """
    stale = await pool.fetch(
        """SELECT id, entity, attribute, value
           FROM knowledge.facts
           WHERE client_id = $1 AND agent_id = $2
           AND invalidated_at IS NULL
           AND now() - accessed_at > make_interval(days => $3)""",
        client_id, agent_id, stale_threshold_days,
    )

    result: str = await pool.execute(
        """UPDATE knowledge.commitments SET status = 'expired'
           WHERE client_id = $1 AND agent_id = $2
           AND status = 'open'
           AND deadline IS NOT NULL
           AND deadline < CURRENT_DATE::text""",
        client_id, agent_id,
    )
    expired = int(result.split()[-1]) if result else 0

    conflicts = await pool.fetch(
        """SELECT f1.entity, f1.attribute,
                  f1.value AS val1, f2.value AS val2
           FROM knowledge.facts f1
           JOIN knowledge.facts f2 ON f1.entity = f2.entity
               AND f1.attribute = f2.attribute
               AND f1.id < f2.id
           WHERE f1.client_id = $1 AND f1.agent_id = $2
           AND f2.client_id = $1 AND f2.agent_id = $2
           AND f1.invalidated_at IS NULL
           AND f2.invalidated_at IS NULL""",
        client_id, agent_id,
    )

    fact_count = await pool.fetchval(
        "SELECT COUNT(*) FROM knowledge.facts "
        "WHERE client_id = $1 AND agent_id = $2 AND invalidated_at IS NULL",
        client_id, agent_id,
    )
    episode_count = await pool.fetchval(
        "SELECT COUNT(*) FROM knowledge.episodes "
        "WHERE client_id = $1 AND agent_id = $2",
        client_id, agent_id,
    )
    commitment_count = await pool.fetchval(
        "SELECT COUNT(*) FROM knowledge.commitments "
        "WHERE client_id = $1 AND agent_id = $2 AND status = 'open'",
        client_id, agent_id,
    )

    return {
        "facts": fact_count or 0,
        "episodes": episode_count or 0,
        "open_commitments": commitment_count or 0,
        "stale": len(stale),
        "expired": expired,
        "conflicts": len(conflicts),
    }
