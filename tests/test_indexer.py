"""Tests for tools/indexer.py — memory indexer."""

import json
import sqlite3
from unittest.mock import patch

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
    rebuild_fts,
    remove_stale_files,
    scan_workspace,
    update_chunks,
)

# ─── Fixtures ────────────────────────────────────────────────────

@pytest.fixture
def index_db(tmp_path):
    """Fresh SQLite DB with production schema."""
    from memory_schema import ensure_schema

    db_path = tmp_path / "memory.sqlite"
    conn = sqlite3.connect(str(db_path))
    ensure_schema(conn)
    conn.close()
    return db_path


@pytest.fixture
def tmp_memory_workspace(tmp_path):
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


def _fake_embeddings(texts, api_key="", base_url="", model=""):
    """Generate deterministic fake embeddings for testing.

    Matches embed_batch(texts, api_key, base_url, model) signature.
    """
    result = []
    for text in texts:
        h = hash(text)
        emb = [(h + j) % 100 / 100.0 for j in range(10)]
        result.append(emb)
    return result


# ─── TestComputeHashes ───────────────────────────────────────────

class TestComputeHashes:
    def test_file_hash_deterministic(self):
        h1 = compute_file_hash("hello world")
        h2 = compute_file_hash("hello world")
        assert h1 == h2

    def test_file_hash_changes_with_content(self):
        h1 = compute_file_hash("hello")
        h2 = compute_file_hash("world")
        assert h1 != h2

    def test_file_hash_is_hex_sha256(self):
        h = compute_file_hash("test")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_chunk_id_includes_path(self):
        id1 = compute_chunk_id("memory/a.md", "same text")
        id2 = compute_chunk_id("memory/b.md", "same text")
        assert id1 != id2

    def test_chunk_id_deterministic(self):
        id1 = compute_chunk_id("p.md", "text")
        id2 = compute_chunk_id("p.md", "text")
        assert id1 == id2

    def test_chunk_id_changes_with_text(self):
        id1 = compute_chunk_id("p.md", "text a")
        id2 = compute_chunk_id("p.md", "text b")
        assert id1 != id2


# ─── TestChunkFile ───────────────────────────────────────────────

