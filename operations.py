"""Periodic operations for the Lucyd daemon.

Handles indexing, consolidation, maintenance, compaction, and session
close consolidation. Each function takes explicit dependencies and is
called by thin daemon wrappers.

These operations are triggered by container cron jobs (installed by
bin/entrypoint.sh) that POST to the daemon's HTTP API with the bearer
token, by direct HTTP API calls, or by session lifecycle events. They
are independent of the message pipeline.
"""

from __future__ import annotations

import datetime as _dt
import logging
import random
import time
import uuid
from collections.abc import Callable, Awaitable
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg

import maintain_state
from config import Config
from context import ContextBuilder
from metering import MeteringDB

log = logging.getLogger("lucyd")

# The maintenance pass runs in its own dedicated session, never the user's.
_MAINTAIN_SESSION_KEY = "system:maintenance"
# The agent's ask-ledger, relative to the workspace (inside allowed_paths so
# she reads/appends it with her normal file tools).
_LEDGER_RELPATH = "notes/maintenance-log.md"


# ─── Session Close Consolidation ─────────────────────────────────


async def consolidate_on_close(
    session: Any,
    config: Config,
    pool: asyncpg.Pool,
    get_provider: Callable[[str], Any],
    context_builder: ContextBuilder,
    metering_db: MeteringDB | None,
) -> None:
    """Consolidation callback fired before session archival.

    Only runs for ``user:*`` sessions — operator/system/agent sessions
    don't feed memory, so extracting facts from them is noise.
    """
    contact = getattr(session, "contact", "")
    if not contact.startswith("user:"):
        return
    try:
        import consolidation
        start_idx, end_idx = await consolidation.get_unprocessed_range(
            session.id, session.messages, session.compaction_count,
            pool,
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
                metering=metering_db,
            )
    except (TimeoutError, RuntimeError, OSError) as e:
        log.warning("Consolidation on session close failed: %s", e, exc_info=True)


# ─── Indexing ────────────────────────────────────────────────────


async def handle_index(
    config: Config,
    pool: asyncpg.Pool,
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
        api_key=config.embedding_api_key,
        force=full,
        embedding_timeout=config.embedding_timeout,
    )
    return summary


async def handle_index_status(
    config: Config,
    pool: asyncpg.Pool,
) -> dict[str, Any]:
    """Return workspace index status."""
    from tools.indexer import get_index_status
    return await get_index_status(pool, config.workspace)


# ─── Consolidation ──────────────────────────────────────────────


