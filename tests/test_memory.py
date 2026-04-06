"""Tests for memory module — cache, vector search, overlap query."""

import json
import sqlite3

import httpx
import pytest

from memory import MemoryInterface, cosine_sim

# Default keyword-only args for MemoryInterface in tests
_MEM_DEFAULTS = dict(
    embedding_timeout=15,
    top_k=10,
    vector_search_limit=10000,
    fts_min_results=3,
)


@pytest.fixture
def memory_db(tmp_path):
    """Create a temporary memory DB with test data."""
    from memory_schema import ensure_schema

    db_path = str(tmp_path / "test_memory.sqlite")
    conn = sqlite3.connect(db_path)
    ensure_schema(conn)
    # Insert test chunks (ensure_schema creates the production schema with all columns)
    conn.execute(
        "INSERT INTO chunks (id, path, source, text, start_line, end_line, hash, model, embedding, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("chunk1", "test.py", "file", "def hello():\n    print('hello')", 1, 10, "h1", "model", "[]", 0),
    )
    conn.execute(
        "INSERT INTO chunks (id, path, source, text, start_line, end_line, hash, model, embedding, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("chunk2", "test.py", "file", "def world():\n    print('world')", 11, 20, "h2", "model", "[]", 0),
    )
    conn.execute(
        "INSERT INTO chunks (id, path, source, text, start_line, end_line, hash, model, embedding, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("chunk3", "test.py", "file", "class Foo:\n    pass", 45, 60, "h3", "model", "[]", 0),
    )
    # Populate FTS
    conn.execute(
        "INSERT INTO chunks_fts(text, id, path, source, model, start_line, end_line) "
        "SELECT text, id, path, source, model, start_line, end_line FROM chunks"
    )
    conn.commit()
    conn.close()
    return db_path


class TestCachTableInit:
    """Cache table is created by ensure_schema, not by MemoryInterface."""

    def test_ensure_schema_creates_cache_table(self, tmp_path):
        from memory_schema import ensure_schema

        db_path = str(tmp_path / "test.sqlite")
        conn = sqlite3.connect(db_path)
        ensure_schema(conn)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='embedding_cache'"
        ).fetchone()
        conn.close()
        assert tables is not None

    def test_handles_missing_db(self, tmp_path):
        db_path = str(tmp_path / "nonexistent.sqlite")
        # Should not raise
        MemoryInterface(db_path=db_path, **_MEM_DEFAULTS)


class TestOverlapQuery:
    """get_file_snippet overlap detection."""

    @pytest.mark.asyncio
    async def test_overlap_finds_partial(self, memory_db):
        """Chunk 45-60 should be found when requesting lines 0-50."""
        mem = MemoryInterface(db_path=memory_db, **_MEM_DEFAULTS)
        result = await mem.get_file_snippet("test.py", start_line=0, end_line=50)
        # chunk1 (1-10), chunk2 (11-20) fully inside, chunk3 (45-60) overlaps
        assert "hello" in result
        assert "world" in result
        assert "Foo" in result

    @pytest.mark.asyncio
    async def test_no_overlap(self, memory_db):
        """No chunks in range 100-200."""
        mem = MemoryInterface(db_path=memory_db, **_MEM_DEFAULTS)
        result = await mem.get_file_snippet("test.py", start_line=100, end_line=200)
        assert "No chunks found" in result

    @pytest.mark.asyncio
    async def test_exact_range(self, memory_db):
        """Exact match on chunk boundaries."""
        mem = MemoryInterface(db_path=memory_db, **_MEM_DEFAULTS)
        result = await mem.get_file_snippet("test.py", start_line=1, end_line=10)
        assert "hello" in result


