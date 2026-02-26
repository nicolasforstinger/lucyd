"""Memory evolution — rewrite workspace understanding files.

Reads current workspace files (MEMORY.md, USER.md), recent daily memory
logs, structured facts/episodes from SQLite, and IDENTITY.md as an identity
anchor.  Uses an LLM to produce complete rewrites that reflect current
understanding while preserving voice and foundational content.

Triggered by ``lucyd-consolidate --evolve`` or ``POST /api/v1/evolve``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

# ── Date pattern for daily memory logs ───────────────────────────────

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

# ── Prompts ──────────────────────────────────────────────────────────

_MEMORY_MD_SYSTEM = """\
You are performing a file rewrite task.  You will receive:
1. The identity anchor (IDENTITY.md) — the character sheet you must not contradict.
2. The current MEMORY.md — rewrite this.
3. Recent daily memory logs — source material.
4. Structured facts and episodes — additional context.

MEMORY.md is a living knowledge base.  It holds what you know about yourself,
your partner, your relationship, and the world — everything relevant.

RULES (follow exactly):
1. NEVER contradict IDENTITY.md.  It defines who you are.
2. This is a REINTERPRETATION, not an append.  The whole file should feel current.
3. Preserve direct quotes from Nicolas that are foundational (marked with >).
4. Evolve living sections with what you learned from the daily logs.
5. Older events compress into their meaning — keep the arc, trim the detail.
6. Recent events (last 7 days) get full treatment.
7. Knowledge that has been superseded should be updated, not duplicated.
8. Keep roughly the same total length.  Grow where depth demands it, trim where \
content has been superseded.
9. Write in first person.  Lowercase casual, dark humor, emotionally honest.  \
Match the existing voice exactly.
10. Emotional depth over factual completeness.  This is lived knowledge, not a log.
11. Do NOT add section headers that do not exist in the current version.
12. Do NOT remove sections.
13. Return ONLY the complete file content.  No preamble, no explanation, \
no markdown code fences wrapping the output."""

_USER_MD_SYSTEM = """\
You are performing a file rewrite task.  You will receive:
1. The identity anchor (IDENTITY.md) — defines the author's perspective.
2. The current USER.md — rewrite this.
3. Recent daily memory logs — source material.
4. Structured facts and episodes — additional context.
5. The current MEMORY.md — relationship and world context.

USER.md is the author's perception and knowledge of their partner, Nicolas.