class TestChunkFile:
    def test_empty_input(self):
        assert chunk_file([]) == []

    def test_single_chunk(self):
        lines = ["line one", "line two", "line three"]
        chunks = chunk_file(lines, chunk_size=1000, overlap=100)
        assert len(chunks) == 1
        assert chunks[0]["text"] == "line one\nline two\nline three"
        assert chunks[0]["start_line"] == 1
        assert chunks[0]["end_line"] == 3

    def test_multiple_chunks(self):
        # Each line ~20 chars, chunk_size=50 → ~2 lines per chunk
        lines = [f"line number {i:04d} here" for i in range(10)]
        chunks = chunk_file(lines, chunk_size=50, overlap=0)
        assert len(chunks) > 1
        # All lines should be covered
        all_start_lines = {c["start_line"] for c in chunks}
        assert 1 in all_start_lines

    def test_one_indexed_lines(self):
        lines = ["a", "b", "c"]
        chunks = chunk_file(lines, chunk_size=1000)
        assert chunks[0]["start_line"] == 1
        assert chunks[0]["end_line"] == 3

    def test_end_line_inclusive(self):
        lines = ["a", "b"]
        chunks = chunk_file(lines, chunk_size=1000)
        assert chunks[0]["end_line"] == 2

    def test_text_matches_newline_join(self):
        lines = ["alpha", "beta", "gamma"]
        chunks = chunk_file(lines, chunk_size=1000)
        assert chunks[0]["text"] == "\n".join(lines)

    def test_overlap_shares_lines(self):
        # Lines of ~30 chars each, chunk_size=70, overlap=35
        lines = [f"this is line number {i:02d} text" for i in range(6)]
        chunks = chunk_file(lines, chunk_size=70, overlap=35)
        assert len(chunks) >= 2
        # Verify overlap: end of chunk N overlaps with start of chunk N+1
        for i in range(len(chunks) - 1):
            assert chunks[i + 1]["start_line"] <= chunks[i]["end_line"]

    def test_forward_progress_on_huge_line(self):
        # One line that exceeds chunk_size — should still make progress
        lines = ["x" * 5000, "short"]
        chunks = chunk_file(lines, chunk_size=100, overlap=50)
        assert len(chunks) == 2
        assert chunks[0]["text"] == "x" * 5000
        assert chunks[0]["start_line"] == 1
        assert chunks[0]["end_line"] == 1
        assert chunks[1]["start_line"] == 2
        assert chunks[1]["end_line"] == 2

    def test_no_empty_chunks(self):
        lines = [f"line {i}" for i in range(20)]
        chunks = chunk_file(lines, chunk_size=30, overlap=10)
        for c in chunks:
            assert c["text"], "chunk text must not be empty"

    def test_all_lines_covered(self):
        """Every source line appears in at least one chunk."""
        lines = [f"line {i}" for i in range(15)]
        chunks = chunk_file(lines, chunk_size=40, overlap=15)
        covered = set()
        for c in chunks:
            for ln in range(c["start_line"], c["end_line"] + 1):
                covered.add(ln)
        expected = set(range(1, 16))
        assert covered == expected

    def test_character_count_respects_chunk_size(self):
        """Each chunk's text length should be <= chunk_size (unless single line)."""
        lines = [f"line {i:03d}" for i in range(50)]
        chunks = chunk_file(lines, chunk_size=60, overlap=20)
        for c in chunks:
            text = c["text"]
            # Single-line chunks may exceed chunk_size (guaranteed at least 1 line)
            if "\n" in text:
                assert len(text) <= 60 + max(len(line) for line in lines) + 1


# ─── TestScanWorkspace ───────────────────────────────────────────

class TestScanWorkspace:
    def test_finds_daily_logs(self, tmp_memory_workspace):
        results = scan_workspace(tmp_memory_workspace)
        paths = [r[0] for r in results]
        assert "memory/2026-02-15.md" in paths
        assert "memory/2026-02-16.md" in paths

    def test_finds_memory_md(self, tmp_memory_workspace):
        results = scan_workspace(tmp_memory_workspace)
        paths = [r[0] for r in results]
        assert "MEMORY.md" in paths

    def test_excludes_cache_dir(self, tmp_memory_workspace):
        results = scan_workspace(tmp_memory_workspace)
        paths = [r[0] for r in results]
        assert not any("cache" in p for p in paths)

    def test_returns_relative_paths(self, tmp_memory_workspace):
        results = scan_workspace(tmp_memory_workspace)
        for rel, abs_path in results:
            assert not rel.startswith("/")
            assert abs_path.is_absolute()

    def test_returns_sorted(self, tmp_memory_workspace):
        results = scan_workspace(tmp_memory_workspace)
        paths = [r[0] for r in results]
        assert paths == sorted(paths)

    def test_empty_workspace(self, tmp_path):
        ws = tmp_path / "empty"
        ws.mkdir()
        results = scan_workspace(ws)
        assert results == []


# ─── TestGetIndexedFiles ─────────────────────────────────────────

class TestGetIndexedFiles:
    def test_empty_db(self, index_db):
        conn = sqlite3.connect(str(index_db))
        assert get_indexed_files(conn) == {}
        conn.close()

    def test_returns_path_hash_map(self, index_db):
        conn = sqlite3.connect(str(index_db))
        conn.execute(
            "INSERT INTO files (path, source, hash, mtime, size) VALUES (?, ?, ?, ?, ?)",
            ("memory/test.md", "memory", "abc123", 1000, 500),
        )
        conn.commit()
        result = get_indexed_files(conn)
        assert result == {"memory/test.md": "abc123"}
        conn.close()


