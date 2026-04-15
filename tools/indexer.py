"""Memory indexer — scan workspace, chunk, embed, write to PostgreSQL.

Incremental and idempotent. Designed to run from cron.
Populates the same Postgres tables that memory.py reads from.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any

import httpx

import metrics

log = logging.getLogger(__name__)

# ─── Module config (set by configure(), defaults for standalone use) ──

CHUNK_SIZE_CHARS = 1600       # ~400 tokens at ~4 chars/token
CHUNK_OVERLAP_CHARS = 320     # ~80 tokens overlap
EMBEDDING_MODEL = ""
EMBEDDING_BASE_URL = ""
EMBEDDING_PROVIDER = ""  # Used only for embedding_cache table provider column
SOURCE = "memory"
INCLUDE_PATTERNS = ["memory/*.md", "MEMORY.md"]
EXCLUDE_DIRS = {"memory/cache"}
_EMBED_BATCH_LIMIT = 100

# Cost tracking (set by configure, used by embed_batch)
_METERING: Any = None
_CONVERTER: Any = None
_COST_RATES: list[float] = []
_CURRENCY: str = "EUR"

# Pool + tenant (set by configure, used by index_workspace)
_POOL: Any = None
_CLIENT_ID: str = ""
_AGENT_ID: str = ""


def configure(
    chunk_size: int = 1600,
    chunk_overlap: int = 320,
    embed_batch_limit: int = 100,
    embedding_model: str = "",
    embedding_base_url: str = "",
    embedding_provider: str = "",
    metering: Any = None,
    converter: Any = None,
    cost_rates: list[float] | None = None,
    currency: str = "EUR",
    pool: Any = None,
    client_id: str = "",
    agent_id: str = "",
) -> None:
    """Set indexer config from lucyd.toml values."""
    global CHUNK_SIZE_CHARS, CHUNK_OVERLAP_CHARS, _EMBED_BATCH_LIMIT
    global EMBEDDING_MODEL, EMBEDDING_BASE_URL, EMBEDDING_PROVIDER
    global _METERING, _CONVERTER, _COST_RATES, _CURRENCY
    global _POOL, _CLIENT_ID, _AGENT_ID
    CHUNK_SIZE_CHARS = chunk_size
    CHUNK_OVERLAP_CHARS = chunk_overlap
    _EMBED_BATCH_LIMIT = embed_batch_limit
    EMBEDDING_MODEL = embedding_model
    EMBEDDING_BASE_URL = embedding_base_url
    EMBEDDING_PROVIDER = embedding_provider
    _METERING = metering
    _CONVERTER = converter
    _COST_RATES = cost_rates or []
    _CURRENCY = currency
    _POOL = pool
    _CLIENT_ID = client_id
    _AGENT_ID = agent_id


# ─── Pure Functions ──────────────────────────────────────────────

def compute_file_hash(content: str) -> str:
    """SHA-256 of file content string. For files table change detection."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def compute_chunk_id(path: str, text: str) -> str:
    """Deterministic chunk ID: SHA-256 of 'path:text'."""
    return hashlib.sha256(f"{path}:{text}".encode()).hexdigest()


def chunk_file(
    lines: list[str],
    chunk_size: int | None = None,
    overlap: int | None = None,
) -> list[dict[str, Any]]:
    """Split lines into overlapping chunks by character count.

    Returns [{"text": str, "start_line": int, "end_line": int}].
    Lines are 1-indexed, end_line inclusive. Splits on line boundaries
    only. Character count includes joining newlines.
    """
    if chunk_size is None:
        chunk_size = CHUNK_SIZE_CHARS
    if overlap is None:
        overlap = CHUNK_OVERLAP_CHARS

    if not lines:
        return []

    chunks = []
    start_idx = 0

    while start_idx < len(lines):
        chunk_lines: list[str] = []
        char_count = 0
        end_idx = start_idx

        while end_idx < len(lines):
            line = lines[end_idx]
            added = len(line) + (1 if chunk_lines else 0)
            if char_count + added > chunk_size and chunk_lines:
                break
            chunk_lines.append(line)
            char_count += added
            end_idx += 1

        text = "\n".join(chunk_lines)
        chunks.append({
            "text": text,
            "start_line": start_idx + 1,
            "end_line": start_idx + len(chunk_lines),
        })

        if end_idx >= len(lines):
            break

        overlap_chars = 0
        overlap_start = end_idx
        for i in range(end_idx - 1, start_idx - 1, -1):
            line_cost = len(lines[i]) + 1
            if overlap_chars + line_cost > overlap:
                break
            overlap_chars += line_cost
            overlap_start = i

        start_idx = max(start_idx + 1, overlap_start)

    return chunks


