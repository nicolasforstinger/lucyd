"""Memory indexer — scan workspace, chunk, embed, write to SQLite.

Incremental and idempotent. Designed to run from cron.
Populates the same SQLite DB that memory.py reads from.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

# ─── Constants (matching existing DB config) ─────────────────────

CHUNK_SIZE_CHARS = 1600       # ~400 tokens at ~4 chars/token
CHUNK_OVERLAP_CHARS = 320     # ~80 tokens overlap
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_PROVIDER = "openai"
EMBEDDING_BASE_URL = "https://api.openai.com/v1"
SOURCE = "memory"
INCLUDE_PATTERNS = ["memory/*.md", "MEMORY.md"]
EXCLUDE_DIRS = {"memory/cache"}
_EMBED_BATCH_LIMIT = 100      # OpenAI API max per call


# ─── Pure Functions ──────────────────────────────────────────────

def compute_file_hash(content: str) -> str:
    """SHA-256 of file content string. For files table change detection."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def compute_chunk_id(path: str, text: str) -> str:
    """Deterministic chunk ID: SHA-256 of 'path:text'."""
    return hashlib.sha256(f"{path}:{text}".encode()).hexdigest()


def chunk_file(
    lines: list[str],
    chunk_size: int = CHUNK_SIZE_CHARS,
    overlap: int = CHUNK_OVERLAP_CHARS,
) -> list[dict]:
    """Split lines into overlapping chunks by character count.

    Returns [{"text": str, "start_line": int, "end_line": int}].
    Lines are 1-indexed, end_line inclusive. Splits on line boundaries
    only. Character count includes joining newlines.
    """
    if not lines:
        return []

    chunks = []
    start_idx = 0

    while start_idx < len(lines):
        # Build chunk: add lines until chunk_size exceeded
        chunk_lines = []
        char_count = 0
        end_idx = start_idx

        while end_idx < len(lines):
            line = lines[end_idx]
            # Newline separator between lines
            added = len(line) + (1 if chunk_lines else 0)
            if char_count + added > chunk_size and chunk_lines:
                break
            chunk_lines.append(line)
            char_count += added
            end_idx += 1

        text = "\n".join(chunk_lines)
        chunks.append({
            "text": text,
            "start_line": start_idx + 1,       # 1-indexed
            "end_line": start_idx + len(chunk_lines),  # inclusive
        })

        if end_idx >= len(lines):
            break

        # Overlap rewind: walk backward from chunk end to find overlap start
        overlap_chars = 0
        overlap_start = end_idx
        for i in range(end_idx - 1, start_idx - 1, -1):
            line_cost = len(lines[i]) + 1  # +1 for newline separator
            if overlap_chars + line_cost > overlap:
                break
            overlap_chars += line_cost
            overlap_start = i

        # Guarantee forward progress
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
            # Normalize path separators
            rel = rel.replace("\\", "/")
            if any(rel.startswith(d + "/") for d in excludes):
                continue
            results[rel] = p
    return sorted(results.items())


# ─── DB Functions ────────────────────────────────────────────────

def get_indexed_files(conn: sqlite3.Connection) -> dict[str, str]:
    """Returns {path: content_hash} from files table."""
    try:
        rows = conn.execute("SELECT path, hash FROM files").fetchall()
        return {row[0]: row[1] for row in rows}
    except sqlite3.OperationalError:
        return {}


def update_chunks(
    conn: sqlite3.Connection,
    path: str,
    source: str,
    chunks: list[dict],
    model: str,
    file_hash: str,
    file_mtime: int,
    file_size: int,
) -> int:
    """Replace all chunks for a file. FTS is rebuilt separately.

    Returns count of chunks inserted.
    """
    now = int(time.time() * 1000)

    # Remove old chunks for this path
    conn.execute("DELETE FROM chunks WHERE path = ?", (path,))

    # Insert new chunks
    for chunk in chunks:
        chunk_id = compute_chunk_id(path, chunk["text"])
        chunk_hash = compute_file_hash(chunk["text"])
        embedding_json = json.dumps(chunk["embedding"]) if chunk.get("embedding") else "[]"
        conn.execute(
            "INSERT INTO chunks (id, path, source, start_line, end_line, "
            "hash, model, text, embedding, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (chunk_id, path, source, chunk["start_line"], chunk["end_line"],
             chunk_hash, model, chunk["text"], embedding_json, now),
        )

    # Update files table
    conn.execute(
        "INSERT OR REPLACE INTO files (path, source, hash, mtime, size) "
        "VALUES (?, ?, ?, ?, ?)",
        (path, source, file_hash, file_mtime, file_size),
    )

    return len(chunks)


def remove_stale_files(
    conn: sqlite3.Connection,
    current_paths: set[str],
) -> list[str]:
    """Remove files and chunks not in current_paths. Returns removed paths."""
    try:
        db_paths = conn.execute("SELECT path FROM files").fetchall()
    except sqlite3.OperationalError:
        return []

    removed = []
    for (db_path,) in db_paths:
        if db_path not in current_paths:
            conn.execute("DELETE FROM chunks WHERE path = ?", (db_path,))
            conn.execute("DELETE FROM files WHERE path = ?", (db_path,))
            removed.append(db_path)
    return removed


def rebuild_fts(conn: sqlite3.Connection) -> None:
    """Full FTS rebuild from chunks table. Called once after all changes."""
    conn.execute("DELETE FROM chunks_fts")
    conn.execute(
        "INSERT INTO chunks_fts(rowid, text, id, path, source, model, start_line, end_line) "
        "SELECT rowid, text, id, path, source, model, start_line, end_line FROM chunks"
    )