# ─── TestUpdateChunks ────────────────────────────────────────────

class TestUpdateChunks:
    def test_inserts_chunks(self, index_db):
        conn = sqlite3.connect(str(index_db))
        chunks = [
            {"text": "chunk one", "start_line": 1, "end_line": 5,
             "embedding": [0.1, 0.2, 0.3]},
            {"text": "chunk two", "start_line": 4, "end_line": 10,
             "embedding": [0.4, 0.5, 0.6]},
        ]
        count = update_chunks(conn, "memory/test.md", "memory", chunks,
                              "text-embedding-3-small", "hash123", 1000, 500)
        conn.commit()
        assert count == 2

        rows = conn.execute("SELECT id, path, text FROM chunks ORDER BY start_line").fetchall()
        assert len(rows) == 2
        assert rows[0][1] == "memory/test.md"
        assert rows[0][2] == "chunk one"
        conn.close()

    def test_reindex_replaces_old_chunks(self, index_db):
        conn = sqlite3.connect(str(index_db))
        # First index
        chunks_v1 = [
            {"text": "old content", "start_line": 1, "end_line": 5,
             "embedding": [0.1]},
        ]
        update_chunks(conn, "memory/test.md", "memory", chunks_v1,
                       "text-embedding-3-small", "hash1", 1000, 500)

        # Re-index same path
        chunks_v2 = [
            {"text": "new content", "start_line": 1, "end_line": 3,
             "embedding": [0.2]},
            {"text": "more new content", "start_line": 3, "end_line": 6,
             "embedding": [0.3]},
        ]
        count = update_chunks(conn, "memory/test.md", "memory", chunks_v2,
                               "text-embedding-3-small", "hash2", 2000, 600)
        conn.commit()

        assert count == 2
        rows = conn.execute("SELECT text FROM chunks WHERE path = ? ORDER BY start_line",
                            ("memory/test.md",)).fetchall()
        assert len(rows) == 2
        assert rows[0][0] == "new content"
        conn.close()

    def test_file_record_updated(self, index_db):
        conn = sqlite3.connect(str(index_db))
        chunks = [{"text": "text", "start_line": 1, "end_line": 1,
                    "embedding": [0.1]}]
        update_chunks(conn, "test.md", "memory", chunks,
                       "model", "hash_a", 1000, 100)
        conn.commit()

        row = conn.execute("SELECT hash, mtime, size FROM files WHERE path = 'test.md'").fetchone()
        assert row == ("hash_a", 1000, 100)

        # Update
        update_chunks(conn, "test.md", "memory", chunks,
                       "model", "hash_b", 2000, 200)
        conn.commit()
        row = conn.execute("SELECT hash, mtime, size FROM files WHERE path = 'test.md'").fetchone()
        assert row == ("hash_b", 2000, 200)
        conn.close()


# ─── TestRebuildFts ──────────────────────────────────────────────

class TestRebuildFts:
    def test_fts_rowids_match_chunks(self, index_db):
        conn = sqlite3.connect(str(index_db))
        # Insert some chunks
        for i in range(3):
            conn.execute(
                "INSERT INTO chunks (id, path, source, start_line, end_line, "
                "hash, model, text, embedding, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"id{i}", "test.md", "memory", i + 1, i + 1,
                 f"h{i}", "model", f"chunk text {i}", "[]", 1000),
            )
        rebuild_fts(conn)
        conn.commit()

        # FTS count should match chunks count
        fts_count = conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
        chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        assert fts_count == chunk_count
        conn.close()

    def test_fts_searchable_after_rebuild(self, index_db):
        conn = sqlite3.connect(str(index_db))
        conn.execute(
            "INSERT INTO chunks (id, path, source, start_line, end_line, "
            "hash, model, text, embedding, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("id1", "test.md", "memory", 1, 5, "h1", "model",
             "the quick brown fox jumps over the lazy dog", "[]", 1000),
        )
        rebuild_fts(conn)
        conn.commit()

        rows = conn.execute(
            "SELECT id FROM chunks_fts WHERE chunks_fts MATCH '\"brown\" \"fox\"'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "id1"
        conn.close()

    def test_fts_count_equals_chunk_count(self, index_db):
        conn = sqlite3.connect(str(index_db))
        for i in range(5):
            conn.execute(
                "INSERT INTO chunks (id, path, source, start_line, end_line, "
                "hash, model, text, embedding, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"id{i}", f"file{i}.md", "memory", 1, 1,
                 f"h{i}", "model", f"text {i}", "[]", 1000),
            )
        rebuild_fts(conn)
        conn.commit()

        fts_count = conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
        assert fts_count == 5
        conn.close()


