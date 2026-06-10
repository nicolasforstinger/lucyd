"""Tests for the GDPR right-to-erasure tools (tools/gdpr.py).

DB-backed via the ``pool`` fixture. Covers search across the data stores and
the delete/redact targets — including the search-index (``search.chunks``)
purge, which keeps redacted workspace text from lingering in FTS/vector hits.
"""

from __future__ import annotations

from typing import Any

import pytest

from tools import gdpr


async def _insert_chunk(pool: Any, chunk_id: str, path: str, text: str) -> None:
    await pool.execute(
        "INSERT INTO search.chunks "
        "(id, path, source, start_line, end_line, hash, model, text) "
        "VALUES ($1, $2, 'memory', 1, 5, 'h', 'test-model', $3)",
        chunk_id, path, text,
    )


# ── Search index (search.chunks) ─────────────────────────────────


@pytest.mark.asyncio
async def test_search_reports_index_chunk_matches(pool: Any) -> None:
    """gdpr_search surfaces PII living in the search index (search.chunks)."""
    gdpr.configure(pool=pool)
    await _insert_chunk(
        pool, "memory/2026-05-25.md:1-5", "memory/2026-05-25.md",
        "Met Alice Bravo to discuss the migration.",
    )

    result = await gdpr.handle_gdpr_search(["Alice Bravo"])

    assert "CHUNK" in result
    assert "memory/2026-05-25.md" in result


@pytest.mark.asyncio
async def test_redact_chunk_purges_all_chunks_for_path(pool: Any) -> None:
    """target='chunk' deletes every index chunk for a file path (they rebuild from source)."""
    gdpr.configure(pool=pool)
    path = "memory/2026-05-25.md"
    await _insert_chunk(pool, f"{path}:1-5", path, "Alice Bravo, line 1.")
    await _insert_chunk(pool, f"{path}:6-9", path, "Alice Bravo, line 2.")

    msg = await gdpr.handle_gdpr_redact(target="chunk", id=0, action="delete", old=path)

    remaining = await pool.fetchval(
        "SELECT count(*) FROM search.chunks WHERE path = $1", path,
    )
    assert remaining == 0
    assert path in msg


# ── Facts + messages (existing behavior characterization) ────────


@pytest.mark.asyncio
async def test_search_then_delete_fact_hard_deletes(pool: Any) -> None:
    """Erasure hard-deletes the fact row — the PII value text is gone, not just flagged."""
    gdpr.configure(pool=pool)
    fid: int = await pool.fetchval(
        "INSERT INTO knowledge.facts (entity, attribute, value) "
        "VALUES ('Alice Bravo', 'email', 'alice@example.com') RETURNING id",
    )

    found = await gdpr.handle_gdpr_search(["alice@example.com"])
    assert "FACT" in found

    msg = await gdpr.handle_gdpr_redact(target="fact", id=fid, action="delete")
    assert "deleted" in msg.lower()

    row = await pool.fetchval(
        "SELECT count(*) FROM knowledge.facts WHERE id = $1", fid,
    )
    assert row == 0


@pytest.mark.asyncio
async def test_redact_chunk_also_purges_cached_embedding(pool: Any) -> None:
    """Purging a path's chunks also clears their cached embeddings (same text hash)."""
    gdpr.configure(pool=pool)
    path = "memory/2026-05-25.md"
    text = "Alice Bravo, line 1."
    # chunks.hash and embedding_cache.hash are both sha256(text) — see the indexer.
    import hashlib
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    await pool.execute(
        "INSERT INTO search.chunks "
        "(id, path, source, start_line, end_line, hash, model, text) "
        "VALUES ($1, $2, 'memory', 1, 5, $3, 'test-model', $4)",
        f"{path}:1-5", path, text_hash, text,
    )
    await pool.execute(
        "INSERT INTO search.embedding_cache "
        "(provider, model, provider_key, hash, embedding, dims) "
        "VALUES ('openai', 'test-model', '', $1, $2::vector, 3)",
        text_hash, "[0.1,0.2,0.3]",
    )

    await gdpr.handle_gdpr_redact(target="chunk", id=0, action="delete", old=path)

    chunks_left = await pool.fetchval(
        "SELECT count(*) FROM search.chunks WHERE path = $1", path,
    )
    cache_left = await pool.fetchval(
        "SELECT count(*) FROM search.embedding_cache WHERE hash = $1", text_hash,
    )
    assert chunks_left == 0
    assert cache_left == 0


@pytest.mark.asyncio
async def test_search_and_delete_download_file(pool: Any, tmp_path: Any) -> None:
    """gdpr_search surfaces a download file with PII; redact target='download' deletes it."""
    dl = tmp_path / "downloads"
    dl.mkdir()
    f = dl / "note.txt"
    f.write_text("Contact Alice Bravo at alice@example.com")

    class _Cfg:
        workspace = tmp_path / "ws"
        http_download_dir = str(dl)
    gdpr.configure(pool=pool, config=_Cfg())  # type: ignore[arg-type]

    found = await gdpr.handle_gdpr_search(["Alice Bravo"])
    assert "DOWNLOAD note.txt" in found

    msg = await gdpr.handle_gdpr_redact(target="download", id=0, action="delete", old="note.txt")
    assert "Deleted download" in msg
    assert not f.exists()


@pytest.mark.asyncio
async def test_download_delete_rejects_path_escape(pool: Any, tmp_path: Any) -> None:
    """A crafted filename can't escape the download dir."""
    dl = tmp_path / "downloads"
    dl.mkdir()

    class _Cfg:
        workspace = tmp_path / "ws"
        http_download_dir = str(dl)
    gdpr.configure(pool=pool, config=_Cfg())  # type: ignore[arg-type]

    # Path() components are stripped to .name, so traversal collapses to a
    # filename in the dir — which won't exist.
    msg = await gdpr.handle_gdpr_redact(
        target="download", id=0, action="delete", old="../../etc/passwd",
    )
    assert "not found" in msg.lower()


@pytest.mark.asyncio
async def test_redact_message_delete_is_refused(pool: Any) -> None:
    """Deleting a message is refused (breaks session structure) — redaction is required instead."""
    gdpr.configure(pool=pool)
    msg = await gdpr.handle_gdpr_redact(target="message", id=1, action="delete")
    assert "cannot be deleted" in msg.lower()
