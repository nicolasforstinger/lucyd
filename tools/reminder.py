"""Reminder tool — schedule a future message to the agent's own session.

Uses the ``at`` daemon (started by the entrypoint) to schedule a curl
call back to the agent's own HTTP API at the specified time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from . import ToolSpec

if TYPE_CHECKING:
    from config import Config

log = logging.getLogger(__name__)

_http_token: str = ""
_http_port: int = 8100


def configure(config: Config | None = None, **_: object) -> None:
    global _http_token, _http_port
    if config is not None:
        _http_token = config.http_auth_token
        _http_port = config.http_port


async def tool_reminder(message: str, minutes: int = 5) -> str:
    """Schedule a reminder that arrives as a system message after N minutes."""
    if not shutil.which("at"):
        return "Error: 'at' command not available in this container"
    if minutes < 1:
        return "Error: minutes must be at least 1"
    if minutes > 1440:
        return "Error: maximum reminder is 1440 minutes (24 hours)"
    if not message.strip():
        return "Error: reminder message cannot be empty"

    # Build JSON payload safely via json.dumps (handles all escaping)
    payload = json.dumps({
        "message": f"Reminder: {message}",
        "sender": "self",
    })

    # Write the curl command to a temp file — avoids all shell quoting issues.
    # The script self-deletes after execution.
    headers = '-H "Content-Type: application/json"'
    if _http_token:
        headers += f' -H "Authorization: Bearer {_http_token}"'

    script = tempfile.NamedTemporaryFile(
        mode="w", suffix=".sh", prefix="lucyd-reminder-",
        delete=False,
    )
    script_path = script.name
    script.write("#!/bin/sh\n")
    script.write(f"curl -s -X POST http://localhost:{_http_port}/api/v1/agent/action "
                 f"{headers} -d {shlex.quote(payload)}\n")
    script.write(f"rm -f {shlex.quote(script_path)}\n")
    script.close()
    Path(script_path).chmod(0o700)

    cmd = f"at -f {shlex.quote(script_path)} now + {minutes} minutes"

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        err = stderr.decode("utf-8", errors="replace") if stderr else ""
        if proc.returncode != 0:
            # Clean up the script on failure
            Path(script_path).unlink(missing_ok=True)
            return f"Error: Failed to schedule reminder: {err}"

        log.info("Reminder scheduled: '%s' in %d minutes", message[:80], minutes)
        return f"Reminder set: \"{message}\" in {minutes} minutes"
    except (OSError, TimeoutError) as e:
        Path(script_path).unlink(missing_ok=True)
        return f"Error: Failed to schedule reminder: {type(e).__name__}: {e}"


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="reminder",
        description=(
            "Schedule a reminder that will arrive as a message after the "
            "specified number of minutes. Use this when asked to remind "
            "about something later."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "What to remind about",
                },
                "minutes": {
                    "type": "integer",
                    "description": "Minutes from now (default: 5, max: 1440)",
                    "default": 5,
                },
            },
            "required": ["message"],
        },
        function=tool_reminder,
    ),
]