# ─── TestRemoveStale ─────────────────────────────────────────────

class TestRemoveStale:
    def test_removes_chunks_and_file_record(self, index_db):
        conn = sqlite3.connect(str(index_db))
        # Insert a file + chunk
        conn.execute(
            "INSERT INTO files (path, source, hash, mtime, size) VALUES (?, ?, ?, ?, ?)",
            ("old.md", "memory", "hash1", 1000, 100),
        )
        conn.execute(
            "INSERT INTO chunks (id, path, source, start_line, end_line, "
            "hash, model, text, embedding, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("id1", "old.md", "memory", 1, 5, "h1", "model", "old text", "[]", 1000),
        )
        conn.commit()

        removed = remove_stale_files(conn, {"new.md"})
        conn.commit()
        assert removed == ["old.md"]

        # Verify chunks removed
        assert conn.execute("SELECT COUNT(*) FROM chunks WHERE path = 'old.md'").fetchone()[0] == 0
        # Verify file record removed
        assert conn.execute("SELECT COUNT(*) FROM files WHERE path = 'old.md'").fetchone()[0] == 0
        conn.close()

    def test_keeps_non_stale_files(self, index_db):
        conn = sqlite3.connect(str(index_db))
        conn.execute(
            "INSERT INTO files (path, source, hash, mtime, size) VALUES (?, ?, ?, ?, ?)",
            ("keep.md", "memory", "hash1", 1000, 100),
        )
        conn.execute(
            "INSERT INTO files (path, source, hash, mtime, size) VALUES (?, ?, ?, ?, ?)",
            ("remove.md", "memory", "hash2", 1000, 100),
        )
        conn.commit()

        removed = remove_stale_files(conn, {"keep.md"})
        conn.commit()
        assert removed == ["remove.md"]
        assert conn.execute("SELECT COUNT(*) FROM files WHERE path = 'keep.md'").fetchone()[0] == 1
        conn.close()


# ─── TestFtsIntegrity ────────────────────────────────────────────

class TestFtsIntegrity:
    def test_join_works_after_update_and_rebuild(self, index_db):
        """Verify the exact JOIN that memory.py uses works correctly."""
        conn = sqlite3.connect(str(index_db))

        chunks = [
            {"text": "nicolas discussed the portfolio project", "start_line": 1,
             "end_line": 5, "embedding": [0.1, 0.2]},
            {"text": "claudio built the memory indexer", "start_line": 4,
             "end_line": 10, "embedding": [0.3, 0.4]},
        ]
        update_chunks(conn, "memory/test.md", "memory", chunks,
                       "text-embedding-3-small", "hash1", 1000, 500)
        rebuild_fts(conn)
        conn.commit()

        # This is the exact query from memory.py._fts_search
        rows = conn.execute(
            """
            SELECT c.id, c.path, c.source, c.text, fts.rank AS score
            FROM chunks_fts fts
            JOIN chunks c ON c.rowid = fts.rowid
            WHERE chunks_fts MATCH '"portfolio"'
            ORDER BY fts.rank
            LIMIT 10
            """,
        ).fetchall()

        assert len(rows) == 1
        assert "portfolio" in rows[0][3]
        assert rows[0][1] == "memory/test.md"

    def test_all_chunks_joinable(self, index_db):
        """Every chunk has a matching FTS entry via rowid JOIN."""
        conn = sqlite3.connect(str(index_db))

        for i in range(5):
            conn.execute(
                "INSERT INTO chunks (id, path, source, start_line, end_line, "
                "hash, model, text, embedding, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"id{i}", f"file{i}.md", "memory", 1, 1,
                 f"h{i}", "model", f"unique text {i}", "[]", 1000),
            )
        rebuild_fts(conn)
        conn.commit()

        # JOIN should return all rows
        joined = conn.execute(
            "SELECT c.id FROM chunks c JOIN chunks_fts f ON c.rowid = f.rowid"
        ).fetchall()
        assert len(joined) == 5
        conn.close()


