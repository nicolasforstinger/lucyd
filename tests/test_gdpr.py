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


# ── Commitments (regression: the table has who/what/deadline, not description) ──


@pytest.mark.asyncio
async def test_search_finds_commitment_by_what(pool: Any) -> None:
    """gdpr_search matches commitments on who/what/deadline (no 'description' column exists)."""
    gdpr.configure(pool=pool)
    await pool.execute(
        "INSERT INTO knowledge.commitments (who, what, status) VALUES ($1, $2, 'open')",
        "agent", "Email Alice Bravo the report",
    )

    result = await gdpr.handle_gdpr_search(["Alice Bravo"])

    assert "COMMITMENT" in result
    assert "Alice Bravo" in result


@pytest.mark.asyncio
async def test_redact_commitment_replaces_text(pool: Any) -> None:
    """gdpr_redact redact replaces matched text in the commitment's who/what/deadline."""
    gdpr.configure(pool=pool)
    cid: int = await pool.fetchval(
        "INSERT INTO knowledge.commitments (who, what, status) "
        "VALUES ('agent', 'Email Alice Bravo the report', 'open') RETURNING id",
    )

    await gdpr.handle_gdpr_redact(
        target="commitment", id=cid, action="redact", old="Alice Bravo", new="[REDACTED]",
    )

    what: str = await pool.fetchval(
        "SELECT what FROM knowledge.commitments WHERE id = $1", cid,
    )
    assert "Alice Bravo" not in what
    assert "[REDACTED]" in what


# ── Facts + messages (existing behavior characterization) ────────


@pytest.mark.asyncio
async def test_search_then_delete_fact_soft_deletes(pool: Any) -> None:
    """A fact is found by search and 'delete' soft-deletes it (invalidated_at set)."""
    gdpr.configure(pool=pool)
    fid: int = await pool.fetchval(
        "INSERT INTO knowledge.facts (entity, attribute, value) "
        "VALUES ('Alice Bravo', 'email', 'alice@example.com') RETURNING id",
    )

    found = await gdpr.handle_gdpr_search(["alice@example.com"])
    assert "FACT" in found

    await gdpr.handle_gdpr_redact(target="fact", id=fid, action="delete")

    invalidated = await pool.fetchval(
        "SELECT invalidated_at FROM knowledge.facts WHERE id = $1", fid,
    )
    assert invalidated is not None


@pytest.mark.asyncio
async def test_redact_message_delete_is_refused(pool: Any) -> None:
    """Deleting a message is refused (breaks session structure) — redaction is required instead."""
    gdpr.configure(pool=pool)
    msg = await gdpr.handle_gdpr_redact(target="message", id=1, action="delete")
    assert "cannot be deleted" in msg.lower()
