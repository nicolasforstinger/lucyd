"""Memory interface — SQLite FTS5 + vector similarity search.

FTS-first, vector fallback. Keyword search handles ~80% of queries
without an API call. Vector is the fallback for semantic gaps.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import sqlite3
import urllib.request
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
        embedding_model: str = "text-embedding-3-small",
        embedding_base_url: str = "https://api.openai.com/v1",
        embedding_provider: str = "openai",
        top_k: int = 10,
    ):
        self.db_path = db_path
        self.api_key = embedding_api_key
        self.model = embedding_model
        self.base_url = embedding_base_url.rstrip("/")
        self.provider = embedding_provider
        self.top_k = top_k

        if not Path(db_path).exists():
            log.warning("Memory DB not found: %s", db_path)
        else:
            self._ensure_cache_table()

    def _ensure_cache_table(self) -> None:
        """Create embedding_cache table if it doesn't exist."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS embedding_cache (
                    provider TEXT,
                    model TEXT,
                    provider_key TEXT,
                    hash TEXT PRIMARY KEY,
                    embedding TEXT,
                    dims INTEGER,
                    updated_at TEXT
                )
            """)
            conn.commit()
        except Exception as e:
            log.warning("Failed to ensure cache table: %s", e)
        finally:
            conn.close()

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
            conn = sqlite3.connect(self.db_path)
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
            conn = sqlite3.connect(self.db_path)
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
            resp = await asyncio.to_thread(urllib.request.urlopen, req, timeout=15)
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
            conn = sqlite3.connect(self.db_path)
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
            conn = sqlite3.connect(self.db_path)
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
            conn = sqlite3.connect(self.db_path)
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
