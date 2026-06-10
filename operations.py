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
import time
import uuid
from collections.abc import Callable, Awaitable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg

import consolidation
import maintain_state
from config import Config
from metering import MeteringDB

if TYPE_CHECKING:
    import asyncio

    from conversion import CurrencyConverter
    from session import Session, SessionManager

log = logging.getLogger("lucyd")

# The maintenance pass runs in its own dedicated session, never the user's.
_MAINTAIN_SESSION_KEY = "system:maintenance"
# The agent's ask-ledger, relative to the workspace (inside allowed_paths so
# she reads/appends it with her normal file tools).
_LEDGER_RELPATH = "notes/maintenance-log.md"


# ─── Memory Harvest (maintenance pass + pre-destructive) ─────────


def _build_harvest_brief(conversation: str) -> str:
    """Lean harvest-only brief — used before compaction or session close."""
    return (
        "=== Catch up your memory before this conversation is compressed ===\n"
        "This stretch of conversation is about to be compacted or closed. Record\n"
        "what lasts before it goes, then you're done — nothing else this pass:\n"
        "- Durable facts -> memory_write, each under the right entity. You know\n"
        "  who's who: you, the people and organizations you serve, and the\n"
        "  framework itself are different things that relate — never collapse\n"
        "  them into one.\n"
        "- One episode -> record_episode: a short summary in your voice, the\n"
        "  topics, and the emotional tone. That's what carries\n"
        "  the thread and the mood forward so you don't wake up cold.\n\n"
        "Conversation since your last consolidation:\n"
        f"{conversation}\n"
    )


async def harvest_conversation(
    session: Session,
    config: Config,
    pool: asyncpg.Pool,
    process_message: Callable[..., Awaitable[None]],
    get_session_lock: Callable[[str], asyncio.Lock],
) -> dict[str, Any]:
    """Harvest a session's unconsolidated messages via a focused agentic turn.

    Fired before a destructive event (compaction or session close) so the agent
    records facts + an episode from messages about to become inaccessible — the
    same job the scheduled maintenance pass does, scoped to just the harvest.
    User sessions only (others don't feed memory). Advances the shared
    consolidation watermark on success.

    Returns ``{"ok_to_compact": bool, "harvested": bool}``. ``ok_to_compact`` is
    False only when there were unconsolidated messages but the harvest turn
    failed — the caller must then skip compaction rather than discard them.
    """
    contact = getattr(session, "contact", "")
    if not contact.startswith("user:"):
        return {"ok_to_compact": True, "harvested": False}

    tid = str(uuid.uuid4())
    # Read the unconsolidated range, dispatch, and advance the watermark all under
    # the maintenance lock. /maintain runs concurrently with the message loop and
    # harvests the same user session; serializing the read-modify-write here means
    # whichever harvester acquires the lock second sees the watermark the first
    # advanced, so a span is never harvested twice (and the watermark never
    # regresses). The watermark advances to the harvested end_idx — not a re-read
    # len — so a concurrent append past end_idx is left for the next harvest.
    async with get_session_lock(_MAINTAIN_SESSION_KEY):
        start_idx, end_idx = await consolidation.get_unprocessed_range(
            session.id, session.messages, session.compaction_count, pool,
        )
        if end_idx <= start_idx:
            return {"ok_to_compact": True, "harvested": False}
        conversation = consolidation.serialize_messages(session.messages, start_idx, end_idx)
        if not conversation.strip():
            return {"ok_to_compact": True, "harvested": False}

        log.info("[%s] Pre-destructive harvest dispatching for session %s (%d chars)",
                 tid[:8], session.id, len(conversation))
        try:
            await process_message(
                text=_build_harvest_brief(conversation),
                sender="maintenance",
                talker="system",
                reply_to="silent",
                session_key=_MAINTAIN_SESSION_KEY,
                trace_id=tid,
            )
        except (TimeoutError, RuntimeError, OSError) as e:
            log.error("[%s] Pre-destructive harvest failed, blocking destructive op: %s",
                      tid[:8], e, exc_info=True)
            return {"ok_to_compact": False, "harvested": False}

        await consolidation.update_consolidation_state(
            session.id, session.compaction_count, end_idx, pool,
        )
    return {"ok_to_compact": True, "harvested": True}


