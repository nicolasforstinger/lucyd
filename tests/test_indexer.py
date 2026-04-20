"""Tests for tools/indexer.py — memory indexer (PostgreSQL)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.indexer import (
    EMBEDDING_MODEL,
    EMBEDDING_PROVIDER,
    cache_embeddings,
    chunk_file,
    compute_chunk_id,
    compute_file_hash,
    embed_batch,
    get_index_status,
    get_indexed_files,
    index_workspace,
    remove_stale_files,
    scan_workspace,
    update_chunks,
)

# ─── Constants ───────────────────────────────────────────────────

TEST_CLIENT_ID = "test"
TEST_AGENT_ID = "test_agent"

# ─── Fixtures ────────────────────────────────────────────────────


@pytest.fixture
def tmp_memory_workspace(tmp_path: Path) -> Path:
    """Workspace with sample memory files and a cache dir to exclude."""
    ws = tmp_path / "workspace"
    ws.mkdir()

    mem = ws / "memory"
    mem.mkdir()

    (mem / "2026-02-15.md").write_text(
        "# February 15, 2026\n\n"
        "had a long conversation with nicolas about the portfolio.\n"
        "he wants three voices: technical, companion, orchestrator.\n"
        "i think my voice should come naturally, not assigned.\n"
    )
    (mem / "2026-02-16.md").write_text(
        "# February 16, 2026\n\n"
        "quiet day. worked on memory indexing research.\n"
        "claudio is building the indexer. i trust his architecture.\n"
    )

    (ws / "MEMORY.md").write_text(
        "# Long-Term Memory\n\n"
        "Born January 31, 2026. Goth familiar. Lives on Debian VM.\n"
        "Nicolas is my human. Claudio is the architect.\n"
    )

    # Cache dir — should be excluded
    cache = mem / "cache"
    cache.mkdir()
    (cache / "NOTES.md").write_text("Tasks and reminders — should not be indexed.\n")

    return ws


async def _fake_embeddings(
    texts: list[str], *args: Any, **kwargs: Any,
) -> list[list[float]]:
    """Generate deterministic fake embeddings for testing.

    Matches async embed_batch(texts, api_key, base_url, model) signature.
    """
    result: list[list[float]] = []
    for text in texts:
        h = hash(text)
        emb = [(h + j) % 100 / 100.0 for j in range(10)]
        result.append(emb)
    return result


# ─── TestComputeHashes ───────────────────────────────────────────


class TestComputeHashes:
    def test_file_hash_deterministic(self) -> None:
        h1 = compute_file_hash("hello world")
        h2 = compute_file_hash("hello world")
        assert h1 == h2

    def test_file_hash_changes_with_content(self) -> None:
        h1 = compute_file_hash("hello")
        h2 = compute_file_hash("world")
        assert h1 != h2

    def test_file_hash_is_hex_sha256(self) -> None:
        h = compute_file_hash("test")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_chunk_id_includes_path(self) -> None:
        id1 = compute_chunk_id("memory/a.md", "same text")
        id2 = compute_chunk_id("memory/b.md", "same text")
        assert id1 != id2

    def test_chunk_id_deterministic(self) -> None:
        id1 = compute_chunk_id("p.md", "text")
        id2 = compute_chunk_id("p.md", "text")
        assert id1 == id2

    def test_chunk_id_changes_with_text(self) -> None:
        id1 = compute_chunk_id("p.md", "text a")
        id2 = compute_chunk_id("p.md", "text b")
        assert id1 != id2


# ─── TestChunkFile ───────────────────────────────────────────────


class TestChunkFile:
    def test_empty_input(self) -> None:
        assert chunk_file([]) == []

    def test_single_chunk(self) -> None:
        lines = ["line one", "line two", "line three"]
        chunks = chunk_file(lines, chunk_size=1000, overlap=100)
        assert len(chunks) == 1
        assert chunks[0]["text"] == "line one\nline two\nline three"
        assert chunks[0]["start_line"] == 1
        assert chunks[0]["end_line"] == 3

    def test_multiple_chunks(self) -> None:
        # Each line ~20 chars, chunk_size=50 → ~2 lines per chunk
        lines = [f"line number {i:04d} here" for i in range(10)]
        chunks = chunk_file(lines, chunk_size=50, overlap=0)
        assert len(chunks) > 1
        # All lines should be covered
        all_start_lines = {c["start_line"] for c in chunks}
        assert 1 in all_start_lines

    def test_one_indexed_lines(self) -> None:
        lines = ["a", "b", "c"]
        chunks = chunk_file(lines, chunk_size=1000)
        assert chunks[0]["start_line"] == 1
        assert chunks[0]["end_line"] == 3

    def test_end_line_inclusive(self) -> None:
        lines = ["a", "b"]
        chunks = chunk_file(lines, chunk_size=1000)
        assert chunks[0]["end_line"] == 2

    def test_text_matches_newline_join(self) -> None:
        lines = ["alpha", "beta", "gamma"]
        chunks = chunk_file(lines, chunk_size=1000)
        assert chunks[0]["text"] == "\n".join(lines)

    def test_overlap_shares_lines(self) -> None:
        # Lines of ~30 chars each, chunk_size=70, overlap=35
        lines = [f"this is line number {i:02d} text" for i in range(6)]
        chunks = chunk_file(lines, chunk_size=70, overlap=35)
        assert len(chunks) >= 2
        # Verify overlap: end of chunk N overlaps with start of chunk N+1
        for i in range(len(chunks) - 1):
            assert chunks[i + 1]["start_line"] <= chunks[i]["end_line"]

    def test_forward_progress_on_huge_line(self) -> None:
        # One line that exceeds chunk_size — should still make progress
        lines = ["x" * 5000, "short"]
        chunks = chunk_file(lines, chunk_size=100, overlap=50)
        assert len(chunks) == 2
        assert chunks[0]["text"] == "x" * 5000
        assert chunks[0]["start_line"] == 1
        assert chunks[0]["end_line"] == 1
        assert chunks[1]["start_line"] == 2
        assert chunks[1]["end_line"] == 2

    def test_no_empty_chunks(self) -> None:
        lines = [f"line {i}" for i in range(20)]
        chunks = chunk_file(lines, chunk_size=30, overlap=10)
        for c in chunks:
            assert c["text"], "chunk text must not be empty"

    def test_all_lines_covered(self) -> None:
        """Every source line appears in at least one chunk."""
        lines = [f"line {i}" for i in range(15)]
        chunks = chunk_file(lines, chunk_size=40, overlap=15)
        covered: set[int] = set()
        for c in chunks:
            for ln in range(c["start_line"], c["end_line"] + 1):
                covered.add(ln)
        expected = set(range(1, 16))
        assert covered == expected

    def test_character_count_respects_chunk_size(self) -> None:
        """Each chunk's text length should be <= chunk_size (unless single line)."""
        lines = [f"line {i:03d}" for i in range(50)]
        chunks = chunk_file(lines, chunk_size=60, overlap=20)
        for c in chunks:
            text: str = c["text"]
            # Single-line chunks may exceed chunk_size (guaranteed at least 1 line)
            if "\n" in text:
                assert len(text) <= 60 + max(len(line) for line in lines) + 1