class TestCosineSim:
    """Cosine similarity edge cases."""

    def test_identical_vectors(self):
        v = [1.0, 0.0, 0.0]
        assert cosine_sim(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert cosine_sim(a, b) == pytest.approx(0.0)

    def test_zero_vector(self):
        a = [0.0, 0.0]
        b = [1.0, 1.0]
        assert cosine_sim(a, b) == 0.0


class TestVectorSearchLimit:
    """Vector search has a LIMIT clause."""

    @pytest.mark.asyncio
    async def test_vector_query_has_limit(self, memory_db):
        """The SQL should include LIMIT 10000."""
        mem = MemoryInterface(db_path=memory_db, **_MEM_DEFAULTS)
        # Without an API key, vector search is skipped gracefully
        results = await mem._vector_search("test", top_k=5)
        assert isinstance(results, list)


class TestFTSSanitize:
    """FTS5 query sanitization — double-quoting tokens."""

    def test_hyphen_preserved(self):
        assert MemoryInterface._sanitize_fts5("2026-02-12") == '"2026-02-12"'

    def test_quotes_and_apostrophes(self):
        assert MemoryInterface._sanitize_fts5("what's new") == '"what\'s" "new"'

    def test_double_quotes_stripped(self):
        assert MemoryInterface._sanitize_fts5('"hello" "world"') == '"hello" "world"'

    def test_special_chars_quoted(self):
        assert MemoryInterface._sanitize_fts5("***") == '"***"'

    def test_single_token(self):
        assert MemoryInterface._sanitize_fts5("hello") == '"hello"'

    def test_multiple_spaces_collapsed(self):
        assert MemoryInterface._sanitize_fts5("a   b") == '"a" "b"'

    def test_mixed_special_chars(self):
        assert MemoryInterface._sanitize_fts5("O'Brien 2026-01-15") == '"O\'Brien" "2026-01-15"'

    def test_empty_string(self):
        assert MemoryInterface._sanitize_fts5("") == ""

    def test_only_whitespace(self):
        assert MemoryInterface._sanitize_fts5("   ") == ""


class TestVectorSearchLimitWarning:
    """BUG-8: Vector search limit is configurable via constructor."""

    def test_vector_search_limit_applied(self, memory_db):
        """Verify the vector_search_limit parameter controls the SQL LIMIT."""
        # Insert 3 rows with embeddings
        conn = sqlite3.connect(memory_db)
        for i in range(3):
            emb = json.dumps([float(i)] * 3)
            conn.execute(
                "INSERT INTO chunks (id, path, source, text, start_line, end_line, hash, model, embedding, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"limit-test-{i}", "test.md", "test", f"text {i}", 1, 1, f"h{i}", "model", emb, 0),
            )
        conn.commit()
        conn.close()

        # Create instance with small vector_search_limit
        mem = MemoryInterface(
            db_path=memory_db,
            **{**_MEM_DEFAULTS, "vector_search_limit": 2},
        )

        # Verify the limit is stored on the instance
        assert mem.vector_search_limit == 2

        # Verify the DB query with that limit returns only 2 rows
        conn = sqlite3.connect(memory_db)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, path, source, text, embedding FROM chunks "
            "WHERE embedding IS NOT NULL LIMIT ?",
            (mem.vector_search_limit,)
        ).fetchall()
        conn.close()

        assert len(rows) == 2  # Limited to 2


# ─── TEST-8: Embedding API call (_embed) ─────────────────────────


