"""db.py — PostgreSQL connection pool and schema management.

Provides pool lifecycle (create, close) and forward-only schema versioning.
Schema files live in ``schema/`` as numbered SQL files (001_initial.sql, etc.).
Applied on startup via a ``public.schema_version`` table.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import asyncpg  # Core dependency — always installed. No type stubs.

log = logging.getLogger(__name__)

_SCHEMA_DIR = Path(__file__).resolve().parent / "schema"


# ---------------------------------------------------------------------------
# Pool lifecycle
# ---------------------------------------------------------------------------

async def create_pool(
    dsn: str,
    *,
    min_size: int = 2,
    max_size: int = 10,
) -> Any:
    """Create and return an ``asyncpg.Pool``.

    Callers must call :func:`close_pool` on shutdown.
    """
    pool: Any = await asyncpg.create_pool(
        dsn,
        min_size=min_size,
        max_size=max_size,
    )
    return pool


async def close_pool(pool: Any) -> None:
    """Gracefully close an asyncpg pool."""
    await pool.close()


# ---------------------------------------------------------------------------
# Schema versioning
# ---------------------------------------------------------------------------

async def ensure_schema(pool: Any) -> None:
    """Apply unapplied schema files from ``schema/``.

    Each ``.sql`` file is named ``NNN_description.sql`` where *NNN* is a
    monotonically increasing integer version.  Files are applied inside a
    transaction; the version number is recorded in ``public.schema_version``
    so the same file is never applied twice.
    """
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS public.schema_version (
                version     INT PRIMARY KEY,
                applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        current: int = await conn.fetchval(
            "SELECT COALESCE(MAX(version), 0) FROM public.schema_version",
        ) or 0

    migrations = _collect_migrations()
    pending = [(v, p) for v, p in migrations if v > current]
    if not pending:
        log.info("schema up-to-date at version %d", current)
        return

    for version, path in pending:
        sql = path.read_text(encoding="utf-8")
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO public.schema_version (version) VALUES ($1)",
                    version,
                )
        log.info("applied schema migration %03d (%s)", version, path.name)


def _collect_migrations() -> list[tuple[int, Path]]:
    """Return sorted ``(version, path)`` pairs from the schema directory."""
    results: list[tuple[int, Path]] = []
    if not _SCHEMA_DIR.is_dir():
        return results
    for p in sorted(_SCHEMA_DIR.glob("*.sql")):
        prefix = p.stem.split("_", 1)[0]
        try:
            version = int(prefix)
        except ValueError:
            log.warning("skipping schema file with non-numeric prefix: %s", p.name)
            continue
        results.append((version, p))
    return results