# ─── Indexing ────────────────────────────────────────────────────


async def handle_index(
    config: Config,
    pool: asyncpg.Pool,
    full: bool = False,
    metering: MeteringDB | None = None,
    converter: CurrencyConverter | None = None,
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
        include_patterns=config.indexer_include_patterns,
        exclude_dirs=set(config.indexer_exclude_dirs),
    )
    return summary


async def handle_index_status(
    config: Config,
    pool: asyncpg.Pool,
) -> dict[str, Any]:
    """Return workspace index status."""
    from tools.indexer import get_index_status
    return await get_index_status(pool, config.workspace)



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
    """Render "<user> last messaged N ago" for the brief header."""
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
    conversation: str,
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
    convo_block = conversation if conversation.strip() else "  (no new conversation since last pass)"
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
        "\nConversation since your last pass — consolidate it (see your protocol):\n"
        f"{convo_block}\n"
        "\n=== Your maintenance protocol (MAINTAIN.md) ===\n"
    )
    return header + protocol


async def handle_maintain(
    config: Config,
    pool: asyncpg.Pool,
    metering_db: MeteringDB | None,
    session_mgr: SessionManager,
    process_message: Callable[..., Awaitable[None]],
    get_session_lock: Callable[[str], asyncio.Lock],
) -> dict[str, Any]:
    """Run mechanical maintenance, then dispatch the self-maintenance pass.

    Mechanical maintenance (stale facts + metering retention) runs on every
    call — it is cheap. The LLM pass then runs every call too; cadence is owned
    by the crontab (a fixed schedule), so there is no interval gate here. The
    pass harvests the conversation since the last pass into facts + an episode,
    tends accrued memory, and may reach out — reading MAINTAIN.md as a
    ``system:maintenance`` turn with tools, in its own session.
    """
    stats = await _run_mechanical_maintenance(config, pool, metering_db)
    result: dict[str, Any] = {"maintenance": stats}

    if not config.maintain_enabled:
        result["outcome"] = "disabled"
        return result

    path = maintain_state.state_path(config.data_dir)
    state = maintain_state.load_state(path)
    now = _dt.datetime.now(_dt.timezone.utc)

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

    # Idle gate: never run the LLM pass — and so never reach out — while the
    # user is mid-conversation. The pass only fires once the user has been quiet
    # for `maintain_idle_minutes`, so a proactive message can't interrupt an
    # active exchange; anything she'd surface waits for the next idle pass or is
    # woven into her next reply. (idle None = no user messages yet → nothing to
    # interrupt, so the pass still runs to tend memory.) Mechanical maintenance
    # already ran above; harvest isn't lost — the watermark only advances on a
    # real pass, and pre-compaction/close harvest still consolidates between passes.
    if idle_minutes is not None and idle_minutes < config.maintain_idle_minutes:
        log.info("[maintain] LLM pass skipped — user active (idle=%.1f min < %d).",
                 idle_minutes, config.maintain_idle_minutes)
        result["outcome"] = "skipped_user_active"
        result["idle_minutes"] = round(idle_minutes, 1)
        return result

    # Harvest source: the user conversation not yet consolidated. Consolidation
    # is the agent's job now — she reads this and records durable facts
    # (memory_write) + one episode in her voice (record_episode), rather than a
    # neutral extractor guessing entities. Shares the consolidation watermark
    # with the pre-compaction/close harvest, so a message is never consolidated
    # twice or missed.
    # Read the harvest range, dispatch the pass, and advance the watermark all
    # under the maintenance lock — same serialization as the pre-compaction
    # harvest (harvest_conversation), so the two can't double-harvest the same
    # span or regress each other's watermark. The watermark advances to the
    # harvested end_idx (set only when there was content), so a concurrent user
    # append past end_idx is left for the next harvest rather than skipped.
    user_key = f"user:{config.user_name}"
    tid = str(uuid.uuid4())
    async with get_session_lock(_MAINTAIN_SESSION_KEY):
        conversation = ""
        advance_to: tuple[str, int, int] | None = None
        if user_key in await session_mgr.list_contacts():
            user_session = await session_mgr.get_or_create(user_key)
            start_idx, end_idx = await consolidation.get_unprocessed_range(
                user_session.id, user_session.messages,
                user_session.compaction_count, pool,
            )
            if end_idx > start_idx:
                conversation = consolidation.serialize_messages(
                    user_session.messages, start_idx, end_idx,
                )
                advance_to = (user_session.id, user_session.compaction_count, end_idx)

        brief = _build_maintain_brief(
            protocol=protocol,
            now_local=_now_local_line(config.user_timezone),
            last_pass_at=state.last_pass_at,
            changed_files=changed_files,
            new_facts=new_facts,
            conversation=conversation,
            idle_line=_format_idle(idle_minutes, config.user_name),
            ledger_path=config.workspace / _LEDGER_RELPATH,
        )

        log.info("[%s] Maintenance pass dispatching (changed=%d, new_facts=%d, convo=%d chars)",
                 tid[:8], len(changed_files), len(new_facts), len(conversation))
        await process_message(
            text=brief,
            sender="maintenance",
            talker="system",
            reply_to="silent",
            session_key=_MAINTAIN_SESSION_KEY,
            trace_id=tid,
        )

        # Harvested span is consolidated — advance the watermark so the next pass
        # (or a pre-compaction/close harvest) won't re-present it.
        if advance_to is not None:
            await consolidation.update_consolidation_state(*advance_to, pool)

    # Advance the marker only after a real pass dispatched.
    maintain_state.save_last_pass(path, now)
    result["outcome"] = "ran"
    result["changed_files"] = changed_files
    result["new_facts"] = len(new_facts)
    result["harvested_chars"] = len(conversation)
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
    session_mgr: SessionManager,
    process_message: Callable[..., Awaitable[None]],
    get_session_lock: Callable[[str], asyncio.Lock],
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