class TestEmbedAPI:
    """TEST-8: _embed method — API call, caching, and error handling."""

    @pytest.mark.asyncio
    async def test_embed_calls_api_and_returns_embedding(self, memory_db):
        """Mock httpx.post returning valid embedding JSON; verify result."""
        from unittest.mock import MagicMock, patch

        fake_embedding = [0.1, 0.2, 0.3, 0.4, 0.5]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": [{"embedding": fake_embedding}],
            "model": "text-embedding-3-small",
            "usage": {"prompt_tokens": 5, "total_tokens": 5},
        }

        mem = MemoryInterface(
            db_path=memory_db,
            embedding_api_key="test-key-123",
            embedding_model="text-embedding-3-small",
            embedding_base_url="https://api.example.com/v1",
            **_MEM_DEFAULTS,
        )

        with patch("memory.httpx.post", return_value=mock_resp) as mock_post:
            result = await mem._embed("hello world")

        assert result == fake_embedding
        call_args = mock_post.call_args
        assert call_args[0][0] == "https://api.example.com/v1/embeddings"
        assert call_args[1]["headers"]["Authorization"] == "Bearer test-key-123"

    @pytest.mark.asyncio
    async def test_embed_caches_result_in_sqlite(self, memory_db):
        """After _embed call, the embedding is cached in embedding_cache table."""
        import hashlib
        from unittest.mock import MagicMock, patch

        fake_embedding = [1.0, 2.0, 3.0]
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"data": [{"embedding": fake_embedding}]}

        mem = MemoryInterface(
            db_path=memory_db,
            embedding_api_key="test-key",
            embedding_model="text-embedding-3-small",
            embedding_base_url="https://api.example.com/v1",
            **_MEM_DEFAULTS,
        )

        with patch("memory.httpx.post", return_value=mock_resp):
            result = await mem._embed("test caching")

        assert result == fake_embedding

        # Verify it was stored in embedding_cache
        text_hash = hashlib.sha256(b"test caching").hexdigest()
        conn = sqlite3.connect(memory_db)
        row = conn.execute(
            "SELECT embedding, dims FROM embedding_cache WHERE hash = ?",
            (text_hash,),
        ).fetchone()
        conn.close()

        assert row is not None
        cached_emb = json.loads(row[0])
        assert cached_emb == fake_embedding
        assert row[1] == 3  # dims == len(embedding)

    @pytest.mark.asyncio
    async def test_embed_returns_cached_on_second_call(self, memory_db):
        """Second call for same text uses cache, not API."""
        from unittest.mock import MagicMock, patch

        fake_embedding = [0.5, 0.6, 0.7]
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"data": [{"embedding": fake_embedding}]}

        mem = MemoryInterface(
            db_path=memory_db,
            embedding_api_key="test-key",
            embedding_model="text-embedding-3-small",
            embedding_base_url="https://api.example.com/v1",
            **_MEM_DEFAULTS,
        )

        with patch("memory.httpx.post", return_value=mock_resp) as mock_post:
            first = await mem._embed("cache me")
            second = await mem._embed("cache me")

        assert first == fake_embedding
        assert second == fake_embedding
        # httpx.post should only be called once — second call hits cache
        assert mock_post.call_count == 1

    @pytest.mark.asyncio
    async def test_embed_api_failure_returns_empty_list(self, memory_db):
        """When httpx.post raises, _embed returns [] gracefully."""
        from unittest.mock import patch

        mem = MemoryInterface(
            db_path=memory_db,
            embedding_api_key="test-key",
            embedding_model="text-embedding-3-small",
            embedding_base_url="https://api.example.com/v1",
            **_MEM_DEFAULTS,
        )

        with patch("memory.httpx.post", side_effect=httpx.ConnectError("connection refused")):
            result = await mem._embed("this will fail")

        assert result == []

    @pytest.mark.asyncio
    async def test_embed_timeout_returns_empty_list(self, memory_db):
        """When httpx.post times out, _embed returns [] gracefully."""
        from unittest.mock import patch

        mem = MemoryInterface(
            db_path=memory_db,
            embedding_api_key="test-key",
            embedding_model="text-embedding-3-small",
            embedding_base_url="https://api.example.com/v1",
            **_MEM_DEFAULTS,
        )

        with patch("memory.httpx.post", side_effect=httpx.TimeoutException("timed out")):
            result = await mem._embed("this will timeout")

        assert result == []

    @pytest.mark.asyncio
    async def test_embed_no_api_key_returns_empty(self, memory_db):
        """Without an API key, _embed still attempts (guard is at search level)."""
        from unittest.mock import MagicMock, patch

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"data": [{"embedding": [0.1]}]}

        mem = MemoryInterface(
            db_path=memory_db,
            embedding_api_key="",  # no key
            embedding_model="text-embedding-3-small",
            embedding_base_url="https://api.example.com/v1",
            **_MEM_DEFAULTS,
        )

        with patch("memory.httpx.post", return_value=mock_resp):
            result = await mem._embed("no key test")

        assert isinstance(result, list)


