"""State and change-detection helpers for the self-maintenance heartbeat.

The ``/maintain`` heartbeat (operations.py::handle_maintain) runs a periodic
self-maintenance pass as a ``system:maintenance`` LLM turn. This module owns
the small amount of framework-managed state and the change detection the pass
needs to orient itself:

- ``MaintainState`` — the on-disk marker (``last_pass_at``) round-tripped
  through ``<data_dir>/maintain/state.json``. Read at the start of every
  call; written only after a pass actually dispatches.
- ``changed_workspace_files`` — workspace ``*.md`` files (memory/diary,
  MEMORY.md, USER.md, notes, …) with an mtime newer than the last pass.
- ``facts_created_since`` — structured facts inserted since the last pass.
- ``idle_minutes_since_user`` — minutes since the last ``user:<name>`` message,
  reported in the brief so the agent gates its reach-out per MAINTAIN.md.

The pass-time brief header (built in operations.py) consumes these.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import asyncpg

log = logging.getLogger("lucyd")

# Workspace files the pass cares about: Markdown memory artefacts. Skill
# bundles, the avatar, and binary assets are out of scope for the diff.
_WORKSPACE_GLOB = "*.md"
_WORKSPACE_SUBDIRS = ("memory", "notes")

_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class MaintainState:
    """Framework-managed marker for the maintenance heartbeat.

    ``last_pass_at`` is ``None`` on first run (no state file yet, or an
    unreadable/blank marker), which the pass treats as "first pass".
    """

    last_pass_at: _dt.datetime | None


def state_path(data_dir: Path) -> Path:
    """Resolve the heartbeat state file under the agent's data dir."""
    return data_dir / "maintain" / "state.json"


def load_state(path: Path) -> MaintainState:
    """Read the heartbeat marker. Missing/invalid → first-run (``None``)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        log.debug("maintain: state file unavailable (%s): %s", path, e)
        return MaintainState(last_pass_at=None)
    raw = data.get("last_pass_at")
    if not isinstance(raw, str):
        return MaintainState(last_pass_at=None)
    parsed = _parse_iso_utc(raw)
    return MaintainState(last_pass_at=parsed)


def save_last_pass(path: Path, when: _dt.datetime) -> None:
    """Persist ``last_pass_at`` atomically. Called only after a real pass."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"last_pass_at": _format_iso_utc(when)})
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def changed_workspace_files(
    workspace: Path,
    since: _dt.datetime | None,
) -> list[str]:
    """Workspace ``*.md`` files modified since *since* (root + memory/ + notes/).

    ``since=None`` (first pass) returns every file — there is no prior marker
    to diff against, so the whole set is "new" to this pass. Paths are returned
    relative to *workspace*, sorted.
    """
    if not workspace.is_dir():
        return []
    cutoff = since.timestamp() if since is not None else None
    found: list[str] = []
    for path in _iter_workspace_md(workspace):
        try:
            mtime = path.stat().st_mtime
        except OSError as e:
            log.warning("maintain: failed to stat %s: %s", path, e)
            continue
        if cutoff is None or mtime > cutoff:
            found.append(str(path.relative_to(workspace)))
    return sorted(found)


def _iter_workspace_md(workspace: Path) -> list[Path]:
    """Markdown files at the workspace root and in the tracked subdirs."""
    paths: list[Path] = [p for p in workspace.glob(_WORKSPACE_GLOB) if p.is_file()]
    for sub in _WORKSPACE_SUBDIRS:
        sub_dir = workspace / sub
        if sub_dir.is_dir():
            paths.extend(p for p in sub_dir.glob(_WORKSPACE_GLOB) if p.is_file())
    return paths


async def facts_created_since(
    pool: asyncpg.Pool,
    since: _dt.datetime | None,
) -> list[str]:
    """Structured facts inserted since *since*, as ``entity · attribute · value``.

    ``since=None`` (first pass) returns an empty list — a first pass has no
    prior marker, and dumping the entire fact store into the brief would be
    noise rather than a diff.
    """
    if since is None:
        return []
    rows = await pool.fetch(
        """SELECT entity, attribute, value
             FROM knowledge.facts
            WHERE created_at > $1
              AND invalidated_at IS NULL
            ORDER BY created_at""",
        since,
    )
    return [f"{r['entity']} · {r['attribute']} · {r['value']}" for r in rows]


async def idle_minutes_since_user(
    pool: asyncpg.Pool,
    user_session_key: str,
) -> float | None:
    """Minutes since the last message in *user_session_key* (``None`` if never).

    The conceptual key (e.g. ``user:<name>``) lives in
    ``sessions.sessions.contact``; ``sessions.messages.session_id`` is the row
    UUID, so the join is required.
    """
    last_ts: float | None = await pool.fetchval(
        """SELECT EXTRACT(EPOCH FROM MAX(m.created_at))
             FROM sessions.messages m
             JOIN sessions.sessions s ON s.id = m.session_id
            WHERE s.contact = $1""",
        user_session_key,
    )
    if last_ts is None:
        return None
    now = _dt.datetime.now(_dt.timezone.utc).timestamp()
    return max(0.0, (now - float(last_ts)) / 60.0)


def _parse_iso_utc(raw: str) -> _dt.datetime | None:
    """Parse an ISO8601 UTC string (``...Z``) to an aware datetime, or None."""
    try:
        return _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        log.debug("maintain: unparseable last_pass_at: %r", raw)
        return None


def _format_iso_utc(when: _dt.datetime) -> str:
    """Format an aware datetime as a UTC ``...Z`` ISO8601 string."""
    return when.astimezone(_dt.timezone.utc).strftime(_ISO_FORMAT)
