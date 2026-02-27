"""Memory evolution — rewrite workspace understanding files.

Reads current workspace files (MEMORY.md, USER.md), recent daily memory
logs, structured facts/episodes from SQLite, and IDENTITY.md as an identity
anchor.  Uses an LLM to produce complete rewrites that reflect current
understanding while preserving voice and foundational content.

Triggered by ``lucyd-evolve`` (cron/CLI) or ``POST /api/v1/evolve`` (HTTP API).
Both paths queue a self-driven evolution message through the daemon's agentic loop.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

# ── Date pattern for daily memory logs ───────────────────────────────

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


# ── State tracking ───────────────────────────────────────────────────

def get_evolution_state(
    file_path: str,
    conn: sqlite3.Connection,
) -> dict | None:
    """Return evolution state for *file_path*, or None if never evolved."""
    row = conn.execute(
        "SELECT last_evolved_at, content_hash, logs_through "
        "FROM evolution_state WHERE file_path = ?",
        (file_path,),
    ).fetchone()
    if row is None:
        return None
    return {
        "last_evolved_at": row[0] if isinstance(row, (tuple, list)) else row["last_evolved_at"],
        "content_hash": row[1] if isinstance(row, (tuple, list)) else row["content_hash"],
        "logs_through": row[2] if isinstance(row, (tuple, list)) else row["logs_through"],
    }


# ── Pre-check for trigger scripts ────────────────────────────────────

def check_new_logs_exist(
    workspace: Path,
    conn: sqlite3.Connection,
    reference_file: str = "MEMORY.md",
) -> tuple[bool, str]:
    """Check whether new daily logs exist since the last evolution.

    Uses the *reference_file* (default ``MEMORY.md``) to determine the
    ``logs_through`` date from the ``evolution_state`` table.

    Returns ``(has_new_logs, since_date)`` where *since_date* is the
    ``logs_through`` value (empty string if never evolved).
    """
    state = get_evolution_state(reference_file, conn)
    since_date = state["logs_through"] if state else ""

    memory_dir = workspace / "memory"
    if not memory_dir.is_dir():
        return False, since_date

    for entry in memory_dir.iterdir():
        if not entry.is_file() or entry.suffix != ".md":
            continue
        m = _DATE_RE.search(entry.stem)
        if m and (not since_date or m.group(1) > since_date):
            return True, since_date

    return False, since_date