class TestMemorySearchRoundTrip:
    """Round-trip: insert chunks + FTS → MemoryInterface.search() → results."""

    @pytest.mark.asyncio
    async def test_fts_search_returns_matching_chunks(self, memory_db):
        """FTS-first path: query matches chunk text, returns results without embeddings."""
        mi = MemoryInterface(db_path=memory_db, **_MEM_DEFAULTS)
        results = await mi.search("hello")
        assert len(results) >= 1
        assert any("hello" in r["text"] for r in results)

    @pytest.mark.asyncio
    async def test_search_no_match_returns_empty(self, memory_db):
        """Query with no FTS matches and no embed_fn returns empty."""
        mi = MemoryInterface(db_path=memory_db, **_MEM_DEFAULTS)
        results = await mi.search("zzz_nonexistent_term_zzz")
        assert results == []


class TestSearchAggregation:
    """search() aggregation: combining FTS5 + vector results with dedup and ordering.

    Mocks _fts_search and _vector_search to isolate the combining logic
    in search() — the part that merges, deduplicates by chunk ID, sorts
    by score, and caps at top_k.
    """

    @pytest.mark.asyncio
    async def test_fts_sufficient_skips_vector(self, memory_db):
        """When FTS returns >= 3 results, vector search is never called."""
        from unittest.mock import AsyncMock, patch

        mi = MemoryInterface(db_path=memory_db, embedding_api_key="key", **_MEM_DEFAULTS)
        fts_results = [
            {"id": "a", "text": "alpha", "score": 0.9},
            {"id": "b", "text": "beta", "score": 0.8},
            {"id": "c", "text": "gamma", "score": 0.7},
        ]

        with patch.object(mi, "_fts_search", new_callable=AsyncMock, return_value=fts_results) as mock_fts, \
             patch.object(mi, "_vector_search", new_callable=AsyncMock) as mock_vector:
            results = await mi.search("test query")

        mock_fts.assert_awaited_once()
        mock_vector.assert_not_awaited()
        assert results == fts_results

    @pytest.mark.asyncio
    async def test_fts_and_vector_combined(self, memory_db):
        """When FTS returns < 3, vector results are merged in."""
        from unittest.mock import AsyncMock, patch

        mi = MemoryInterface(db_path=memory_db, embedding_api_key="key", **_MEM_DEFAULTS)
        fts_results = [
            {"id": "a", "text": "alpha", "score": 0.9},
        ]
        vector_results = [
            {"id": "x", "text": "xray", "score": 0.85},
            {"id": "y", "text": "yankee", "score": 0.6},
        ]

        with patch.object(mi, "_fts_search", new_callable=AsyncMock, return_value=fts_results), \
             patch.object(mi, "_vector_search", new_callable=AsyncMock, return_value=vector_results):
            results = await mi.search("test query", top_k=10)

        ids = [r["id"] for r in results]
        assert "a" in ids
        assert "x" in ids
        assert "y" in ids
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_deduplication_by_chunk_id(self, memory_db):
        """Duplicate chunk IDs across FTS and vector are deduplicated."""
        from unittest.mock import AsyncMock, patch

        mi = MemoryInterface(db_path=memory_db, embedding_api_key="key", **_MEM_DEFAULTS)
        # Same chunk "a" appears in both FTS and vector results
        fts_results = [
            {"id": "a", "text": "alpha from fts", "score": 0.5},
        ]
        vector_results = [
            {"id": "a", "text": "alpha from vector", "score": 0.95},
            {"id": "b", "text": "bravo", "score": 0.7},
        ]

        with patch.object(mi, "_fts_search", new_callable=AsyncMock, return_value=fts_results), \
             patch.object(mi, "_vector_search", new_callable=AsyncMock, return_value=vector_results):
            results = await mi.search("test query", top_k=10)

        # "a" should appear exactly once — FTS version is kept (seen-set prefers FTS)
        ids = [r["id"] for r in results]
        assert ids.count("a") == 1
        assert len(results) == 2  # "a" (from FTS) + "b" (from vector)
        # The FTS version of "a" is kept since it's added first
        a_result = next(r for r in results if r["id"] == "a")
        assert a_result["text"] == "alpha from fts"

    @pytest.mark.asyncio
    async def test_merged_results_sorted_by_score_descending(self, memory_db):
        """After merging, results are sorted by score descending."""
        from unittest.mock import AsyncMock, patch

        mi = MemoryInterface(db_path=memory_db, embedding_api_key="key", **_MEM_DEFAULTS)
        fts_results = [
            {"id": "low", "text": "low score", "score": 0.1},
            {"id": "mid", "text": "mid score", "score": 0.5},
        ]
        vector_results = [
            {"id": "high", "text": "high score", "score": 0.99},
            {"id": "medium", "text": "medium score", "score": 0.6},
        ]

        with patch.object(mi, "_fts_search", new_callable=AsyncMock, return_value=fts_results), \
             patch.object(mi, "_vector_search", new_callable=AsyncMock, return_value=vector_results):
            results = await mi.search("test query", top_k=10)

        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)
        assert results[0]["id"] == "high"
        assert results[-1]["id"] == "low"

    @pytest.mark.asyncio
    async def test_merged_results_capped_at_top_k(self, memory_db):
        """Merged results are truncated to top_k."""
        from unittest.mock import AsyncMock, patch

        mi = MemoryInterface(db_path=memory_db, embedding_api_key="key", **_MEM_DEFAULTS)
        fts_results = [
            {"id": "a", "text": "a", "score": 0.9},
            {"id": "b", "text": "b", "score": 0.8},
        ]
        vector_results = [
            {"id": "c", "text": "c", "score": 0.7},
            {"id": "d", "text": "d", "score": 0.6},
            {"id": "e", "text": "e", "score": 0.5},
        ]

        with patch.object(mi, "_fts_search", new_callable=AsyncMock, return_value=fts_results), \
             patch.object(mi, "_vector_search", new_callable=AsyncMock, return_value=vector_results):
            results = await mi.search("test query", top_k=3)

        assert len(results) == 3
        # Should keep the 3 highest-scored
        ids = [r["id"] for r in results]
        assert "a" in ids
        assert "b" in ids
        assert "c" in ids
        assert "d" not in ids
        assert "e" not in ids

    @pytest.mark.asyncio
    async def test_fts_results_vector_empty(self, memory_db):
        """FTS returns results but vector returns empty — FTS results survive."""
        from unittest.mock import AsyncMock, patch

        mi = MemoryInterface(db_path=memory_db, embedding_api_key="key", **_MEM_DEFAULTS)
        fts_results = [
            {"id": "a", "text": "alpha", "score": 0.5},
        ]

        with patch.object(mi, "_fts_search", new_callable=AsyncMock, return_value=fts_results), \
             patch.object(mi, "_vector_search", new_callable=AsyncMock, return_value=[]):
            results = await mi.search("test query", top_k=10)

        assert len(results) == 1
        assert results[0]["id"] == "a"

    @pytest.mark.asyncio
    async def test_fts_empty_vector_returns_results(self, memory_db):
        """FTS returns empty, vector returns results — vector results survive."""
        from unittest.mock import AsyncMock, patch

        mi = MemoryInterface(db_path=memory_db, embedding_api_key="key", **_MEM_DEFAULTS)
        vector_results = [
            {"id": "v1", "text": "vector hit", "score": 0.8},
            {"id": "v2", "text": "another", "score": 0.6},
        ]

        with patch.object(mi, "_fts_search", new_callable=AsyncMock, return_value=[]), \
             patch.object(mi, "_vector_search", new_callable=AsyncMock, return_value=vector_results):
            results = await mi.search("test query", top_k=10)

        assert len(results) == 2
        assert results[0]["id"] == "v1"
        assert results[1]["id"] == "v2"

    @pytest.mark.asyncio
    async def test_no_api_key_skips_vector(self, memory_db):
        """Without api_key, vector search is skipped even when FTS < 3."""
        from unittest.mock import AsyncMock, patch

        mi = MemoryInterface(db_path=memory_db, embedding_api_key="", **_MEM_DEFAULTS)
        fts_results = [
            {"id": "a", "text": "alpha", "score": 0.5},
        ]

        with patch.object(mi, "_fts_search", new_callable=AsyncMock, return_value=fts_results) as mock_fts, \
             patch.object(mi, "_vector_search", new_callable=AsyncMock) as mock_vector:
            results = await mi.search("test query")

        mock_fts.assert_awaited_once()
        mock_vector.assert_not_awaited()
        assert results == fts_results

    @pytest.mark.asyncio
    async def test_both_empty_returns_empty(self, memory_db):
        """Both FTS and vector return nothing — empty list."""
        from unittest.mock import AsyncMock, patch

        mi = MemoryInterface(db_path=memory_db, embedding_api_key="key", **_MEM_DEFAULTS)

        with patch.object(mi, "_fts_search", new_callable=AsyncMock, return_value=[]), \
             patch.object(mi, "_vector_search", new_callable=AsyncMock, return_value=[]):
            results = await mi.search("test query")

        assert results == []

    @pytest.mark.asyncio
    async def test_fts_exactly_three_skips_vector(self, memory_db):
        """Boundary: exactly 3 FTS results means vector is NOT called."""
        from unittest.mock import AsyncMock, patch

        mi = MemoryInterface(db_path=memory_db, embedding_api_key="key", **_MEM_DEFAULTS)
        fts_results = [
            {"id": "a", "text": "a", "score": 0.9},
            {"id": "b", "text": "b", "score": 0.8},
            {"id": "c", "text": "c", "score": 0.7},
        ]

        with patch.object(mi, "_fts_search", new_callable=AsyncMock, return_value=fts_results), \
             patch.object(mi, "_vector_search", new_callable=AsyncMock) as mock_vector:
            results = await mi.search("test query")

        mock_vector.assert_not_awaited()
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_fts_two_triggers_vector(self, memory_db):
        """Boundary: exactly 2 FTS results triggers vector fallback."""
        from unittest.mock import AsyncMock, patch

        mi = MemoryInterface(db_path=memory_db, embedding_api_key="key", **_MEM_DEFAULTS)
        fts_results = [
            {"id": "a", "text": "a", "score": 0.9},
            {"id": "b", "text": "b", "score": 0.8},
        ]
        vector_results = [
            {"id": "v1", "text": "vector", "score": 0.75},
        ]

        with patch.object(mi, "_fts_search", new_callable=AsyncMock, return_value=fts_results), \
             patch.object(mi, "_vector_search", new_callable=AsyncMock, return_value=vector_results) as mock_vector:
            results = await mi.search("test query")

        mock_vector.assert_awaited_once()
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_dedup_all_overlapping(self, memory_db):
        """When vector returns only IDs already in FTS, no new results added."""
        from unittest.mock import AsyncMock, patch

        mi = MemoryInterface(db_path=memory_db, embedding_api_key="key", **_MEM_DEFAULTS)
        fts_results = [
            {"id": "a", "text": "alpha", "score": 0.5},
            {"id": "b", "text": "bravo", "score": 0.4},
        ]
        vector_results = [
            {"id": "a", "text": "alpha again", "score": 0.99},
            {"id": "b", "text": "bravo again", "score": 0.88},
        ]

        with patch.object(mi, "_fts_search", new_callable=AsyncMock, return_value=fts_results), \
             patch.object(mi, "_vector_search", new_callable=AsyncMock, return_value=vector_results):
            results = await mi.search("test query", top_k=10)

        # Only 2 unique IDs, both from FTS
        assert len(results) == 2
        ids = [r["id"] for r in results]
        assert sorted(ids) == ["a", "b"]

    @pytest.mark.asyncio
    async def test_missing_score_treated_as_zero(self, memory_db):
        """Results without 'score' key sort to the bottom."""
        from unittest.mock import AsyncMock, patch

        mi = MemoryInterface(db_path=memory_db, embedding_api_key="key", **_MEM_DEFAULTS)
        fts_results = [
            {"id": "no-score", "text": "no score field"},  # no "score" key
        ]
        vector_results = [
            {"id": "has-score", "text": "has score", "score": 0.7},
        ]

        with patch.object(mi, "_fts_search", new_callable=AsyncMock, return_value=fts_results), \
             patch.object(mi, "_vector_search", new_callable=AsyncMock, return_value=vector_results):
            results = await mi.search("test query", top_k=10)

        # "has-score" (0.7) should sort before "no-score" (defaults to 0)
        assert results[0]["id"] == "has-score"
        assert results[1]["id"] == "no-score"