async def handle_consolidate(
    config: Config,
    pool: asyncpg.Pool,
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
                str(abs_path), provider, pool,
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
        except (TimeoutError, RuntimeError, OSError) as e:
            log.error("Consolidation failed for %s: %s", rel_path, e, exc_info=True)

    return {"status": "completed", "facts": total_facts,
            "files_scanned": len(file_list), "files_with_facts": files_with_facts}


# ─── Maintenance ─────────────────────────────────────────────────


async def _run_mechanical_maintenance(
    config: Config,
    pool: asyncpg.Pool,
    metering_db: MeteringDB | None,
) -> dict[str, Any]:
    """Stale-fact detection + metering retention. Cheap; runs every call."""
    from memory import run_maintenance

    stats: dict[str, Any] = await run_maintenance(
        pool, config.maintenance_stale_threshold_days,
    )
    if metering_db:
        stats["metering_deleted"] = await metering_db.enforce_retention(
            config.metering_retention_months,
        )
    else:
        stats["metering_deleted"] = 0
    log.info("Maintenance stats: %s", stats)
    return stats


def _now_local_line(user_tz: str) -> str:
    """Human-readable current local date/time for the brief header."""
    try:
        tz: _dt.tzinfo = ZoneInfo(user_tz)
    except (ZoneInfoNotFoundError, ValueError):
        tz = _dt.timezone.utc
    return f"{_dt.datetime.now(tz):%A %Y-%m-%d %H:%M %Z}"


def _format_idle(idle_minutes: float | None, user_name: str) -> str:
    """Render "Nicolas last messaged N ago" for the brief header."""
    if idle_minutes is None:
        return f"{user_name} has no messages on record yet."
    if idle_minutes < 60:
        return f"{user_name} last messaged {int(idle_minutes)} minutes ago."
    return f"{user_name} last messaged {idle_minutes / 60:.1f} hours ago."


def _build_maintain_brief(
    *,
    protocol: str,
    now_local: str,
    last_pass_at: _dt.datetime | None,
    changed_files: list[str],
    new_facts: list[str],
    idle_line: str,
    ledger_path: Path,
) -> str:
    """Header (generated this pass) + the MAINTAIN.md protocol body."""
    last_pass = (
        f"{last_pass_at.astimezone(_dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ}"
        if last_pass_at is not None else "never (first pass)"
    )
    files_block = "\n".join(f"  - {f}" for f in changed_files) if changed_files else "  (none)"
    facts_block = "\n".join(f"  - {f}" for f in new_facts) if new_facts else "  (none)"
    header = (
        "=== This pass ===\n"
        f"Now: {now_local}\n"
        f"Last pass: {last_pass}\n"
        f"{idle_line}\n"
        f"Your ask-ledger: {ledger_path} — read it before you ask anything so "
        "you don't re-ask, and append what you ask/fix this pass.\n"
        "\nFiles of yours changed since last pass:\n"
        f"{files_block}\n"
        "\nStructured facts created since last pass:\n"
        f"{facts_block}\n"
        "\n=== Your maintenance protocol (MAINTAIN.md) ===\n"
    )
    return header + protocol


async def handle_maintain(
    config: Config,
    pool: asyncpg.Pool,
    metering_db: MeteringDB | None,
    process_message: Callable[..., Awaitable[None]],
    get_session_lock: Callable[[str], Any],
) -> dict[str, Any]:
    """Run mechanical maintenance, then dispatch the self-maintenance pass.

    Mechanical maintenance (stale facts + metering retention) runs on every
    call — it is cheap and was daily before. The LLM pass dispatches only when
    enabled and the elapsed time since the last pass exceeds a randomized
    interval in ``[interval_min_minutes, interval_max_minutes]`` (the hourly
    cron polls; most calls return ``too_soon``). The pass reads MAINTAIN.md and
    runs as a ``system:maintenance`` turn with tools, in its own session.
    """
    stats = await _run_mechanical_maintenance(config, pool, metering_db)
    result: dict[str, Any] = {"maintenance": stats}

    if not config.maintain_enabled:
        result["outcome"] = "disabled"
        return result

    path = maintain_state.state_path(config.data_dir)
    state = maintain_state.load_state(path)
    now = _dt.datetime.now(_dt.timezone.utc)

    # Interval gate — randomized so passes don't land on a fixed clock edge.
    interval_minutes = random.randint(
        config.maintain_interval_min_minutes,
        config.maintain_interval_max_minutes,
    )
    if state.last_pass_at is not None:
        elapsed_minutes = (now - state.last_pass_at).total_seconds() / 60.0
        if elapsed_minutes < interval_minutes:
            result["outcome"] = "too_soon"
            result["elapsed_minutes"] = round(elapsed_minutes, 1)
            result["interval_minutes"] = interval_minutes
            return result

    protocol = _read_maintain_protocol(config.workspace)
    if protocol is None:
        result["outcome"] = "skipped"
        result["reason"] = "MAINTAIN.md missing"
        return result

    changed_files = maintain_state.changed_workspace_files(
        config.workspace, state.last_pass_at,
    )
    new_facts = await maintain_state.facts_created_since(pool, state.last_pass_at)
    idle_minutes = await maintain_state.idle_minutes_since_user(
        pool, f"user:{config.user_name}",
    )

    brief = _build_maintain_brief(
        protocol=protocol,
        now_local=_now_local_line(config.user_timezone),
        last_pass_at=state.last_pass_at,
        changed_files=changed_files,
        new_facts=new_facts,
        idle_line=_format_idle(idle_minutes, config.user_name),
        ledger_path=config.workspace / _LEDGER_RELPATH,
    )

    tid = str(uuid.uuid4())
    log.info("[%s] Maintenance pass dispatching (changed=%d, new_facts=%d)",
             tid[:8], len(changed_files), len(new_facts))
    async with get_session_lock(_MAINTAIN_SESSION_KEY):
        await process_message(
            text=brief,
            sender="maintenance",
            talker="system",
            reply_to="silent",
            session_key=_MAINTAIN_SESSION_KEY,
            trace_id=tid,
        )

    # Advance the marker only after a real pass dispatched.
    maintain_state.save_last_pass(path, now)
    result["outcome"] = "ran"
    result["changed_files"] = changed_files
    result["new_facts"] = len(new_facts)
    return result


def _read_maintain_protocol(workspace: Path) -> str | None:
    """Read MAINTAIN.md from the workspace. ``None`` if absent → skip the pass."""
    protocol_path = workspace / "MAINTAIN.md"
    try:
        return protocol_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning("maintain: MAINTAIN.md missing at %s — skipping LLM pass", protocol_path)
        return None
    except OSError as e:
        log.warning("maintain: failed to read MAINTAIN.md (%s): %s", protocol_path, e)
        return None


# ─── Compaction ──────────────────────────────────────────────────


async def handle_compact(
    config: Config,
    session_mgr: Any,
    process_message: Callable[..., Awaitable[None]],
    get_session_lock: Callable[[str], Any],
) -> dict[str, Any]:
    """Force-compact the user session after agent writes diary.

    Targets the single ``user:<config.user.name>`` session only — non-user
    sessions don't accumulate enough history to need nightly compaction,
    and in-flight pressure is handled by per-message compaction.
    """
    user_key = f"user:{config.user_name}"
    if user_key not in await session_mgr.list_contacts():
        return {"status": "skipped", "reason": "no active user session"}

    session = await session_mgr.get_or_create(user_key)
    today = time.strftime("%Y-%m-%d")
    diary_text = config.diary_prompt.replace("{date}", today)

    tid = str(uuid.uuid4())
    log.info("[%s] Forced compact: diary + compaction for user session %s",
             tid[:8], session.id)

    async with get_session_lock(user_key):
        await process_message(
            text=diary_text,
            sender=config.user_name,
            talker="user",
            trace_id=tid,
            force_compact=True,
            reply_to="silent",
            session_key=user_key,
        )
    return {"status": "completed", "session": session.id}