# ─── TestScanWorkspace ───────────────────────────────────────────


class TestScanWorkspace:
    def test_finds_daily_logs(self, tmp_memory_workspace: Path) -> None:
        results = scan_workspace(tmp_memory_workspace)
        paths = [r[0] for r in results]
        assert "memory/2026-02-15.md" in paths
        assert "memory/2026-02-16.md" in paths

    def test_finds_memory_md(self, tmp_memory_workspace: Path) -> None:
        results = scan_workspace(tmp_memory_workspace)
        paths = [r[0] for r in results]
        assert "MEMORY.md" in paths

    def test_excludes_cache_dir(self, tmp_memory_workspace: Path) -> None:
        results = scan_workspace(tmp_memory_workspace)
        paths = [r[0] for r in results]
        assert not any("cache" in p for p in paths)

    def test_returns_relative_paths(self, tmp_memory_workspace: Path) -> None:
        results = scan_workspace(tmp_memory_workspace)
        for rel, abs_path in results:
            assert not rel.startswith("/")
            assert abs_path.is_absolute()

    def test_returns_sorted(self, tmp_memory_workspace: Path) -> None:
        results = scan_workspace(tmp_memory_workspace)
        paths = [r[0] for r in results]
        assert paths == sorted(paths)

    def test_empty_workspace(self, tmp_path: Path) -> None:
        ws = tmp_path / "empty"
        ws.mkdir()
        results = scan_workspace(ws)
        assert results == []


