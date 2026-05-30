"""Scheduled-task tools — `remind_user` and `schedule_self_task`.

Both use the OS ``at`` daemon (started by entrypoint.sh, with the spool
host-mounted via named volume so jobs survive container recreation).

Everything the agent does is an agent action. Both tools fire as an
agent:self turn at the scheduled time (``POST /api/v1/agent/action``,
``sender="self"``); they differ only in intent marker, which the
pipeline uses to frame the turn:

- ``remind_user`` — the **user's** item. He asked to be reminded of
  something. Fires with the ``[Reminder]`` marker. At fire time the
  agent delivers the reminder's substance to the user, woven into the
  current situation. Delivery is mandatory; weaving is about fit.

- ``schedule_self_task`` — the **agent's** own deferred work. Fires
  with the ``[Scheduled task]`` marker. The agent does the work; a
  user-facing message at the end is optional and situational.

Both get the recent user-conversation tail injected into the fire-time
turn (see pipeline) so delivery fits what's actually happening rather
than firing context-blind. Instructions/messages MUST be self-contained
because compaction may erase the conversation that led to scheduling.

Legacy: ``/api/v1/outbound/send`` (verbatim, no agent turn) is retained
only as the low-level bridge primitive and as frozen compat for any
at-job spooled before this change — it is no longer a reminder path.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import ToolSpec
from log_utils import redact_content

if TYPE_CHECKING:
    from config import Config

log = logging.getLogger(__name__)

# Far-future cap: 1 year. A sanity bound — far-future instructions must
# be self-contained per the schedule_self_task contract.
_MAX_DELAY = dt.timedelta(days=366)


# DI-injected at daemon startup.
_http_auth_token: str = ""
_http_port: int = 8100
_user_tz: dt.tzinfo = dt.timezone.utc


def configure(
    config: Config | None = None,
    *,
    http_auth_token: str = "",
    http_port: int = 8100,
    user_timezone: str = "UTC",
    **_: object,
) -> None:
    """Wire dependencies. Called once at daemon startup."""
    global _http_auth_token, _http_port, _user_tz
    if config is not None:
        _http_auth_token = config.http_auth_token
        _http_port = config.http_port
        tz_name = config.user_timezone
    else:
        _http_auth_token = http_auth_token
        _http_port = http_port
        tz_name = user_timezone
    try:
        _user_tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("Unknown user timezone %r; falling back to UTC", tz_name)
        _user_tz = dt.timezone.utc


def _resolve_when(when: str) -> tuple[str, str, str]:
    """Resolve an absolute local datetime to an ``at -t`` UTC stamp.

    ``when`` is ISO 8601 in the user's timezone (e.g. '2026-05-26 14:00').
    The framework — never the model — does all clock math: attach the user
    tz if the value is naive, convert to UTC, validate 1 min to 1 year out.
    Returns ``(stamp, human_display, "")`` on success or ``("", "", error)``.
    ``stamp`` is ``YYYYMMDDHHMM`` UTC; ``human_display`` echoes weekday +
    local time so the caller can verify the date it actually scheduled.
    """
    try:
        parsed = dt.datetime.fromisoformat(when.strip())
    except ValueError:
        return "", "", (
            "Error: 'when' must be an absolute ISO 8601 local datetime like "
            f"'2026-05-26 14:00' or '2026-05-26T14:00' (got {when!r})"
        )
    local = parsed.replace(tzinfo=_user_tz) if parsed.tzinfo is None else parsed
    target_utc = local.astimezone(dt.timezone.utc)
    delta = target_utc - dt.datetime.now(dt.timezone.utc)
    if delta < dt.timedelta(minutes=1):
        return "", "", "Error: 'when' must be at least 1 minute in the future"
    if delta > _MAX_DELAY:
        return "", "", "Error: 'when' must be within 1 year"
    return (
        target_utc.strftime("%Y%m%d%H%M"),
        local.strftime("%A %Y-%m-%d %H:%M %Z"),
        "",
    )


async def _schedule_at_job(script_body: str, at_stamp: str) -> str:
    """Write the script to a tempfile and submit it via ``at -t``.

    ``at_stamp`` is a UTC ``YYYYMMDDHHMM``; ``TZ=UTC`` pins ``at -t``'s
    interpretation regardless of container tz. Returns an error string on
    failure, empty string on success.
    """
    if not shutil.which("at"):
        return "Error: 'at' command not available in this container"

    script = tempfile.NamedTemporaryFile(
        mode="w", suffix=".sh", prefix="lucyd-scheduled-", delete=False,
    )
    script_path = script.name
    script.write("#!/bin/sh\n")
    script.write(script_body)
    script.write(f"\nrm -f {shlex.quote(script_path)}\n")
    script.close()
    Path(script_path).chmod(0o700)

    cmd = f"TZ=UTC at -t {shlex.quote(at_stamp)} -f {shlex.quote(script_path)}"
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        err = stderr.decode("utf-8", errors="replace") if stderr else ""
        if proc.returncode != 0:
            Path(script_path).unlink(missing_ok=True)
            return f"Error: failed to schedule via at: {err}"
        return ""
    except (OSError, TimeoutError) as e:
        Path(script_path).unlink(missing_ok=True)
        return f"Error: failed to schedule via at: {type(e).__name__}: {e}"


async def _run_at(cmd: str) -> tuple[int, str, str]:
    """Run an ``at``/``atrm`` command, returning (rc, stdout, stderr)."""
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout=10)
    rc = proc.returncode if proc.returncode is not None else -1
    return rc, out.decode("utf-8", errors="replace"), err.decode("utf-8", errors="replace")


# `at -l` line: "<id>\t<Day Mon DD HH:MM:SS YYYY> <queue> <user>".
_AT_LIST_RE = re.compile(r"^(\d+)\s+(.+?)\s+\w\s+\S+$")
# Pull the [Reminder]/[Scheduled task] intent from a spooled job's curl payload.
_JOB_MSG_RE = re.compile(r'"(?:message|text)":\s*"((?:[^"\\]|\\.)*)"')


async def _job_intent(job_id: str) -> str:
    """Best-effort one-line intent of a spooled job, for list_scheduled."""
    try:
        rc, out, _ = await _run_at(f"at -c {shlex.quote(job_id)}")
    except (OSError, TimeoutError):
        return "(unreadable)"
    if rc != 0:
        return "(unreadable)"
    m = _JOB_MSG_RE.search(out)
    if not m:
        return "(no intent found)"
    raw = m.group(1).encode("utf-8").decode("unicode_escape", errors="replace")
    return raw[:160]


def _at_job_body(framed_message: str) -> str:
    """Build the curl-POST at-job script body for a framed agent:self message."""
    payload = json.dumps({"message": framed_message, "sender": "self"})
    headers = '-H "Content-Type: application/json"'
    if _http_auth_token:
        headers += f' -H "Authorization: Bearer {_http_auth_token}"'
    return (
        f"curl -s -X POST http://localhost:{_http_port}/api/v1/agent/action "
        f"{headers} -d {shlex.quote(payload)}\n"
    )


async def tool_remind_user(message: str, when: str) -> str:
    """Schedule a user-requested reminder, delivered as a situational agent turn."""
    if not message.strip():
        return "Error: message must not be empty"
    at_stamp, display, err = _resolve_when(when)
    if err:
        return err
    err = await _schedule_at_job(_at_job_body(f"[Reminder] {message}"), at_stamp)
    if err:
        return err
    log.info("remind_user scheduled for %s: %s", display, redact_content(message, 80))
    return f"Scheduled: reminder for {display}"


async def tool_schedule_self_task(instruction: str, when: str) -> str:
    """Schedule a future agent:self work turn at an absolute time."""
    if not instruction.strip():
        return "Error: instruction must not be empty"
    at_stamp, display, err = _resolve_when(when)
    if err:
        return err
    err = await _schedule_at_job(_at_job_body(f"[Scheduled task] {instruction}"), at_stamp)
    if err:
        return err
    log.info("schedule_self_task scheduled for %s: %s", display, instruction[:80])
    return f"Scheduled: self-task for {display}"


async def tool_list_scheduled() -> str:
    """List your pending scheduled jobs (reminders + self-tasks) with id, time, intent."""
    if not shutil.which("at"):
        return "Error: 'at' command not available in this container"
    try:
        rc, out, err = await _run_at("at -l")
    except (OSError, TimeoutError) as e:
        return f"Error: failed to list scheduled jobs: {type(e).__name__}: {e}"
    if rc != 0:
        return f"Error: failed to list scheduled jobs: {err.strip()}"
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if not lines:
        return "No scheduled jobs pending."
    rendered: list[str] = []
    for ln in lines:
        m = _AT_LIST_RE.match(ln.strip())
        if not m:
            continue
        job_id, when_str = m.group(1), m.group(2).strip()
        rendered.append(f"  [{job_id}] {when_str} — {await _job_intent(job_id)}")
    if not rendered:
        return "No scheduled jobs pending."
    return (
        "Pending scheduled jobs (times shown in the container's UTC clock):\n"
        + "\n".join(rendered)
        + "\nTo reschedule: cancel_scheduled the old id, then create the new one."
    )


async def tool_cancel_scheduled(job_id: int) -> str:
    """Cancel a pending scheduled job by its id (from list_scheduled)."""
    if not shutil.which("atrm"):
        return "Error: 'atrm' command not available in this container"
    try:
        rc, _, err = await _run_at(f"atrm {int(job_id)}")
    except (OSError, TimeoutError) as e:
        return f"Error: failed to cancel job: {type(e).__name__}: {e}"
    if rc != 0:
        return f"Error: could not cancel job {job_id} ({err.strip() or 'no such job?'})"
    log.info("cancel_scheduled removed job %s", job_id)
    return f"Cancelled scheduled job {job_id}."


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="remind_user",
        description=(
            "Use this when the USER asked to be reminded of something — he said "
            "'remind me to X', 'remind me in an hour', 'we need to do Y in two "
            "weeks', or otherwise wants to be brought back to something later. "
            "It is HIS item.\n\n"
            "Decision rule (do not get this wrong): if HE asked to be reminded → "
            "remind_user. If YOU decided to do work later (research, cleanup, a "
            "follow-up you chose) → schedule_self_task. 'I want him to hear "
            "something later' is remind_user. 'I need to DO something later' is "
            "schedule_self_task.\n\n"
            "At fire time you wake as a normal agent turn WITH the recent "
            "conversation in view, and you deliver the reminder to him via "
            "send_message — woven naturally into whatever is happening. "
            "Mid-conversation: fold it in ('btw — you wanted me to flag X'). "
            "Quiet: a clean standalone. Delivering the reminder's substance is "
            "MANDATORY; the weaving only controls how it fits, never whether it "
            "is sent.\n\n"
            "'when' is an ABSOLUTE local datetime in the user's timezone, e.g. "
            "'2026-05-26 14:00' — NOT a duration. State the wall-clock time he "
            "wants; the framework does all the date/offset math. The success "
            "reply echoes back the weekday + date it scheduled — read it and "
            "confirm it's the day you meant.\n"
            "To change or cancel a reminder you already set: use list_scheduled "
            "to find its id, cancel_scheduled to remove it, then create the new "
            "one. If you tell the user you'll move or cancel a reminder, you MUST "
            "actually call cancel_scheduled — saying it is not doing it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": (
                        "What he needs reminded of (the substance/intent). You "
                        "deliver it in-context at fire time — it is not sent "
                        "verbatim, so capture the WHAT, not exact wording."
                    ),
                },
                "when": {
                    "type": "string",
                    "description": (
                        "Absolute local datetime in the user's timezone, ISO "
                        "8601, e.g. '2026-05-26 14:00'. Must be 1 min to 1 year "
                        "out. The framework converts and validates — never pass "
                        "a duration or do offset math yourself."
                    ),
                },
            },
            "required": ["message", "when"],
        },
        function=tool_remind_user,
    ),
    ToolSpec(
        name="schedule_self_task",
        description=(
            "Use this when YOU decide something needs doing later — research, "
            "cleanup, a follow-up check, processing something on a future date. "
            "It is YOUR work, not a message to him.\n\n"
            "Decision rule (do not get this wrong): if YOU need to DO something "
            "later → schedule_self_task. If the USER asked to be reminded of "
            "something, or you only want him to hear something later → "
            "remind_user. Do NOT use schedule_self_task as a way to message him "
            "on a timer — that is the exact misuse this rule exists to stop. Use "
            "it only when there is actual WORK to do.\n\n"
            "At fire time you wake with all tools AND the recent conversation in "
            "view. Do the work. Then decide: does he need to know? If yes, "
            "send_message him about it — woven naturally into the current "
            "situation, never a cold context-blind standalone. If the work does "
            "not concern him, just do it silently.\n\n"
            "'when' is an ABSOLUTE local datetime in the user's timezone (e.g. "
            "'2026-05-26 14:00'), NOT a duration — the framework does the math "
            "and echoes back the weekday it scheduled; verify it. To change a "
            "scheduled task: list_scheduled → cancel_scheduled the old id → "
            "create the new one (and if you told the user you'd move it, "
            "actually cancel it).\n\n"
            "CRITICAL: the instruction must be a SELF-CONTAINED PLAN. At fire "
            "time you may have ZERO memory of this conversation — compaction can "
            "erase any context. Write it for a stranger:\n"
            "- State the goal explicitly (not 'finish what we discussed').\n"
            "- List concrete steps if multi-step.\n"
            "- Include all specific values: file paths, names, parameters.\n"
            "- Reference durable context explicitly: 'see /data/workspace/X.md', "
            "'recall memory about Y' — never 'the thing we talked about'.\n"
            "- State explicitly whether and what to tell the user.\n"
            "- Voice trap: in this fire-time turn the reply is silent, so "
            "`tts` alone does NOT deliver. Generate audio with "
            "`tts(text=..., output_file=\"/tmp/lucyd-tts-<name>.mp3\")` THEN "
            "`send_message(text=\"<caption>\", "
            "attachments=[\"/tmp/lucyd-tts-<name>.mp3\"])`.\n\n"
            "Bad: 'finish the analysis'\n"
            "Good: 'Read /data/workspace/notes/build-2026-04-25.md, identify the "
            "root cause of the test_indexer failure, write findings to "
            "/data/workspace/findings/build-2026-04-25.md, and send_message the "
            "user with a one-line summary.'\n\n"
            "Voice example: 'Generate a voice message saying \"hey, your 5 minutes "
            "are up\" with tts(output_file=\"/tmp/lucyd-tts-5min-up.mp3\"), then "
            "send_message(text=\"voice ping\", attachments=[\"/tmp/lucyd-tts-5min-up.mp3\"]).'"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": "Self-contained plan future-you will execute",
                },
                "when": {
                    "type": "string",
                    "description": (
                        "Absolute local datetime in the user's timezone, ISO "
                        "8601, e.g. '2026-05-26 14:00'. Must be 1 min to 1 year "
                        "out. The framework converts and validates — never pass "
                        "a duration or do offset math yourself."
                    ),
                },
            },
            "required": ["instruction", "when"],
        },
        function=tool_schedule_self_task,
    ),
    ToolSpec(
        name="list_scheduled",
        description=(
            "List your pending scheduled jobs — reminders and self-tasks you've "
            "set for the future — with each job's id, fire time, and a one-line "
            "intent. Use this before rescheduling or cancelling, and any time "
            "you're unsure what's already queued (so you don't double-book or "
            "leave a stale duplicate). Takes no arguments."
        ),
        input_schema={"type": "object", "properties": {}},
        function=tool_list_scheduled,
    ),
    ToolSpec(
        name="cancel_scheduled",
        description=(
            "Cancel a pending scheduled job by its id (get the id from "
            "list_scheduled). Use this to remove a reminder/task that's no "
            "longer right, or as the first half of a reschedule (cancel the old "
            "one, then create the corrected one). If you told the user you'd "
            "move or drop a reminder, calling this is how you actually do it — "
            "do not claim it's done without cancelling."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "integer",
                    "description": "The job id to cancel, as shown by list_scheduled.",
                },
            },
            "required": ["job_id"],
        },
        function=tool_cancel_scheduled,
    ),
]