# ─── Embedding Functions ────────────────────────────────────────

def embed_batch(
    texts: list[str],
    api_key: str,
    base_url: str = EMBEDDING_BASE_URL,
    model: str = EMBEDDING_MODEL,
) -> list[list[float]]:
    """Embed texts via OpenAI-compatible batch API.

    Returns list of embedding vectors in same order as input.
    Batches into groups of 100 (API limit).
    """
    if not texts:
        return []

    all_embeddings: list[tuple[int, list[float]]] = []

    for batch_start in range(0, len(texts), _EMBED_BATCH_LIMIT):
        batch = texts[batch_start:batch_start + _EMBED_BATCH_LIMIT]

        url = f"{base_url.rstrip('/')}/embeddings"
        payload = json.dumps({
            "model": model,
            "input": batch,
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload, headers={  # noqa: S310 — base_url defaults to hardcoded https://api.openai.com/v1; not user-controlled
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        })

        resp = urllib.request.urlopen(req, timeout=30)  # noqa: S310
        data = json.loads(resp.read().decode("utf-8"))

        for item in data["data"]:
            all_embeddings.append((batch_start + item["index"], item["embedding"]))

    # Sort by original index to preserve order
    all_embeddings.sort(key=lambda x: x[0])
    return [emb for _, emb in all_embeddings]


def cache_embeddings(
    conn: sqlite3.Connection,
    texts: list[str],
    embeddings: list[list[float]],
    model: str = EMBEDDING_MODEL,
    provider: str = EMBEDDING_PROVIDER,
) -> None:
    """Populate embedding_cache so memory.py runtime hits cache."""
    now = int(time.time() * 1000)
    for text, embedding in zip(texts, embeddings, strict=True):
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        conn.execute(
            "INSERT OR REPLACE INTO embedding_cache "
            "(provider, model, provider_key, hash, embedding, dims, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (provider, model, "", text_hash, json.dumps(embedding),
             len(embedding), now),
        )


# ─── Main Entry Point ───────────────────────────────────────────

def index_workspace(
    workspace: Path,
    db_path: Path,
    api_key: str,
    base_url: str = EMBEDDING_BASE_URL,
    model: str = EMBEDDING_MODEL,
    force: bool = False,
) -> dict:
    """Scan workspace, chunk changed files, embed, and write to DB.

    All DB operations happen in one transaction — atomic commit.

    Returns summary dict with indexed/skipped/removed counts.
    """
    if not api_key:
        raise ValueError("API key required for embedding")

    # 1. Scan workspace
    file_list = scan_workspace(workspace)
    scanned_paths = {rel for rel, _ in file_list}

    # 2. Connect to DB and ensure schema exists
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")

    from memory_schema import ensure_schema
    ensure_schema(conn)

    try:
        # 3. Get current index state
        indexed = get_indexed_files(conn)

        summary = {
            "indexed": [],      # [(path, chunk_count)]
            "skipped": 0,
            "removed": [],
            "errors": [],
            "total_files": 0,
            "total_chunks": 0,
        }

        # 4. Process each file
        for rel_path, abs_path in file_list:
            try:
                content = abs_path.read_text(encoding="utf-8")
            except OSError as e:
                log.error("Failed to read %s: %s", rel_path, e)
                summary["errors"].append(f"{rel_path}: read error: {e}")
                continue

            file_hash = compute_file_hash(content)

            # Skip unchanged files (unless force)
            if not force and rel_path in indexed and indexed[rel_path] == file_hash:
                summary["skipped"] += 1
                continue

            # Chunk the file
            lines = content.splitlines()
            chunks = chunk_file(lines)

            if not chunks:
                summary["skipped"] += 1
                continue

            # Embed all chunk texts
            chunk_texts = [c["text"] for c in chunks]
            try:
                embeddings = embed_batch(chunk_texts, api_key, base_url, model)
                for i, emb in enumerate(embeddings):
                    chunks[i]["embedding"] = emb
            except Exception as e:
                log.error("Embedding failed for %s: %s", rel_path, e)
                summary["errors"].append(f"{rel_path}: embedding error: {e}")
                continue

            # Write chunks to DB
            stat = abs_path.stat()
            count = update_chunks(
                conn, rel_path, SOURCE, chunks, model,
                file_hash, int(stat.st_mtime * 1000), stat.st_size,
            )
            summary["indexed"].append((rel_path, count))

            # Cache embeddings for runtime
            cache_embeddings(conn, chunk_texts, embeddings, model)

        # 5. Remove stale files
        removed = remove_stale_files(conn, scanned_paths)
        summary["removed"] = removed

        # 6. Rebuild FTS
        rebuild_fts(conn)

        # 7. Atomic commit
        conn.commit()

        # Final counts
        total_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        total_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        summary["total_files"] = total_files
        summary["total_chunks"] = total_chunks

        return summary

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_index_status(db_path: Path, workspace: Path) -> dict:
    """Get current index status without modifying anything."""
    status = {
        "db_exists": db_path.exists(),
        "indexed_files": 0,
        "total_chunks": 0,
        "pending_files": [],
        "stale_files": [],
    }

    if not db_path.exists():
        return status

    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        indexed = get_indexed_files(conn)
        status["indexed_files"] = len(indexed)

        total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        status["total_chunks"] = total

        # Check for pending/changed files
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

        # Stale files in DB but not on disk
        for db_path_str in indexed:
            if db_path_str not in scanned_paths:
                status["stale_files"].append(db_path_str)

    finally:
        conn.close()

    return status