async def handle_session_reset(
    config: Config,
    session_mgr: SessionManager,
    pool: asyncpg.Pool,
    get_session_lock: Callable[[str], asyncio.Lock],
) -> dict[str, Any]:
    """Reset the user session on the weekly schedule (Monday-morning cron).

    Closes the single ``user:<config.user.name>`` session so the next message
    starts on a fresh context, shedding a week of accumulated compaction drift.
    Gated to fire only once the day's diary maintenance has captured continuity,
    never mid-conversation, and never twice for the same week.
    """
    if not config.session_auto_reset_enabled:
        return {"outcome": "disabled"}

    user_key = f"user:{config.user_name}"
    row = await pool.fetchrow(
        "SELECT id, created_at FROM sessions.sessions "
        "WHERE contact = $1 AND closed_at IS NULL",
        user_key,
    )
    if row is None:
        return {"outcome": "no_session"}

    # Continuity first: only reset after today's diary maintenance has written
    # its entry, so the week's context is preserved before the slate is wiped.
    today = time.strftime("%Y-%m-%d")
    if not (config.workspace / "memory" / f"{today}.md").is_file():
        return {"outcome": "waiting_for_diary"}

    # Idempotent across the morning retries: a session already opened this week
    # (Monday 00:00 local onward) is left untouched.
    now = _dt.datetime.now().astimezone()
    week_start = (now - _dt.timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    if row["created_at"].astimezone() >= week_start:
        return {"outcome": "already_reset_this_week"}

    # Never cut a live conversation.
    idle = await maintain_state.idle_minutes_since_user(pool, user_key)
    if idle is None or idle < config.session_auto_reset_idle_minutes:
        return {"outcome": "user_active", "idle_minutes": round(idle or 0.0, 1)}

    async with get_session_lock(user_key):
        await session_mgr.close_session(user_key)
    log.info("Weekly session reset: closed %s (%s, idle %.0fm)",
             user_key, row["id"], idle)
    return {"outcome": "reset", "closed_session": row["id"]}