def scan_workspace(
    workspace: Path,
    include_patterns: list[str] | None = None,
    exclude_dirs: set[str] | None = None,
) -> list[tuple[str, Path]]:
    """Scan workspace for indexable memory files.

    Returns [(relative_path, absolute_path)] sorted by path.
    """
    patterns = include_patterns or INCLUDE_PATTERNS
    excludes = exclude_dirs if exclude_dirs is not None else EXCLUDE_DIRS
    results: dict[str, Path] = {}
    for pattern in patterns:
        for p in workspace.glob(pattern):
            if not p.is_file():
                continue
            rel = str(p.relative_to(workspace))
            rel = rel.replace("\\", "/")
            if any(rel.startswith(d + "/") for d in excludes):
                continue
            results[rel] = p
    return sorted(results.items())


# ─── DB Functions (async, PostgreSQL) ────────────────────────────

async def get_indexed_files(pool: Any, client_id: str, agent_id: str) -> dict[str, str]:
    """Returns {path: content_hash} from search.files table."""
    rows = await pool.fetch(
        "SELECT path, hash FROM search.files "
        "WHERE client_id = $1 AND agent_id = $2",
        client_id, agent_id,
    )
    return {row["path"]: row["hash"] for row in rows}


async def update_chunks(
    pool: Any,
    client_id: str,
    agent_id: str,
    path: str,
    source: str,
    chunks: list[dict[str, Any]],
    model: str,
    file_hash: str,
    file_mtime: int,
    file_size: int,
) -> int:
    """Replace all chunks for a file. tsvector is auto-maintained.

    Returns count of chunks inserted.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Remove old chunks for this path
            await conn.execute(
                "DELETE FROM search.chunks "
                "WHERE client_id = $1 AND agent_id = $2 AND path = $3",
                client_id, agent_id, path,
            )

            # Insert new chunks
            for chunk in chunks:
                chunk_id = compute_chunk_id(path, chunk["text"])
                chunk_hash = compute_file_hash(chunk["text"])
                embedding = chunk.get("embedding")
                embedding_str: str | None = None
                if embedding:
                    embedding_str = "[" + ",".join(str(f) for f in embedding) + "]"

                await conn.execute(
                    """INSERT INTO search.chunks
                       (client_id, agent_id, id, path, source,
                        start_line, end_line, hash, model, text,
                        embedding, updated_at)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                               $11::vector, now())""",
                    client_id, agent_id, chunk_id, path, source,
                    chunk["start_line"], chunk["end_line"],
                    chunk_hash, model, chunk["text"],
                    embedding_str,
                )

            # Upsert files table
            await conn.execute(
                """INSERT INTO search.files (client_id, agent_id, path, source, hash, mtime, size)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)
                   ON CONFLICT (client_id, agent_id, path)
                   DO UPDATE SET hash = EXCLUDED.hash,
                                 mtime = EXCLUDED.mtime,
                                 size = EXCLUDED.size""",
                client_id, agent_id, path, source, file_hash, file_mtime, file_size,
            )

    return len(chunks)


async def remove_stale_files(
    pool: Any,
    client_id: str,
    agent_id: str,
    current_paths: set[str],
) -> list[str]:
    """Remove files and chunks not in current_paths. Returns removed paths."""
    rows = await pool.fetch(
        "SELECT path FROM search.files WHERE client_id = $1 AND agent_id = $2",
        client_id, agent_id,
    )
    removed = []
    for row in rows:
        db_path: str = row["path"]
        if db_path not in current_paths:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "DELETE FROM search.chunks "
                        "WHERE client_id = $1 AND agent_id = $2 AND path = $3",
                        client_id, agent_id, db_path,
                    )
                    await conn.execute(
                        "DELETE FROM search.files "
                        "WHERE client_id = $1 AND agent_id = $2 AND path = $3",
                        client_id, agent_id, db_path,
                    )
            removed.append(db_path)
    return removed


# ─── Embedding Functions ────────────────────────────────────────

async def embed_batch(
    texts: list[str],
    api_key: str,
    base_url: str | None = None,
    model: str | None = None,
    embedding_timeout: int = 15,
) -> list[list[float]]:
    """Embed texts via OpenAI-compatible batch API.

    Returns list of embedding vectors in same order as input.
    Batches into groups of 100 (API limit).
    """
    if not texts:
        return []

    base_url = base_url if base_url is not None else EMBEDDING_BASE_URL
    model = model if model is not None else EMBEDDING_MODEL

    all_embeddings: list[tuple[int, list[float]]] = []

    for batch_start in range(0, len(texts), _EMBED_BATCH_LIMIT):
        batch = texts[batch_start:batch_start + _EMBED_BATCH_LIMIT]

        url = f"{base_url.rstrip('/')}/embeddings"

        try:
            resp = httpx.post(url, json={"model": model, "input": batch},
                              headers={"Authorization": f"Bearer {api_key}"},
                              timeout=embedding_timeout)
            resp.raise_for_status()
        except Exception:
            if metrics.ENABLED:
                metrics.API_CALLS_TOTAL.labels(
                    model=model or "", provider=EMBEDDING_PROVIDER, status="error",
                ).inc()
            raise
        data = resp.json()

        for item in data["data"]:
            all_embeddings.append((batch_start + item["index"], item["embedding"]))

        # Record embedding call metrics + cost
        usage_data = data.get("usage", {})
        if usage_data:
            from providers import Usage
            usage = Usage(input_tokens=usage_data.get("prompt_tokens", 0))
            metrics.record_api_call(
                model or "", EMBEDDING_PROVIDER, usage,
            )
            if _METERING and _COST_RATES:
                await _METERING.record(
                    session_id="indexer", model=model or "",
                    provider=EMBEDDING_PROVIDER,
                    usage=usage, cost_rates=_COST_RATES,
                    call_type="embedding", converter=_CONVERTER,
                    currency=_CURRENCY,
                )

    all_embeddings.sort(key=lambda x: x[0])
    return [emb for _, emb in all_embeddings]


async def cache_embeddings(
    pool: Any,
    client_id: str,
    agent_id: str,
    texts: list[str],
    embeddings: list[list[float]],
    model: str | None = None,
    provider: str | None = None,
) -> None:
    """Populate embedding_cache so memory.py runtime hits cache."""
    model = model if model is not None else EMBEDDING_MODEL
    provider = provider if provider is not None else EMBEDDING_PROVIDER
    for text, embedding in zip(texts, embeddings, strict=True):
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        embedding_str = "[" + ",".join(str(f) for f in embedding) + "]"
        await pool.execute(
            """INSERT INTO search.embedding_cache
               (client_id, agent_id, provider, model, provider_key,
                hash, embedding, dims, updated_at)
               VALUES ($1, $2, $3, $4, '', $5, $6::vector, $7, now())
               ON CONFLICT (client_id, agent_id, provider, model, provider_key, hash)
               DO UPDATE SET embedding = EXCLUDED.embedding,
                             dims = EXCLUDED.dims,
                             updated_at = now()""",
            client_id, agent_id, provider, model,
            text_hash, embedding_str, len(embedding),
        )


# ─── Main Entry Point ───────────────────────────────────────────

async def index_workspace(
    workspace: Path,
    pool: Any,
    client_id: str,
    agent_id: str,
    api_key: str,
    base_url: str | None = None,
    model: str | None = None,
    force: bool = False,
    embedding_timeout: int = 15,
) -> dict[str, Any]:
    """Scan workspace, chunk changed files, embed, and write to Postgres.

    Returns summary dict with indexed/skipped/removed counts.
    """
    if not api_key:
        raise ValueError("API key required for embedding")

    base_url = base_url if base_url is not None else EMBEDDING_BASE_URL
    model = model if model is not None else EMBEDDING_MODEL

    # 1. Scan workspace
    file_list = scan_workspace(workspace)
    scanned_paths = {rel for rel, _ in file_list}

    # 2. Get current index state
    indexed = await get_indexed_files(pool, client_id, agent_id)

    summary: dict[str, Any] = {
        "indexed": [],
        "skipped": 0,
        "removed": [],
        "errors": [],
        "total_files": 0,
        "total_chunks": 0,
    }

    # 3. Process each file
    for rel_path, abs_path in file_list:
        try:
            content = abs_path.read_text(encoding="utf-8")
        except OSError as e:
            log.error("Failed to read %s: %s", rel_path, e)
            summary["errors"].append(f"{rel_path}: read error: {e}")
            continue

        file_hash = compute_file_hash(content)

        if not force and rel_path in indexed and indexed[rel_path] == file_hash:
            summary["skipped"] += 1
            continue

        lines = content.splitlines()
        chunks = chunk_file(lines)

        if not chunks:
            summary["skipped"] += 1
            continue

        # Embed all chunk texts
        chunk_texts = [c["text"] for c in chunks]
        try:
            embeddings = await embed_batch(
                chunk_texts, api_key, base_url, model,
                embedding_timeout=embedding_timeout,
            )
            for i, emb in enumerate(embeddings):
                chunks[i]["embedding"] = emb
        except Exception as e:
            log.error("Embedding failed for %s: %s", rel_path, e, exc_info=True)
            summary["errors"].append(f"{rel_path}: embedding error: {e}")
            continue

        # Write chunks to DB
        stat = abs_path.stat()
        count = await update_chunks(
            pool, client_id, agent_id,
            rel_path, SOURCE, chunks, model,
            file_hash, int(stat.st_mtime * 1000), stat.st_size,
        )
        summary["indexed"].append((rel_path, count))

        # Cache embeddings for runtime
        await cache_embeddings(pool, client_id, agent_id, chunk_texts, embeddings, model)

    # 4. Remove stale files
    removed = await remove_stale_files(pool, client_id, agent_id, scanned_paths)
    summary["removed"] = removed

    # 5. Final counts (no FTS rebuild needed — tsvector is auto-maintained)
    total_chunks = await pool.fetchval(
        "SELECT COUNT(*) FROM search.chunks WHERE client_id = $1 AND agent_id = $2",
        client_id, agent_id,
    )
    total_files = await pool.fetchval(
        "SELECT COUNT(*) FROM search.files WHERE client_id = $1 AND agent_id = $2",
        client_id, agent_id,
    )
    summary["total_files"] = total_files or 0
    summary["total_chunks"] = total_chunks or 0

    return summary


async def get_index_status(
    pool: Any,
    client_id: str,
    agent_id: str,
    workspace: Path,
) -> dict[str, Any]:
    """Get current index status without modifying anything."""
    status: dict[str, Any] = {
        "db_exists": True,  # Always true with Postgres
        "indexed_files": 0,
        "total_chunks": 0,
        "pending_files": [],
        "stale_files": [],
    }

    indexed = await get_indexed_files(pool, client_id, agent_id)
    status["indexed_files"] = len(indexed)

    total = await pool.fetchval(
        "SELECT COUNT(*) FROM search.chunks WHERE client_id = $1 AND agent_id = $2",
        client_id, agent_id,
    )
    status["total_chunks"] = total or 0

    file_list = scan_workspace(workspace)
    scanned_paths = {rel for rel, _ in file_list}

    for rel_path, abs_path in file_list:
        try:
            content = abs_path.read_text(encoding="utf-8")
        except OSError:
            continue
        file_hash = compute_file_hash(content)
        if rel_path not in indexed or indexed[rel_path] != file_hash:
            status["pending_files"].append(rel_path)

    for db_path_str in indexed:
        if db_path_str not in scanned_paths:
            status["stale_files"].append(db_path_str)

    return status