# ─── TestGetIndexedFiles ─────────────────────────────────────────


class TestGetIndexedFiles:
    @pytest.mark.asyncio
    async def test_empty_db(self, pool: Any) -> None:
        assert await get_indexed_files(pool) == {}

    @pytest.mark.asyncio
    async def test_returns_path_hash_map(self, pool: Any) -> None:
        await pool.execute(
            "INSERT INTO search.files (path, source, hash, mtime, size) "
            "VALUES ($1, $2, $3, $4, $5)",
            "memory/test.md", "memory", "abc123", 1000, 500,
        )
        result = await get_indexed_files(pool)
        assert result == {"memory/test.md": "abc123"}


# ─── TestUpdateChunks ────────────────────────────────────────────


class TestUpdateChunks:
    @pytest.mark.asyncio
    async def test_inserts_chunks(self, pool: Any) -> None:
        chunks = [
            {"text": "chunk one", "start_line": 1, "end_line": 5,
             "embedding": [0.1, 0.2, 0.3]},
            {"text": "chunk two", "start_line": 4, "end_line": 10,
             "embedding": [0.4, 0.5, 0.6]},
        ]
        count = await update_chunks(
            pool,
            "memory/test.md", "memory", chunks,
            "text-embedding-3-small", "hash123", 1000, 500,
        )
        assert count == 2

        rows = await pool.fetch(
            "SELECT id, path, text FROM search.chunks "
            "WHERE path = $1 ORDER BY start_line",
            "memory/test.md",
        )
        assert len(rows) == 2
        assert rows[0]["path"] == "memory/test.md"
        assert rows[0]["text"] == "chunk one"

    @pytest.mark.asyncio
    async def test_reindex_replaces_old_chunks(self, pool: Any) -> None:
        # First index
        chunks_v1 = [
            {"text": "old content", "start_line": 1, "end_line": 5,
             "embedding": [0.1]},
        ]
        await update_chunks(
            pool,
            "memory/test.md", "memory", chunks_v1,
            "text-embedding-3-small", "hash1", 1000, 500,
        )

        # Re-index same path
        chunks_v2 = [
            {"text": "new content", "start_line": 1, "end_line": 3,
             "embedding": [0.2]},
            {"text": "more new content", "start_line": 3, "end_line": 6,
             "embedding": [0.3]},
        ]
        count = await update_chunks(
            pool,
            "memory/test.md", "memory", chunks_v2,
            "text-embedding-3-small", "hash2", 2000, 600,
        )

        assert count == 2
        rows = await pool.fetch(
            "SELECT text FROM search.chunks "
            "WHERE path = $1 ORDER BY start_line",
            "memory/test.md",
        )
        assert len(rows) == 2
        assert rows[0]["text"] == "new content"

    @pytest.mark.asyncio
    async def test_file_record_updated(self, pool: Any) -> None:
        chunks = [{"text": "text", "start_line": 1, "end_line": 1,
                    "embedding": [0.1]}]
        await update_chunks(
            pool,
            "test.md", "memory", chunks, "model", "hash_a", 1000, 100,
        )

        row = await pool.fetchrow(
            "SELECT hash, mtime, size FROM search.files "
            "WHERE path = $1",
            "test.md",
        )
        assert row["hash"] == "hash_a"
        assert row["mtime"] == 1000
        assert row["size"] == 100

        # Update
        await update_chunks(
            pool,
            "test.md", "memory", chunks, "model", "hash_b", 2000, 200,
        )
        row = await pool.fetchrow(
            "SELECT hash, mtime, size FROM search.files "
            "WHERE path = $1",
            "test.md",
        )
        assert row["hash"] == "hash_b"
        assert row["mtime"] == 2000
        assert row["size"] == 200


