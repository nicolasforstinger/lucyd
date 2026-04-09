"""Periodic operations for the Lucyd daemon.

Handles evolution, indexing, consolidation, maintenance, compaction,
and session close consolidation. Each function takes explicit dependencies
and is called by thin daemon wrappers.

These operations are triggered by cron (via lucydctl), HTTP endpoints,
or session lifecycle events. They are independent of the message pipeline.
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
import time
import uuid
from collections.abc import Callable, Awaitable
from pathlib import Path
from typing import Any

from config import Config
from context import ContextBuilder
from metering import MeteringDB

log = logging.getLogger("lucyd")


# ─── Evolution Helpers ───────────────────────────────────────────

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


async def get_evolution_state(
    file_path: str,
    pool: Any,  # asyncpg.Pool — no stubs available
    client_id: str,
    agent_id: str,
) -> dict[str, Any] | None:
    """Return evolution state for *file_path*, or None if never evolved."""
    row = await pool.fetchrow(
        "SELECT last_evolved_at, content_hash, logs_through "
        "FROM knowledge.evolution_state "
        "WHERE client_id = $1 AND agent_id = $2 AND file_path = $3",
        client_id, agent_id, file_path,
    )
    if row is None:
        return None
    return {
        "last_evolved_at": row["last_evolved_at"],
        "content_hash": row["content_hash"],
        "logs_through": row["logs_through"],
    }


async def update_evolution_state(
    config: Config,
    pool: Any,  # asyncpg.Pool — no stubs available
    client_id: str,
    agent_id: str,
) -> dict[str, str]:
    """Record evolution completion for MEMORY.md and USER.md.

    Computes content hashes and finds the latest daily log date,
    then upserts into ``knowledge.evolution_state``.
    """
    workspace = config.workspace

    # Find latest daily log date
    memory_dir = workspace / "memory"
    latest = ""
    if memory_dir.is_dir():
        for entry in memory_dir.iterdir():
            if entry.is_file() and entry.suffix == ".md":
                m = _DATE_RE.search(entry.stem)
                if m and m.group(1) > latest:
                    latest = m.group(1)

    updated: dict[str, str] = {}
    for fname in ("MEMORY.md", "USER.md"):
        fpath = workspace / fname
        content = fpath.read_text() if fpath.exists() else ""
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        await pool.execute(
            "INSERT INTO knowledge.evolution_state "
            "(client_id, agent_id, file_path, content_hash, logs_through) "
            "VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (client_id, agent_id, file_path) DO UPDATE SET "
            "last_evolved_at = now(), content_hash = $4, logs_through = $5",
            client_id, agent_id, fname, content_hash, latest,
        )
        updated[fname] = content_hash

    log.info("Evolution state updated: logs through %s", latest or "none")
    return updated


async def check_new_logs_exist(
    workspace: Path,
    pool: Any,  # asyncpg.Pool — no stubs available
    client_id: str,
    agent_id: str,
    reference_file: str = "MEMORY.md",
) -> tuple[bool, str]:
    """Check whether new daily logs exist since the last evolution.

    Uses the *reference_file* (default ``MEMORY.md``) to determine the
    ``logs_through`` date from the ``evolution_state`` table.

    Returns ``(has_new_logs, since_date)`` where *since_date* is the
    ``logs_through`` value (empty string if never evolved).
    """
    state = await get_evolution_state(reference_file, pool, client_id, agent_id)
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


# ─── Git Operations ──────────────────────────────────────────────


def git_snapshot(workspace: Path, label: str) -> str | None:
    """Create a git checkpoint in the workspace. Returns tag name or None."""
    ws = str(workspace)
    tag = f"pre-{label}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    try:
        subprocess.run(
            ["git", "-C", ws, "add", "-A"],
            capture_output=True, timeout=30, check=False,
        )
        subprocess.run(
            ["git", "-C", ws, "commit", "--allow-empty",
             "-m", f"checkpoint: {label}"],
            capture_output=True, timeout=30, check=False,
        )
        subprocess.run(
            ["git", "-C", ws, "tag", tag],
            capture_output=True, timeout=30, check=True,
        )
        log.info("Git snapshot: %s", tag)
        return tag
    except Exception as e:
        log.warning("Git snapshot failed: %s", e)
        return None


def git_rollback(workspace: Path, tag: str) -> bool:
    """Rollback workspace to a git tag."""
    try:
        subprocess.run(
            ["git", "-C", str(workspace), "reset", "--hard", tag],
            capture_output=True, timeout=30, check=True,
        )
        log.warning("Rolled back workspace to %s", tag)
        return True
    except Exception as e:
        log.error("Git rollback to %s failed: %s", tag, e)
        return False


def validate_evolution(config: Config) -> bool:
    """Validate workspace files after evolution. Returns True if valid."""
    workspace = config.workspace
    for name in config.context_stable + config.context_semi_stable:
        path = workspace / name
        if not path.exists():
            log.error("Evolution validation failed: %s missing", name)
            return False
        if path.stat().st_size == 0:
            log.error("Evolution validation failed: %s is empty", name)
            return False
    return True


# ─── Session Close Consolidation ─────────────────────────────────


async def consolidate_on_close(
    session: Any,
    config: Config,
    pool: Any,  # asyncpg.Pool — no stubs available
    client_id: str,
    agent_id: str,
    get_provider: Callable[[str], Any],
    context_builder: ContextBuilder,
    metering_db: MeteringDB | None,
) -> None:
    """Consolidation callback fired before session archival."""
    try:
        import consolidation
        start_idx, end_idx = await consolidation.get_unprocessed_range(
            session.id, session.messages, session.compaction_count,
            pool, client_id, agent_id,
        )
        if end_idx > start_idx:
            await consolidation.consolidate_session(
                session_id=session.id,
                messages=session.messages,
                compaction_count=session.compaction_count,
                config=config,
                provider=get_provider("consolidation"),
                context_builder=context_builder,
                pool=pool,
                client_id=client_id,
                agent_id=agent_id,
                metering=metering_db,
            )
    except Exception:
        log.warning("consolidation on close failed", exc_info=True)


# ─── Evolution ───────────────────────────────────────────────────


async def handle_evolve(
    *,
    force: bool,
    config: Config,
    pool: Any,  # asyncpg.Pool — no stubs available
    client_id: str,
    agent_id: str,
    queue: Any,
    set_rollback_tag: Callable[[str], None],
) -> dict[str, Any]:
    """Handle evolution request — snapshot, push to queue, validate after."""
    if not force:
        try:
            has_new, since_date = await check_new_logs_exist(
                config.workspace, pool, client_id, agent_id,
            )
            if not has_new:
                return {"status": "skipped", "reason": f"no new daily logs since {since_date or 'ever'}"}
        except Exception:
            log.warning("Evolution pre-check failed, proceeding anyway", exc_info=True)

    tag = git_snapshot(config.workspace, "evolve")
    if tag:
        set_rollback_tag(tag)

    msg = {
        "type": "system",
        "sender": "evolution",
        "task_type": "system",
        "text": (
            "[AUTOMATED SYSTEM MESSAGE] "
            "Load the evolution skill and evolve your memory files. "
            "New daily logs are available."
        ),
    }
    await queue.put(msg)
    return {"status": "queued", "session": "evolution"}


# ─── Indexing ────────────────────────────────────────────────────


async def handle_index(
    config: Config,
    pool: Any,  # asyncpg.Pool — no stubs available
    client_id: str,
    agent_id: str,
    full: bool = False,
    metering: Any = None,
    converter: Any = None,
) -> dict[str, Any]:
    """Run workspace indexing."""
    from tools.indexer import configure as indexer_configure
    from tools.indexer import index_workspace

    indexer_configure(
        chunk_size=config.indexer_chunk_size,
        chunk_overlap=config.indexer_chunk_overlap,
        embed_batch_limit=config.indexer_embed_batch_limit,
        embedding_model=config.embedding_model,
        embedding_base_url=config.embedding_base_url,
        embedding_provider=config.embedding_provider,
        metering=metering,
        converter=converter,
        cost_rates=config.embedding_cost_rates,
        currency=config.embedding_currency,
    )

    summary: dict[str, Any] = await index_workspace(
        workspace=config.workspace,
        pool=pool,
        client_id=client_id,
        agent_id=agent_id,
        api_key=config.embedding_api_key,
        force=full,
        embedding_timeout=config.embedding_timeout,
    )
    return summary


async def handle_index_status(
    config: Config,
    pool: Any,  # asyncpg.Pool — no stubs available
    client_id: str,
    agent_id: str,
) -> dict[str, Any]:
    """Return workspace index status."""
    from tools.indexer import get_index_status
    return await get_index_status(pool, client_id, agent_id, config.workspace)


# ─── Consolidation ──────────────────────────────────────────────


async def handle_consolidate(
    config: Config,
    pool: Any,  # asyncpg.Pool — no stubs available
    client_id: str,
    agent_id: str,
    get_provider: Callable[[str], Any],
    metering_db: MeteringDB | None,
    converter: Any = None,
) -> dict[str, Any]:
    """Run memory consolidation — extract facts from workspace files."""
    from consolidation import extract_from_file
    from tools.indexer import scan_workspace

    provider = get_provider("consolidation")
    fact_model_cfg = config.model_config("primary")
    model_name = fact_model_cfg.get("model", "primary")
    provider_name = fact_model_cfg.get("provider", "")
    cost_rates = fact_model_cfg.get("cost_per_mtok", [])
    currency = fact_model_cfg.get("currency", "EUR")

    file_list = scan_workspace(
        config.workspace,
        include_patterns=config.indexer_include_patterns,
        exclude_dirs=set(config.indexer_exclude_dirs),
    )

    total_facts = 0
    files_with_facts = 0
    for rel_path, abs_path in file_list:
        try:
            count = await extract_from_file(
                str(abs_path), provider, pool, client_id, agent_id,
                config.consolidation_confidence_threshold,
                model_name=model_name,
                provider_name=provider_name,
                cost_rates=cost_rates,
                metering=metering_db,
                converter=converter,
                currency=currency,
            )
            if count:
                files_with_facts += 1
                log.info("Extracted %d facts from %s", count, rel_path)
            total_facts += count
        except Exception:
            log.exception("Failed to process %s", rel_path)

    return {"status": "completed", "facts": total_facts,
            "files_scanned": len(file_list), "files_with_facts": files_with_facts}


# ─── Maintenance ─────────────────────────────────────────────────


async def handle_maintain(
    config: Config,
    pool: Any,  # asyncpg.Pool — no stubs available
    client_id: str,
    agent_id: str,
    metering_db: MeteringDB | None,
) -> dict[str, Any]:
    """Run memory maintenance + metering retention."""
    from memory import run_maintenance

    stats: dict[str, Any] = await run_maintenance(
        pool, client_id, agent_id, config.maintenance_stale_threshold_days,
    )

    if metering_db:
        stats["metering_deleted"] = await metering_db.enforce_retention(
            config.metering_retention_months,
        )
    else:
        stats["metering_deleted"] = 0

    log.info("Maintenance stats: %s", stats)
    return stats


# ─── Compaction ──────────────────────────────────────────────────


async def handle_compact(
    config: Config,
    session_mgr: Any,
    process_message: Callable[..., Awaitable[None]],
    get_session_lock: Callable[[str], Any],
) -> dict[str, Any]:
    """Force-compact the primary session after agent writes diary."""
    primary = None
    for contact in await session_mgr.list_contacts():
        if contact.startswith("http:"):
            continue
        session = await session_mgr.get_or_create(contact)
        if primary is None or len(session.messages) > len(primary[1].messages):
            primary = (contact, session)

    if not primary:
        return {"status": "skipped", "reason": "no active session"}

    contact, session = primary
    today = time.strftime("%Y-%m-%d")
    diary_text = config.diary_prompt.replace("{date}", today)

    tid = str(uuid.uuid4())
    log.info("[%s] Forced compact: diary + compaction for session %s (%s)",
             tid[:8], session.id, contact)

    async with get_session_lock(contact):
        await process_message(
            text=diary_text,
            sender=contact,
            source="system",
            deliver=False,
            trace_id=tid,
            force_compact=True,
            task_type="system",
            session_key=contact,
        )
    return {"status": "completed", "session": session.id}
