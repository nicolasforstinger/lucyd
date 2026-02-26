"""Memory interface — SQLite FTS5 + vector similarity search + structured recall.

FTS-first, vector fallback. Keyword search handles ~80% of queries
without an API call. Vector is the fallback for semantic gaps.

Structured recall (v2): entity-attribute-value facts, episodes,
commitments. Budget-aware context injection via RecallBlock.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import sqlite3
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_VECTOR_SEARCH_LIMIT = 10_000


def cosine_sim(a: list[float], b: list[float]) -> float:
    """Pure-Python cosine similarity."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


class MemoryInterface:
    """SQLite-based long-term memory with FTS5 and vector search."""

    def __init__(
        self,
        db_path: str,
        embedding_api_key: str = "",
        embedding_model: str = "",
        embedding_base_url: str = "",
        embedding_provider: str = "",
        embedding_timeout: int = 15,
        top_k: int = 10,
    ):
        self.db_path = db_path
        self.api_key = embedding_api_key
        self.model = embedding_model
        self.base_url = embedding_base_url.rstrip("/")
        self.provider = embedding_provider
        self.embedding_timeout = embedding_timeout
        self.top_k = top_k

        if not Path(db_path).exists():
            log.warning("Memory DB not found: %s", db_path)

    async def search(self, query: str, top_k: int | None = None) -> list[dict]:
        """Search memory: FTS first, vector fallback."""
        k = top_k or self.top_k

        # Try FTS first
        fts_results = await self._fts_search(query, k)
        # Fall back to vector search if FTS returns fewer than 3 results
        if len(fts_results) >= 3:
            return fts_results

        # Vector fallback
        if self.api_key:
            vector_results = await self._vector_search(query, k)
            # Merge: deduplicate by chunk ID, prefer vector scores
            seen = {r["id"] for r in fts_results}
            merged = list(fts_results)
            for vr in vector_results:
                if vr["id"] not in seen:
                    merged.append(vr)
            return sorted(merged, key=lambda x: x.get("score", 0), reverse=True)[:k]

        return fts_results

    @staticmethod
    def _sanitize_fts5(query: str) -> str:
        """Sanitize query for safe FTS5 MATCH.

        Double-quotes each token so FTS5 treats hyphens, apostrophes,
        and other special characters as literals, not operators.
        """
        # Remove characters that break double-quoting itself
        query = query.replace('"', "")
        tokens = query.split()
        if not tokens:
            return ""
        return " ".join(f'"{t}"' for t in tokens)

    async def _fts_search(self, query: str, top_k: int) -> list[dict]:
        """Full-text search via FTS5."""
        safe_query = self._sanitize_fts5(query)
        if not safe_query:
            return []

        def _query():
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    """
                    SELECT c.id, c.path, c.source, c.text,
                           fts.rank AS score
                    FROM chunks_fts fts
                    JOIN chunks c ON c.rowid = fts.rowid
                    WHERE chunks_fts MATCH ?
                    ORDER BY fts.rank
                    LIMIT ?
                    """,
                    (safe_query, top_k),
                ).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.OperationalError as e:
                log.debug("FTS query failed: %s", e)
                return []
            finally:
                conn.close()

        return await asyncio.to_thread(_query)

    async def _vector_search(self, query: str, top_k: int) -> list[dict]:
        """Embed query, compute cosine similarity against stored embeddings."""
        query_embedding = await self._embed(query)
        if not query_embedding:
            return []

        def _search():
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT id, path, source, text, embedding FROM chunks "
                    "WHERE embedding IS NOT NULL LIMIT ?",
                    (_VECTOR_SEARCH_LIMIT,)
                ).fetchall()
                if len(rows) == _VECTOR_SEARCH_LIMIT:
                    log.warning("Vector search hit %d row limit — results may be incomplete", _VECTOR_SEARCH_LIMIT)
            finally:
                conn.close()

            results = []
            for row in rows:
                row = dict(row)
                emb_json = row.pop("embedding", None)
                if not emb_json:
                    continue
                try:
                    stored_emb = json.loads(emb_json)
                except (json.JSONDecodeError, TypeError):
                    continue
                score = cosine_sim(query_embedding, stored_emb)
                row["score"] = score
                results.append(row)

            results.sort(key=lambda x: x["score"], reverse=True)
            return results[:top_k]

        return await asyncio.to_thread(_search)

    async def _embed(self, text: str) -> list[float]:
        """Get embedding via OpenAI-compatible API."""
        if not self.base_url or not self.model:
            return []

        # Check cache first
        cached = await self._get_cached_embedding(text)
        if cached:
            return cached

        url = f"{self.base_url}/embeddings"
        payload = json.dumps({
            "model": self.model,
            "input": text,
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload, headers={  # noqa: S310 — URL built from config base_url + "/embeddings"; not user-controlled
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        })

        try:
            resp = await asyncio.to_thread(urllib.request.urlopen, req, timeout=self.embedding_timeout)
            data = json.loads(resp.read().decode("utf-8"))
            embedding = data["data"][0]["embedding"]

            # Cache it
            await self._cache_embedding(text, embedding)
            return embedding
        except Exception as e:
            log.error("Embedding failed: %s", e)
            return []

    async def _get_cached_embedding(self, text: str) -> list[float]:
        """Check embedding cache."""
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

        def _query():
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            try:
                row = conn.execute(
                    "SELECT embedding FROM embedding_cache WHERE hash = ? AND model = ?",
                    (text_hash, self.model),
                ).fetchone()
                if row:
                    return json.loads(row[0])
            except Exception as e:
                log.warning("Embedding cache lookup failed: %s", e)
            finally:
                conn.close()
            return []

        return await asyncio.to_thread(_query)

    async def _cache_embedding(self, text: str, embedding: list[float]) -> None:
        """Store embedding in cache."""
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

        def _store():
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO embedding_cache
                       (provider, model, provider_key, hash, embedding, dims, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
                    (self.provider, self.model, "", text_hash,
                     json.dumps(embedding), len(embedding)),
                )
                conn.commit()
            except Exception as e:
                log.warning("Failed to cache embedding: %s", e)
            finally:
                conn.close()

        await asyncio.to_thread(_store)

    async def get_file_snippet(self, file_path: str,
                               start_line: int = 0, end_line: int = 50) -> str:
        """Retrieve file content from chunks by path and line range."""
        def _query():
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            try:
                # Overlap detection: find chunks that intersect the requested range
                rows = conn.execute(
                    """SELECT text, start_line, end_line FROM chunks
                       WHERE path = ? AND start_line < ? AND end_line > ?
                       ORDER BY start_line""",
                    (file_path, end_line, start_line),
                ).fetchall()
            finally:
                conn.close()
            if not rows:
                return f"No chunks found for {file_path} lines {start_line}-{end_line}"
            return "\n".join(row[0] for row in rows)

        return await asyncio.to_thread(_query)


# ─── Structured Recall (Memory v2) ──────────────────────────────

# Default priority assignments (higher = more important, dropped lowest-first).
# These are overridden by config.recall_priority_* when config is available.
_DEFAULT_PRIORITIES = {
    "vector": 35,
    "episodes": 25,
    "facts": 15,
    "commitments": 40,
}


@dataclass
class RecallBlock:
    priority: int   # higher = keep longer
    section: str    # e.g. "[Known facts]"
    text: str       # formatted content
    est_tokens: int # len(text) // 4


def _format_fact_row(f: sqlite3.Row, fmt: str = "natural") -> str:
    """Format a fact from a sqlite3.Row (dict-like access)."""
    if fmt == "compact":
        return f"  {f['entity']}.{f['attribute']}: {f['value']}"
    entity = f["entity"].replace("_", " ")
    attr = f["attribute"].replace("_", " ")
    return f"  {entity} — {attr}: {f['value']}"


def _format_fact_tuple(f: tuple, fmt: str = "natural") -> str:
    """Format a fact from a raw tuple (entity, attribute, value)."""
    if fmt == "compact":
        return f"  {f[0]}.{f[1]}: {f[2]}"
    entity = f[0].replace("_", " ")
    attr = f[1].replace("_", " ")
    return f"  {entity} — {attr}: {f[2]}"


def _format_episode(e, show_tone: bool = True) -> str:
    """Format an episode for recall display.

    Accepts sqlite3.Row (dict-like) or tuple (date, summary, emotional_tone).
    """
    if isinstance(e, sqlite3.Row):
        date, summary, tone = e["date"], e["summary"], e["emotional_tone"]
    else:
        date, summary, tone = e[0], e[1], e[2]
    if show_tone and tone and tone.lower() != "neutral":
        return f"  [{date}] {summary} (tone: {tone})"
    return f"  [{date}] {summary}"


def resolve_entity(name: str, conn: sqlite3.Connection) -> str:
    """Resolve an entity name through the alias table."""
    normalized = name.lower().strip().replace(" ", "_")
    row = conn.execute(
        "SELECT canonical FROM entity_aliases WHERE alias = ?",
        (normalized,)
    ).fetchone()
    return row[0] if row else normalized


def extract_query_entities(query: str, conn: sqlite3.Connection) -> set[str]:
    """Extract known entity names from a natural language query.

    Checks individual words, bigrams, and trigrams against both
    the facts table and the alias table.
    """
    words = query.lower().replace("'s", "").split()
    candidates = []

    # Individual words
    for w in words:
        candidates.append(w.strip("?.,!\"'()"))

    # Bigrams
    for i in range(len(words) - 1):
        w1 = words[i].strip("?.,!\"'()")
        w2 = words[i + 1].strip("?.,!\"'()")
        candidates.append(f"{w1}_{w2}")

    # Trigrams
    for i in range(len(words) - 2):
        w1 = words[i].strip("?.,!\"'()")
        w2 = words[i + 1].strip("?.,!\"'()")
        w3 = words[i + 2].strip("?.,!\"'()")
        candidates.append(f"{w1}_{w2}_{w3}")

    entities = set()
    for candidate in candidates:
        if not candidate:
            continue

        # Check direct entity match
        exists = conn.execute(
            "SELECT 1 FROM facts WHERE entity = ? "
            "AND invalidated_at IS NULL LIMIT 1",
            (candidate,)
        ).fetchone()
        if exists:
            entities.add(candidate)

        # Check alias table
        canonical = resolve_entity(candidate, conn)
        if canonical != candidate:
            entities.add(canonical)

    return entities


def lookup_facts(
    entities: set[str],
    conn: sqlite3.Connection,
    max_results: int = 20,
) -> list[sqlite3.Row]:
    """Direct fact lookup by entity names.

    Returns current (non-invalidated) facts. Updates accessed_at.
    Uses rowid as the implicit integer PK for facts table.
    """
    if not entities:
        return []

    placeholders = ",".join("?" * len(entities))
    rows = conn.execute(f"""
        SELECT id, entity, attribute, value, confidence
        FROM facts
        WHERE entity IN ({placeholders})
          AND invalidated_at IS NULL
        ORDER BY confidence DESC
        LIMIT ?
    """, (*entities, max_results)).fetchall()  # noqa: S608 — placeholders are literal "?" chars, values are parameterized

    if rows:
        ids = [r[0] for r in rows]
        id_placeholders = ",".join("?" * len(ids))
        conn.execute(f"""
            UPDATE facts SET accessed_at = datetime('now')
            WHERE id IN ({id_placeholders})
        """, ids)  # noqa: S608 — placeholders are literal "?" chars from DB rowids, values parameterized
        conn.commit()

    return rows


def search_episodes(
    keywords: list[str],
    conn: sqlite3.Connection,
    max_results: int = 3,
    days_back: int | None = None,
) -> list[sqlite3.Row]:
    """Search episodes by topic keywords and optional date range."""
    conditions: list[str] = []
    params: list = []

    if days_back:
        conditions.append("date >= date('now', ?)")
        params.append(f"-{days_back} days")

    keyword_conditions = []
    for kw in keywords:
        keyword_conditions.append("(topics LIKE ? OR summary LIKE ?)")
        params.extend([f"%{kw}%", f"%{kw}%"])
    if keyword_conditions:
        conditions.append(f"({' OR '.join(keyword_conditions)})")

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"""
        SELECT id, session_id, date, topics, decisions, summary, emotional_tone
        FROM episodes
        {where}
        ORDER BY date DESC LIMIT ?
    """  # noqa: S608 — conditions are hardcoded SQL templates with "?" placeholders; all values parameterized
    params.append(max_results)
    return conn.execute(query, params).fetchall()


def get_open_commitments(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Get all open commitments, ordered by deadline."""
    return conn.execute("""
        SELECT id, who, what, deadline, created_at
        FROM commitments
        WHERE status = 'open'
        ORDER BY deadline IS NULL, deadline ASC, created_at DESC
    """).fetchall()


async def recall(
    query: str,
    conn: sqlite3.Connection,
    memory_interface: MemoryInterface,
    config,
    top_k: int = 5,
) -> list[RecallBlock]:
    """Three-stage recall: facts -> episodes -> vector fallback.

    Returns list of RecallBlocks ordered by priority (highest first).
    Priorities and formatting are config-driven via [memory.recall.personality].
    """
    blocks: list[RecallBlock] = []
    max_facts = getattr(config, "recall_max_facts", 20)
    decay_rate = getattr(config, "recall_decay_rate", 0.03)
    fact_fmt = getattr(config, "recall_fact_format", "natural")
    show_tone = getattr(config, "recall_show_emotional_tone", True)
    p_facts = getattr(config, "recall_priority_facts", _DEFAULT_PRIORITIES["facts"])
    p_episodes = getattr(config, "recall_priority_episodes", _DEFAULT_PRIORITIES["episodes"])
    p_vector = getattr(config, "recall_priority_vector", _DEFAULT_PRIORITIES["vector"])
    p_commitments = getattr(config, "recall_priority_commitments", _DEFAULT_PRIORITIES["commitments"])
    episode_header = getattr(config, "recall_episode_section_header", "Recent conversations")

    # Stage 1: Structured fact lookup
    entities = extract_query_entities(query, conn)
    if entities:
        facts = lookup_facts(entities, conn, max_results=max_facts)
        if facts:
            lines = [_format_fact_row(f, fact_fmt) for f in facts]
            text = "\n".join(lines)
            blocks.append(RecallBlock(
                priority=p_facts,
                section="[Known facts]",
                text=text,
                est_tokens=len(text) // 4,
            ))

    # Stage 2: Episode search
    max_ep = getattr(config, "recall_max_episodes_at_start", 3)
    keywords = [w for w in query.lower().split() if len(w) > 3]
    if keywords:
        episodes = search_episodes(keywords, conn, max_results=max_ep)
        if episodes:
            lines = [_format_episode(e, show_tone) for e in episodes]
            text = "\n".join(lines)
            blocks.append(RecallBlock(
                priority=p_episodes,
                section=f"[{episode_header}]",
                text=text,
                est_tokens=len(text) // 4,
            ))

    # Stage 3: Vector search with decay
    # No pre-throttle — inject_recall() handles budget overflow by dropping
    # lowest-priority blocks. Pre-throttling starves emotional context exactly
    # when structured data is present, which is when warmth matters most.
    vector_results = await memory_interface.search(query, top_k=top_k)
    if vector_results:
        for r in vector_results:
            days_old = r.get("days_old", 0)
            r["decayed_score"] = r["score"] * math.exp(
                -decay_rate * days_old
            )
        vector_results.sort(key=lambda r: r["decayed_score"], reverse=True)
        lines = [f"  {r['text'][:200]}" for r in vector_results[:top_k]]
        text = "\n".join(lines)
        blocks.append(RecallBlock(
            priority=p_vector,
            section="[Memory search]",
            text=text,
            est_tokens=len(text) // 4,
        ))

    # Stage 4: Open commitments (always included)
    commitments = get_open_commitments(conn)
    if commitments:
        lines = []
        for c in commitments:
            deadline = f" (by {c['deadline']})" if c["deadline"] else ""
            lines.append(f"  #{c['id']} - {c['who']}: {c['what']}{deadline}")
        text = "\n".join(lines)
        blocks.append(RecallBlock(
            priority=p_commitments,
            section="[Open commitments]",
            text=text,
            est_tokens=len(text) // 4,
        ))

    blocks.sort(key=lambda b: b.priority, reverse=True)
    return blocks


def inject_recall(blocks: list[RecallBlock], max_tokens: int) -> str:
    """Apply token budget to recall blocks.

    Blocks arrive sorted by priority (highest first). Adds blocks
    until budget exhausted, dropping lowest-priority blocks.
    Appends a footer showing what was loaded vs. budget, plus
    any dropped sections so the agent knows what it's missing.
    """
    result = []
    included_sections = []
    dropped_sections = []
    remaining = max_tokens
    for block in blocks:
        if block.est_tokens <= remaining:
            result.append(f"{block.section}\n{block.text}")
            included_sections.append(block.section.strip("[]"))
            remaining -= block.est_tokens
        else:
            dropped_sections.append(block.section.strip("[]"))

    if not result:
        log.debug("Recall budget: no blocks included (0/%d tokens)", max_tokens)
        return ""

    used = max_tokens - remaining
    log.debug("Recall budget: included=[%s] (%d/%d tokens), dropped=[%s]",
              ", ".join(included_sections), used, max_tokens,
              ", ".join(dropped_sections) if dropped_sections else "none")
    sections_str = ", ".join(included_sections)
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


def get_session_start_context(
    conn: sqlite3.Connection,
    config=None,
    max_facts: int = 20,
    max_episodes: int = 3,
    max_tokens: int = 1500,
) -> str:
    """Build structured context for the first message of a session.

    Includes facts, recent episodes, and open commitments. All formatting
    and priority values are config-driven via [memory.recall.personality].
    """
    # Read config or fall back to defaults
    fact_fmt = getattr(config, "recall_fact_format", "natural") if config else "natural"
    show_tone = getattr(config, "recall_show_emotional_tone", True) if config else True
    episode_header = getattr(config, "recall_episode_section_header", "Recent conversations") if config else "Recent conversations"
    p_facts = getattr(config, "recall_priority_facts", _DEFAULT_PRIORITIES["facts"]) if config else _DEFAULT_PRIORITIES["facts"]
    p_episodes = getattr(config, "recall_priority_episodes", _DEFAULT_PRIORITIES["episodes"]) if config else _DEFAULT_PRIORITIES["episodes"]
    p_commitments = getattr(config, "recall_priority_commitments", _DEFAULT_PRIORITIES["commitments"]) if config else _DEFAULT_PRIORITIES["commitments"]

    blocks: list[RecallBlock] = []

    facts = conn.execute("""
        SELECT entity, attribute, value
        FROM facts
        WHERE invalidated_at IS NULL
        ORDER BY accessed_at DESC
        LIMIT ?
    """, (max_facts,)).fetchall()

    if facts:
        lines = [_format_fact_tuple(f, fact_fmt) for f in facts]
        text = "\n".join(lines)
        blocks.append(RecallBlock(
            priority=p_facts,
            section="[Known facts]",
            text=text,
            est_tokens=len(text) // 4,
        ))

    episodes = conn.execute("""
        SELECT date, summary, emotional_tone
        FROM episodes
        ORDER BY date DESC
        LIMIT ?
    """, (max_episodes,)).fetchall()

    if episodes:
        lines = [_format_episode(e, show_tone) for e in episodes]
        text = "\n".join(lines)
        blocks.append(RecallBlock(
            priority=p_episodes,
            section=f"[{episode_header}]",
            text=text,
            est_tokens=len(text) // 4,
        ))

    commitments = get_open_commitments(conn)
    if commitments:
        lines = []
        for c in commitments:
            deadline = f" (by {c['deadline']})" if c["deadline"] else ""
            lines.append(f"  #{c['id']} - {c['who']}: {c['what']}{deadline}")
        text = "\n".join(lines)
        blocks.append(RecallBlock(
            priority=p_commitments,
            section="[Open commitments]",
            text=text,
            est_tokens=len(text) // 4,
        ))

    return inject_recall(blocks, max_tokens)