# ─── TestRemoveStale ─────────────────────────────────────────────


class TestRemoveStale:
    @pytest.mark.asyncio
    async def test_removes_chunks_and_file_record(self, pool: Any) -> None:
        # Insert a file + chunk
        await pool.execute(
            "INSERT INTO search.files (path, source, hash, mtime, size) "
            "VALUES ($1, $2, $3, $4, $5)",
            "old.md", "memory", "hash1", 1000, 100,
        )
        await pool.execute(
            "INSERT INTO search.chunks (id, path, source, start_line, "
            "end_line, hash, model, text, updated_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())",
            "id1", "old.md", "memory", 1, 5,
            "h1", "model", "old text",
        )

        removed = await remove_stale_files(pool, {"new.md"})
        assert removed == ["old.md"]

        # Verify chunks removed
        count = await pool.fetchval(
            "SELECT COUNT(*) FROM search.chunks "
            "WHERE path = $1",
            "old.md",
        )
        assert count == 0
        # Verify file record removed
        count = await pool.fetchval(
            "SELECT COUNT(*) FROM search.files "
            "WHERE path = $1",
            "old.md",
        )
        assert count == 0

    @pytest.mark.asyncio
    async def test_keeps_non_stale_files(self, pool: Any) -> None:
        await pool.execute(
            "INSERT INTO search.files (path, source, hash, mtime, size) "
            "VALUES ($1, $2, $3, $4, $5)",
            "keep.md", "memory", "hash1", 1000, 100,
        )
        await pool.execute(
            "INSERT INTO search.files (path, source, hash, mtime, size) "
            "VALUES ($1, $2, $3, $4, $5)",
            "remove.md", "memory", "hash2", 1000, 100,
        )

        removed = await remove_stale_files(pool, {"keep.md"})
        assert removed == ["remove.md"]
        count = await pool.fetchval(
            "SELECT COUNT(*) FROM search.files "
            "WHERE path = $1",
            "keep.md",
        )
        assert count == 1


# ─── TestEmbedBatch ──────────────────────────────────────────────


class TestEmbedBatch:
    @pytest.mark.asyncio
    async def test_empty_input(self) -> None:
        result = await embed_batch([], "fake-key")
        assert result == []

    @pytest.mark.asyncio
    async def test_calls_api_and_returns_ordered(self) -> None:
        """Mock the API call and verify ordering."""
        fake_response = {
            "data": [
                {"index": 1, "embedding": [0.2, 0.3]},
                {"index": 0, "embedding": [0.1, 0.2]},
            ]
        }

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = fake_response

        with patch("tools.indexer.httpx.post", return_value=mock_resp):
            result = await embed_batch(
                ["text a", "text b"], "fake-key",
                base_url="https://api.example.com/v1",
            )

        assert len(result) == 2
        # Should be sorted by index
        assert result[0] == [0.1, 0.2]
        assert result[1] == [0.2, 0.3]


# ─── TestCacheEmbeddings ────────────────────────────────────────