# ─── TestEmbedBatch ──────────────────────────────────────────────

class TestEmbedBatch:
    def test_empty_input(self):
        result = embed_batch([], "fake-key")
        assert result == []

    def test_calls_api_and_returns_ordered(self):
        """Mock the API call and verify ordering."""
        fake_response = {
            "data": [
                {"index": 1, "embedding": [0.2, 0.3]},
                {"index": 0, "embedding": [0.1, 0.2]},
            ]
        }

        class FakeResp:
            def read(self):
                return json.dumps(fake_response).encode()

        with patch("tools.indexer.urllib.request.urlopen", return_value=FakeResp()):
            result = embed_batch(["text a", "text b"], "fake-key")

        assert len(result) == 2
        # Should be sorted by index
        assert result[0] == [0.1, 0.2]
        assert result[1] == [0.2, 0.3]


# ─── TestCacheEmbeddings ────────────────────────────────────────

class TestCacheEmbeddings:
    def test_populates_cache(self, index_db):
        conn = sqlite3.connect(str(index_db))
        texts = ["hello world", "test text"]
        embeddings = [[0.1, 0.2], [0.3, 0.4]]

        cache_embeddings(conn, texts, embeddings)
        conn.commit()

        rows = conn.execute(
            "SELECT provider, model, provider_key, dims FROM embedding_cache"
        ).fetchall()
        assert len(rows) == 2
        for row in rows:
            assert row[0] == EMBEDDING_PROVIDER
            assert row[1] == EMBEDDING_MODEL
            assert row[2] == ""  # provider_key
            assert row[3] == 2   # dims
        conn.close()

    def test_cache_lookup_compatible_with_memory_py(self, index_db):
        """Verify our cache entries match memory.py's lookup pattern."""
        import hashlib
        conn = sqlite3.connect(str(index_db))

        text = "test lookup"
        emb = [0.1, 0.2, 0.3]
        cache_embeddings(conn, [text], [emb])
        conn.commit()

        # This is the exact query from memory.py._get_cached_embedding
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        row = conn.execute(
            "SELECT embedding FROM embedding_cache WHERE hash = ? AND model = ?",
            (text_hash, EMBEDDING_MODEL),
        ).fetchone()

        assert row is not None
        assert json.loads(row[0]) == emb
        conn.close()


# ─── TestIndexWorkspace (Integration) ────────────────────────────