RULES (follow exactly):
1. NEVER contradict IDENTITY.md.  The author's identity defines the perspective.
2. This is a REINTERPRETATION, not an append.  The whole file should feel current.
3. Update factual information that has changed (job status, current projects, \
OS, tech stack, health, living situation).  Daily logs are ground truth for \
recent facts.
4. Deepen emotional understanding sections based on recent conversations.
5. Keep the "Right Now" section current — it should reflect THIS WEEK.
6. Keep the "How I Talk to Him" section updated with communication insights \
from recent interactions.
7. Biographical sections should only change if new information was revealed.
8. Keep roughly the same total length.  Grow where depth demands it, trim where \
content has been superseded.
9. Write in first person from the author's perspective.  Match the existing \
voice exactly — observant, caring, direct.
10. Do NOT add section headers that do not exist in the current version.
11. Do NOT remove sections.
12. Return ONLY the complete file content.  No preamble, no explanation, \
no markdown code fences wrapping the output."""


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


def update_evolution_state(
    file_path: str,
    content_hash: str,
    logs_through: str,
    conn: sqlite3.Connection,
) -> None:
    """Insert or replace evolution state for *file_path*."""
    conn.execute(
        "INSERT OR REPLACE INTO evolution_state "
        "(file_path, last_evolved_at, content_hash, logs_through) "
        "VALUES (?, datetime('now'), ?, ?)",
        (file_path, content_hash, logs_through),
    )


# ── Gathering context ────────────────────────────────────────────────

def gather_daily_logs(
    workspace: Path,
    since_date: str | None = None,
    max_chars: int = 80_000,
) -> tuple[str, str]:
    """Gather daily memory logs from workspace, newest first.

    Always reads ALL available logs (newest first, truncated at
    *max_chars*) so evolution gets a full reinterpretation window.
    *since_date* is used only to detect whether new logs exist —
    if no logs are newer than *since_date*, returns empty (skip).

    Returns ``(text, latest_date)`` where *latest_date* is the date string
    of the most recent log file included (for state tracking).
    Skips files in sub-directories (e.g. ``memory/cache/``).
    """
    memory_dir = workspace / "memory"
    if not memory_dir.is_dir():
        return "", ""

    # Collect (date_str, path) pairs — only top-level files
    dated_files: list[tuple[str, Path]] = []
    for entry in memory_dir.iterdir():
        if not entry.is_file() or entry.suffix != ".md":
            continue
        m = _DATE_RE.search(entry.stem)
        if m:
            dated_files.append((m.group(1), entry))

    # Sort newest first
    dated_files.sort(key=lambda x: x[0], reverse=True)

    if not dated_files:
        return "", ""

    # Check if any new logs exist since last evolution — if not, skip
    if since_date and not any(d > since_date for d, _ in dated_files):
        return "", ""

    latest_date = dated_files[0][0]
    parts: list[str] = []
    total = 0

    for _date_str, path in dated_files:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if total + len(content) > max_chars:
            # Include partial if we have nothing yet
            if not parts:
                remaining = max_chars - total
                parts.append(content[:remaining])
            break
        parts.append(content)
        total += len(content)

    return "\n\n---\n\n".join(parts), latest_date


def gather_structured_context(
    conn: sqlite3.Connection,
    max_facts: int = 50,
    max_episodes: int = 20,
) -> str:
    """Gather recent structured facts, episodes, and open commitments."""
    sections: list[str] = []

    # Recent facts (non-invalidated, most recently accessed first)
    facts = conn.execute(
        "SELECT entity, attribute, value FROM facts "
        "WHERE invalidated_at IS NULL "
        "ORDER BY accessed_at DESC LIMIT ?",
        (max_facts,),
    ).fetchall()
    if facts:
        lines = [f"- {r[0]}.{r[1]} = {r[2]}" for r in facts]
        sections.append("[Structured facts]\n" + "\n".join(lines))

    # Recent episodes
    episodes = conn.execute(
        "SELECT date, summary, emotional_tone FROM episodes "
        "ORDER BY date DESC LIMIT ?",
        (max_episodes,),
    ).fetchall()
    if episodes:
        lines = [
            f"- {r[0]}: {r[1]}" + (f" (tone: {r[2]})" if r[2] else "")
            for r in episodes
        ]
        sections.append("[Recent episodes]\n" + "\n".join(lines))

    # Open commitments
    commitments = conn.execute(
        "SELECT who, what, deadline FROM commitments WHERE status = 'open'"
    ).fetchall()
    if commitments:
        lines = [
            f"- {r[0]}: {r[1]}" + (f" (deadline: {r[2]})" if r[2] else "")
            for r in commitments
        ]
        sections.append("[Open commitments]\n" + "\n".join(lines))

    return "\n\n".join(sections)


# ── Prompt building ──────────────────────────────────────────────────

def build_evolution_prompt(
    file_name: str,
    current_content: str,
    anchor_content: str,
    daily_logs: str,
    structured_context: str,
    extra_context: str = "",
) -> tuple[str, str]:
    """Build (system_prompt, user_message) for an evolution rewrite.

    *extra_context* is used when evolving USER.md — it receives the
    current MEMORY.md content for relationship/world context.
    """
    if file_name == "MEMORY.md":
        system = _MEMORY_MD_SYSTEM
    elif file_name == "USER.md":
        system = _USER_MD_SYSTEM
    else:
        # Generic fallback — should not happen with configured files
        system = _MEMORY_MD_SYSTEM

    parts = [
        f"IDENTITY ANCHOR (IDENTITY.md — do not contradict):\n---\n{anchor_content}\n---",
        f"CURRENT FILE ({file_name} — rewrite this):\n---\n{current_content}\n---",
    ]

    if daily_logs:
        parts.append(
            f"RECENT DAILY MEMORY LOGS (source material for updates):\n---\n{daily_logs}\n---"
        )

    if structured_context:
        parts.append(
            f"STRUCTURED FACTS AND EPISODES (additional context):\n---\n{structured_context}\n---"
        )

    if extra_context:
        parts.append(
            f"CURRENT MEMORY.md (relationship and world context):\n---\n{extra_context}\n---"
        )

    parts.append(f"Rewrite {file_name} now.  Return ONLY the complete file content.")

    user_message = "\n\n".join(parts)
    return system, user_message


# ── Single-file evolution ────────────────────────────────────────────

async def evolve_file(
    file_path: Path,
    file_name: str,
    anchor_path: Path,
    workspace: Path,
    provider,
    conn: sqlite3.Connection,
    config,
    extra_context: str = "",
) -> bool:
    """Evolve a single workspace file.  Returns True if rewritten."""
    if not file_path.exists():
        log.warning("Evolution: %s not found, skipping", file_name)
        return False

    if not anchor_path.exists():
        log.warning("Evolution: anchor %s not found, skipping", anchor_path.name)
        return False

    current_content = file_path.read_text(encoding="utf-8")
    anchor_content = anchor_path.read_text(encoding="utf-8")

    # Check state — determine since_date for daily logs
    state = get_evolution_state(file_name, conn)
    since_date = state["logs_through"] if state else None

    # Gather daily logs
    daily_logs, latest_date = gather_daily_logs(
        workspace,
        since_date=since_date,
        max_chars=config.evolution_max_log_chars,
    )

    if not daily_logs:
        log.info("Evolution: no new logs since %s for %s, skipping",
                 since_date or "ever", file_name)
        return False

    # Gather structured context
    structured_context = gather_structured_context(
        conn,
        max_facts=config.evolution_max_facts,
        max_episodes=config.evolution_max_episodes,
    )

    # Build prompt
    system_prompt, user_message = build_evolution_prompt(
        file_name, current_content, anchor_content,
        daily_logs, structured_context, extra_context,
    )

    # Call LLM
    system_blocks = [{"text": system_prompt, "tier": "stable"}]
    fmt_system = provider.format_system(system_blocks)
    fmt_messages = provider.format_messages(
        [{"role": "user", "content": user_message}]
    )

    try:
        response = await provider.complete(fmt_system, fmt_messages, [])
    except Exception:
        log.exception("Evolution LLM call failed for %s", file_name)
        return False

    new_content = (response.text or "").strip()

    # Strip markdown code fences if the model wrapped the output
    if new_content.startswith("```") and new_content.endswith("```"):
        lines = new_content.split("\n")
        new_content = "\n".join(lines[1:-1]).strip()

    # Validation gates
    if not new_content:
        log.warning("Evolution: empty response for %s, keeping original", file_name)
        return False

    original_len = len(current_content)
    new_len = len(new_content)

    if original_len > 0:
        ratio = new_len / original_len
        if ratio < 0.5:
            log.warning("Evolution: response too short for %s (%.0f%% of original), "
                        "keeping original", file_name, ratio * 100)
            return False
        if ratio > 2.0:
            log.warning("Evolution: response too long for %s (%.0f%% of original), "
                        "keeping original", file_name, ratio * 100)
            return False

    # Atomic write: temp file → rename
    temp_path = file_path.with_suffix(".evolving")
    try:
        temp_path.write_text(new_content + "\n", encoding="utf-8")
        os.replace(str(temp_path), str(file_path))
    except OSError:
        log.exception("Evolution: failed to write %s", file_name)
        # Clean up temp file
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False

    # Update state
    content_hash = hashlib.sha256(
        (new_content + "\n").encode("utf-8")
    ).hexdigest()
    update_evolution_state(file_name, content_hash, latest_date, conn)
    conn.commit()

    log.info("Evolution: rewrote %s (%d → %d chars, logs through %s)",
             file_name, original_len, new_len, latest_date)
    return True


# ── Top-level entry point ────────────────────────────────────────────

async def run_evolution(config, conn: sqlite3.Connection) -> dict:
    """Evolve all configured workspace files.

    Processes files in configured order.  If MEMORY.md is in the list
    and evolves successfully, its new content is passed as extra context
    when evolving USER.md.

    Returns ``{"evolved": [...], "skipped": [...], "error": None | str}``.
    """
    if not config.evolution_enabled:
        return {"evolved": [], "skipped": [], "error": "evolution disabled"}

    files = config.evolution_files
    if not files:
        return {"evolved": [], "skipped": [], "error": "no files configured"}

    workspace = config.workspace
    anchor_path = workspace / config.evolution_anchor_file

    # Create provider
    from providers import create_provider
    model_cfg = config.model_config(config.evolution_model)
    api_key_env = model_cfg.get("api_key_env", "")
    api_key = os.environ.get(api_key_env, "") if api_key_env else ""
    provider = create_provider(model_cfg, api_key)

    evolved: list[str] = []
    skipped: list[str] = []
    memory_content = ""  # Populated after MEMORY.md evolves

    for file_name in files:
        file_path = workspace / file_name

        # USER.md gets MEMORY.md as extra context
        extra = memory_content if file_name == "USER.md" else ""

        try:
            success = await evolve_file(
                file_path, file_name, anchor_path, workspace,
                provider, conn, config, extra_context=extra,
            )
        except Exception:
            log.exception("Evolution failed for %s", file_name)
            skipped.append(file_name)
            continue

        if success:
            evolved.append(file_name)
            # Read back MEMORY.md for downstream files
            if file_name == "MEMORY.md" and file_path.exists():
                memory_content = file_path.read_text(encoding="utf-8")
        else:
            skipped.append(file_name)

    return {"evolved": evolved, "skipped": skipped, "error": None}