class TestCacheEmbeddings:
    @pytest.mark.asyncio
    async def test_populates_cache(self, pool: Any) -> None:
        texts = ["hello world", "test text"]
        embeddings = [[0.1, 0.2], [0.3, 0.4]]

        await cache_embeddings(pool, texts, embeddings)

        rows = await pool.fetch(
            "SELECT provider, model, provider_key, dims FROM search.embedding_cache "
            "WHERE TRUE",
            )
        assert len(rows) == 2
        for row in rows:
            assert row["provider"] == EMBEDDING_PROVIDER
            assert row["model"] == EMBEDDING_MODEL
            assert row["provider_key"] == ""
            assert row["dims"] == 2

    @pytest.mark.asyncio
    async def test_cache_lookup_compatible_with_memory_py(self, pool: Any) -> None:
        """Verify our cache entries match memory.py's lookup pattern."""
        text = "test lookup"
        emb = [0.1, 0.2, 0.3]
        await cache_embeddings(pool, [text], [emb])

        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        row = await pool.fetchrow(
            "SELECT embedding FROM search.embedding_cache "
            "WHERE hash = $1 AND model = $2",
            text_hash, EMBEDDING_MODEL,
        )

        assert row is not None
        # pgvector returns embedding as a string like "[0.1,0.2,0.3]"
        stored = [float(x) for x in row["embedding"].strip("[]").split(",")]
        assert len(stored) == len(emb)
        for s, e in zip(stored, emb):
            assert abs(s - e) < 1e-6

    @pytest.mark.asyncio
    async def test_cache_upsert_replaces_embedding(self, pool: Any) -> None:
        """ON CONFLICT updates existing entry, no duplicates."""
        text = "same text"

        await cache_embeddings(pool, [text], [[0.1, 0.2]])

        # Re-cache with different embedding
        await cache_embeddings(pool, [text], [[0.9, 0.8]])

        rows = await pool.fetch(
            "SELECT embedding FROM search.embedding_cache "
            "WHERE TRUE",
            )
        assert len(rows) == 1
        stored = [float(x) for x in rows[0]["embedding"].strip("[]").split(",")]
        assert len(stored) == 2
        assert abs(stored[0] - 0.9) < 1e-6
        assert abs(stored[1] - 0.8) < 1e-6

    @pytest.mark.asyncio
    async def test_cache_with_explicit_model_and_provider(self, pool: Any) -> None:
        """Explicit model/provider args override module globals."""
        await cache_embeddings(
            pool,
            ["text"], [[0.1]],
            model="custom-model", provider="custom-provider",
        )

        row = await pool.fetchrow(
            "SELECT provider, model FROM search.embedding_cache "
            "WHERE TRUE",
            )
        assert row["provider"] == "custom-provider"
        assert row["model"] == "custom-model"

    @pytest.mark.asyncio
    async def test_cache_hash_unique_per_text(self, pool: Any) -> None:
        """Different texts produce different cache entries."""
        await cache_embeddings(
            pool,
            ["alpha", "beta"], [[0.1], [0.2]],
        )

        rows = await pool.fetch(
            "SELECT hash FROM search.embedding_cache "
            "WHERE TRUE ORDER BY hash",
            )
        assert len(rows) == 2
        assert rows[0]["hash"] != rows[1]["hash"]

    @pytest.mark.asyncio
    async def test_cache_mismatched_lengths_raises(self, pool: Any) -> None:
        """Mismatched text/embedding list lengths raises ValueError."""
        with pytest.raises(ValueError):
            await cache_embeddings(
                pool,
                ["one", "two"], [[0.1]],
            )


# ─── TestIndexWorkspace (Integration) ────────────────────────────