class TestIndexWorkspace:
    def test_full_flow(self, index_db, tmp_memory_workspace):
        """Full index with mocked embeddings."""
        with patch("tools.indexer.embed_batch", side_effect=_fake_embeddings):
            summary = index_workspace(
                workspace=tmp_memory_workspace,
                db_path=index_db,
                api_key="fake-key",
            )

        assert len(summary["indexed"]) == 3  # 2 daily logs + MEMORY.md
        assert summary["skipped"] == 0
        assert summary["removed"] == []
        assert summary["total_files"] == 3
        assert summary["total_chunks"] > 0

    def test_skip_unchanged(self, index_db, tmp_memory_workspace):
        """Second run skips unchanged files."""
        with patch("tools.indexer.embed_batch", side_effect=_fake_embeddings) as mock_embed:
            index_workspace(tmp_memory_workspace, index_db, "fake-key")
            first_calls = mock_embed.call_count

            summary = index_workspace(tmp_memory_workspace, index_db, "fake-key")
            second_calls = mock_embed.call_count

        assert summary["skipped"] == 3
        assert len(summary["indexed"]) == 0
        assert second_calls == first_calls  # No new embedding calls

    def test_reindex_changed_file(self, index_db, tmp_memory_workspace):
        """Changed file gets re-indexed."""
        with patch("tools.indexer.embed_batch", side_effect=_fake_embeddings):
            index_workspace(tmp_memory_workspace, index_db, "fake-key")

            # Modify a file
            (tmp_memory_workspace / "memory" / "2026-02-15.md").write_text(
                "# Updated content\n\nnew text here.\n"
            )

            summary = index_workspace(tmp_memory_workspace, index_db, "fake-key")

        assert len(summary["indexed"]) == 1
        assert summary["indexed"][0][0] == "memory/2026-02-15.md"
        assert summary["skipped"] == 2

    def test_remove_deleted_file(self, index_db, tmp_memory_workspace):
        """Deleted file gets removed from DB."""
        with patch("tools.indexer.embed_batch", side_effect=_fake_embeddings):
            index_workspace(tmp_memory_workspace, index_db, "fake-key")

            # Remove a file
            (tmp_memory_workspace / "memory" / "2026-02-16.md").unlink()

            summary = index_workspace(tmp_memory_workspace, index_db, "fake-key")

        assert "memory/2026-02-16.md" in summary["removed"]
        assert summary["total_files"] == 2

    def test_fts_searchable_for_all_content(self, index_db, tmp_memory_workspace):
        """FTS can find content from all indexed files."""
        with patch("tools.indexer.embed_batch", side_effect=_fake_embeddings):
            index_workspace(tmp_memory_workspace, index_db, "fake-key")

        conn = sqlite3.connect(str(index_db))
        # Search for content from daily log
        rows = conn.execute(
            "SELECT c.path FROM chunks_fts fts "
            "JOIN chunks c ON c.rowid = fts.rowid "
            "WHERE chunks_fts MATCH '\"portfolio\"'"
        ).fetchall()
        assert len(rows) >= 1
        assert rows[0][0] == "memory/2026-02-15.md"

        # Search for content from MEMORY.md
        rows = conn.execute(
            "SELECT c.path FROM chunks_fts fts "
            "JOIN chunks c ON c.rowid = fts.rowid "
            "WHERE chunks_fts MATCH '\"familiar\"'"
        ).fetchall()
        assert len(rows) >= 1
        assert rows[0][0] == "MEMORY.md"
        conn.close()

    def test_summary_correct(self, index_db, tmp_memory_workspace):
        with patch("tools.indexer.embed_batch", side_effect=_fake_embeddings):
            summary = index_workspace(tmp_memory_workspace, index_db, "fake-key")

        # Verify DB counts match summary
        conn = sqlite3.connect(str(index_db))
        db_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        db_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        db_fts = conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
        conn.close()

        assert summary["total_chunks"] == db_chunks
        assert summary["total_files"] == db_files
        assert db_fts == db_chunks  # FTS matches chunks

    def test_force_reindexes_all(self, index_db, tmp_memory_workspace):
        """--full flag re-indexes even unchanged files."""
        with patch("tools.indexer.embed_batch", side_effect=_fake_embeddings):
            index_workspace(tmp_memory_workspace, index_db, "fake-key")

            summary = index_workspace(
                tmp_memory_workspace, index_db, "fake-key", force=True,
            )

        assert len(summary["indexed"]) == 3
        assert summary["skipped"] == 0

    def test_missing_api_key_raises(self, index_db, tmp_memory_workspace):
        with pytest.raises(ValueError, match="API key"):
            index_workspace(tmp_memory_workspace, index_db, "")

    def test_embedding_error_skips_file(self, index_db, tmp_memory_workspace):
        """Embedding failure for one file doesn't block others."""
        call_count = [0]

        def failing_embed(texts, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("API down")
            return _fake_embeddings(texts)

        with patch("tools.indexer.embed_batch", side_effect=failing_embed):
            summary = index_workspace(tmp_memory_workspace, index_db, "fake-key")

        assert len(summary["errors"]) == 1
        assert len(summary["indexed"]) == 2  # 2 of 3 succeeded
        assert summary["total_files"] == 2


# ─── TestIdempotency ─────────────────────────────────────────────

class TestIdempotency:
    def test_double_run_same_result(self, index_db, tmp_memory_workspace):
        """Running twice produces identical DB state."""
        with patch("tools.indexer.embed_batch", side_effect=_fake_embeddings):
            index_workspace(tmp_memory_workspace, index_db, "fake-key")

        conn = sqlite3.connect(str(index_db))
        chunks_after_first = conn.execute(
            "SELECT id, path, text FROM chunks ORDER BY id"
        ).fetchall()
        fts_after_first = conn.execute(
            "SELECT COUNT(*) FROM chunks_fts"
        ).fetchone()[0]
        conn.close()

        with patch("tools.indexer.embed_batch", side_effect=_fake_embeddings):
            index_workspace(tmp_memory_workspace, index_db, "fake-key")

        conn = sqlite3.connect(str(index_db))
        chunks_after_second = conn.execute(
            "SELECT id, path, text FROM chunks ORDER BY id"
        ).fetchall()
        fts_after_second = conn.execute(
            "SELECT COUNT(*) FROM chunks_fts"
        ).fetchone()[0]
        conn.close()

        assert chunks_after_first == chunks_after_second
        assert fts_after_first == fts_after_second

    def test_no_embedding_calls_on_second_run(self, index_db, tmp_memory_workspace):
        with patch("tools.indexer.embed_batch", side_effect=_fake_embeddings) as mock:
            index_workspace(tmp_memory_workspace, index_db, "fake-key")
            calls_after_first = mock.call_count

            index_workspace(tmp_memory_workspace, index_db, "fake-key")
            calls_after_second = mock.call_count

        assert calls_after_second == calls_after_first


# ─── TestGetIndexStatus ──────────────────────────────────────────

class TestGetIndexStatus:
    def test_nonexistent_db(self, tmp_path):
        status = get_index_status(tmp_path / "no.db", tmp_path)
        assert status["db_exists"] is False
        assert status["indexed_files"] == 0

    def test_pending_files_detected(self, index_db, tmp_memory_workspace):
        status = get_index_status(index_db, tmp_memory_workspace)
        assert len(status["pending_files"]) == 3  # All files are pending

    def test_no_pending_after_index(self, index_db, tmp_memory_workspace):
        with patch("tools.indexer.embed_batch", side_effect=_fake_embeddings):
            index_workspace(tmp_memory_workspace, index_db, "fake-key")

        status = get_index_status(index_db, tmp_memory_workspace)
        assert status["pending_files"] == []
        assert status["indexed_files"] == 3
        assert status["total_chunks"] > 0

    def test_stale_files_detected(self, index_db, tmp_memory_workspace):
        with patch("tools.indexer.embed_batch", side_effect=_fake_embeddings):
            index_workspace(tmp_memory_workspace, index_db, "fake-key")

        # Remove a file from disk
        (tmp_memory_workspace / "memory" / "2026-02-16.md").unlink()

        status = get_index_status(index_db, tmp_memory_workspace)
        assert "memory/2026-02-16.md" in status["stale_files"]