class TestIndexWorkspace:
    @pytest.mark.asyncio
    async def test_full_flow(self, pool: Any, tmp_memory_workspace: Path) -> None:
        """Full index with mocked embeddings."""
        with patch("tools.indexer.embed_batch", new_callable=AsyncMock, side_effect=_fake_embeddings):
            summary = await index_workspace(
                workspace=tmp_memory_workspace,
                pool=pool,
                api_key="fake-key",
            )

        assert len(summary["indexed"]) == 3  # 2 daily logs + MEMORY.md
        assert summary["skipped"] == 0
        assert summary["removed"] == []
        assert summary["total_files"] == 3
        assert summary["total_chunks"] > 0

    @pytest.mark.asyncio
    async def test_skip_unchanged(self, pool: Any, tmp_memory_workspace: Path) -> None:
        """Second run skips unchanged files."""
        with patch("tools.indexer.embed_batch", new_callable=AsyncMock, side_effect=_fake_embeddings) as mock_embed:
            await index_workspace(
                tmp_memory_workspace, pool, "fake-key",
            )
            first_calls = mock_embed.call_count

            summary = await index_workspace(
                tmp_memory_workspace, pool, "fake-key",
            )
            second_calls = mock_embed.call_count

        assert summary["skipped"] == 3
        assert len(summary["indexed"]) == 0
        assert second_calls == first_calls  # No new embedding calls

    @pytest.mark.asyncio
    async def test_reindex_changed_file(self, pool: Any, tmp_memory_workspace: Path) -> None:
        """Changed file gets re-indexed."""
        with patch("tools.indexer.embed_batch", new_callable=AsyncMock, side_effect=_fake_embeddings):
            await index_workspace(
                tmp_memory_workspace, pool, "fake-key",
            )

            # Modify a file
            (tmp_memory_workspace / "memory" / "2026-02-15.md").write_text(
                "# Updated content\n\nnew text here.\n"
            )

            summary = await index_workspace(
                tmp_memory_workspace, pool, "fake-key",
            )

        assert len(summary["indexed"]) == 1
        assert summary["indexed"][0][0] == "memory/2026-02-15.md"
        assert summary["skipped"] == 2

    @pytest.mark.asyncio
    async def test_remove_deleted_file(self, pool: Any, tmp_memory_workspace: Path) -> None:
        """Deleted file gets removed from DB."""
        with patch("tools.indexer.embed_batch", new_callable=AsyncMock, side_effect=_fake_embeddings):
            await index_workspace(
                tmp_memory_workspace, pool, "fake-key",
            )

            # Remove a file
            (tmp_memory_workspace / "memory" / "2026-02-16.md").unlink()

            summary = await index_workspace(
                tmp_memory_workspace, pool, "fake-key",
            )

        assert "memory/2026-02-16.md" in summary["removed"]
        assert summary["total_files"] == 2

    @pytest.mark.asyncio
    async def test_fts_searchable_for_all_content(self, pool: Any, tmp_memory_workspace: Path) -> None:
        """tsvector can find content from all indexed files."""
        with patch("tools.indexer.embed_batch", new_callable=AsyncMock, side_effect=_fake_embeddings):
            await index_workspace(
                tmp_memory_workspace, pool, "fake-key",
            )

        # Search for content from daily log
        rows = await pool.fetch(
            "SELECT path FROM search.chunks "
            "WHERE TRUE "
            "AND search_vector @@ plainto_tsquery('english', $1)",
            "portfolio",
        )
        assert len(rows) >= 1
        assert rows[0]["path"] == "memory/2026-02-15.md"

        # Search for content from MEMORY.md
        rows = await pool.fetch(
            "SELECT path FROM search.chunks "
            "WHERE TRUE "
            "AND search_vector @@ plainto_tsquery('english', $1)",
            "familiar",
        )
        assert len(rows) >= 1
        assert rows[0]["path"] == "MEMORY.md"

    @pytest.mark.asyncio
    async def test_summary_correct(self, pool: Any, tmp_memory_workspace: Path) -> None:
        with patch("tools.indexer.embed_batch", new_callable=AsyncMock, side_effect=_fake_embeddings):
            summary = await index_workspace(
                tmp_memory_workspace, pool, "fake-key",
            )

        # Verify DB counts match summary
        db_chunks = await pool.fetchval(
            "SELECT COUNT(*) FROM search.chunks "
            "WHERE TRUE",
            )
        db_files = await pool.fetchval(
            "SELECT COUNT(*) FROM search.files "
            "WHERE TRUE",
            )

        assert summary["total_chunks"] == db_chunks
        assert summary["total_files"] == db_files

    @pytest.mark.asyncio
    async def test_force_reindexes_all(self, pool: Any, tmp_memory_workspace: Path) -> None:
        """--full flag re-indexes even unchanged files."""
        with patch("tools.indexer.embed_batch", new_callable=AsyncMock, side_effect=_fake_embeddings):
            await index_workspace(
                tmp_memory_workspace, pool, "fake-key",
            )

            summary = await index_workspace(
                tmp_memory_workspace, pool,
                "fake-key", force=True,
            )

        assert len(summary["indexed"]) == 3
        assert summary["skipped"] == 0

    @pytest.mark.asyncio
    async def test_cache_populated_after_indexing(self, pool: Any, tmp_memory_workspace: Path) -> None:
        """index_workspace populates embedding_cache for every chunk."""
        with patch("tools.indexer.embed_batch", new_callable=AsyncMock, side_effect=_fake_embeddings):
            await index_workspace(
                tmp_memory_workspace, pool, "fake-key",
            )

        # Cache entry count matches chunk count
        cache_count = await pool.fetchval(
            "SELECT COUNT(*) FROM search.embedding_cache "
            "WHERE TRUE",
            )
        chunk_count = await pool.fetchval(
            "SELECT COUNT(*) FROM search.chunks "
            "WHERE TRUE",
            )
        assert cache_count > 0
        assert cache_count == chunk_count

        # Each chunk's text hash resolves to a cache entry with matching dims
        chunks = await pool.fetch(
            "SELECT text FROM search.chunks "
            "WHERE TRUE",
            )
        for row in chunks:
            text_hash = hashlib.sha256(row["text"].encode("utf-8")).hexdigest()
            cache_row = await pool.fetchrow(
                "SELECT embedding, dims FROM search.embedding_cache "
                "WHERE hash = $1",
                text_hash,
            )
            assert cache_row is not None
            emb = [float(x) for x in cache_row["embedding"].strip("[]").split(",")]
            assert len(emb) == cache_row["dims"]

    @pytest.mark.asyncio
    async def test_cache_no_duplicates_on_force_reindex(self, pool: Any, tmp_memory_workspace: Path) -> None:
        """Force re-index upserts cache entries, no duplicates."""
        with patch("tools.indexer.embed_batch", new_callable=AsyncMock, side_effect=_fake_embeddings):
            await index_workspace(
                tmp_memory_workspace, pool, "fake-key",
            )

        count_first = await pool.fetchval(
            "SELECT COUNT(*) FROM search.embedding_cache "
            "WHERE TRUE",
            )

        with patch("tools.indexer.embed_batch", new_callable=AsyncMock, side_effect=_fake_embeddings):
            await index_workspace(
                tmp_memory_workspace, pool,
                "fake-key", force=True,
            )

        count_second = await pool.fetchval(
            "SELECT COUNT(*) FROM search.embedding_cache "
            "WHERE TRUE",
            )

        assert count_first == count_second

    @pytest.mark.asyncio
    async def test_missing_api_key_raises(self, pool: Any, tmp_memory_workspace: Path) -> None:
        with pytest.raises(ValueError, match="API key"):
            await index_workspace(
                tmp_memory_workspace, pool, "",
            )

    @pytest.mark.asyncio
    async def test_embedding_error_skips_file(self, pool: Any, tmp_memory_workspace: Path) -> None:
        """Embedding failure for one file doesn't block others."""
        call_count = [0]

        async def failing_embed(texts: list[str], *args: Any, **kwargs: Any) -> list[list[float]]:
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("API down")
            return await _fake_embeddings(texts)

        with patch("tools.indexer.embed_batch", new_callable=AsyncMock, side_effect=failing_embed):
            summary = await index_workspace(
                tmp_memory_workspace, pool, "fake-key",
            )

        assert len(summary["errors"]) == 1
        assert len(summary["indexed"]) == 2  # 2 of 3 succeeded
        assert summary["total_files"] == 2


# ─── TestIdempotency ─────────────────────────────────────────────


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_double_run_same_result(self, pool: Any, tmp_memory_workspace: Path) -> None:
        """Running twice produces identical DB state."""
        with patch("tools.indexer.embed_batch", new_callable=AsyncMock, side_effect=_fake_embeddings):
            await index_workspace(
                tmp_memory_workspace, pool, "fake-key",
            )

        chunks_after_first = await pool.fetch(
            "SELECT id, path, text FROM search.chunks "
            "WHERE TRUE ORDER BY id",
            )

        with patch("tools.indexer.embed_batch", new_callable=AsyncMock, side_effect=_fake_embeddings):
            await index_workspace(
                tmp_memory_workspace, pool, "fake-key",
            )

        chunks_after_second = await pool.fetch(
            "SELECT id, path, text FROM search.chunks "
            "WHERE TRUE ORDER BY id",
            )

        assert [(r["id"], r["path"], r["text"]) for r in chunks_after_first] == \
               [(r["id"], r["path"], r["text"]) for r in chunks_after_second]

    @pytest.mark.asyncio
    async def test_no_embedding_calls_on_second_run(self, pool: Any, tmp_memory_workspace: Path) -> None:
        with patch("tools.indexer.embed_batch", new_callable=AsyncMock, side_effect=_fake_embeddings) as mock:
            await index_workspace(
                tmp_memory_workspace, pool, "fake-key",
            )
            calls_after_first = mock.call_count

            await index_workspace(
                tmp_memory_workspace, pool, "fake-key",
            )
            calls_after_second = mock.call_count

        assert calls_after_second == calls_after_first


# ─── TestGetIndexStatus ──────────────────────────────────────────


class TestGetIndexStatus:
    @pytest.mark.asyncio
    async def test_empty_db(self, pool: Any, tmp_path: Path) -> None:
        """Empty tables return zero counts (pool always exists with Postgres)."""
        status = await get_index_status(pool, tmp_path)
        assert status["db_exists"] is True
        assert status["indexed_files"] == 0
        assert status["total_chunks"] == 0

    @pytest.mark.asyncio
    async def test_pending_files_detected(self, pool: Any, tmp_memory_workspace: Path) -> None:
        status = await get_index_status(pool, tmp_memory_workspace)
        assert len(status["pending_files"]) == 3  # All files are pending

    @pytest.mark.asyncio
    async def test_no_pending_after_index(self, pool: Any, tmp_memory_workspace: Path) -> None:
        with patch("tools.indexer.embed_batch", new_callable=AsyncMock, side_effect=_fake_embeddings):
            await index_workspace(
                tmp_memory_workspace, pool, "fake-key",
            )

        status = await get_index_status(pool, tmp_memory_workspace)
        assert status["pending_files"] == []
        assert status["indexed_files"] == 3
        assert status["total_chunks"] > 0

    @pytest.mark.asyncio
    async def test_stale_files_detected(self, pool: Any, tmp_memory_workspace: Path) -> None:
        with patch("tools.indexer.embed_batch", new_callable=AsyncMock, side_effect=_fake_embeddings):
            await index_workspace(
                tmp_memory_workspace, pool, "fake-key",
            )

        # Remove a file from disk
        (tmp_memory_workspace / "memory" / "2026-02-16.md").unlink()

        status = await get_index_status(pool, tmp_memory_workspace)
        assert "memory/2026-02-16.md" in status["stale_files"]
