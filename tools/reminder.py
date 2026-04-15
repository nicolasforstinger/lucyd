"""Reminder tool — schedule a future message to the agent's own session.

Uses the ``at`` daemon (started by the entrypoint) to schedule a curl
call back to the agent's own HTTP API at the specified time.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import TYPE_CHECKING

from . import ToolSpec

if TYPE_CHECKING:
    from config import Config

log = logging.getLogger(__name__)

_http_token: str = ""


def configure(config: Config | None = None, **_: object) -> None:
    global _http_token
    if config is not None:
        _http_token = config.http_auth_token


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

    # Build the curl command that sends the reminder back to the agent
    headers = '-H "Content-Type: application/json"'
    if _http_token:
        headers += f' -H "Authorization: Bearer {_http_token}"'

    # Escape double quotes in the message for JSON
    safe_msg = message.replace("\\", "\\\\").replace('"', '\\"')
    payload = f'{{"message": "Reminder: {safe_msg}", "sender": "system", "task_type": "system"}}'
    curl_cmd = f'curl -s -X POST http://localhost:8100/api/v1/message {headers} -d \'{payload}\''

    cmd = f'echo \'{curl_cmd}\' | at now + {minutes} minutes'

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        err = stderr.decode("utf-8", errors="replace") if stderr else ""
        if proc.returncode != 0:
            return f"Error: Failed to schedule reminder: {err}"

        log.info("Reminder scheduled: '%s' in %d minutes", message[:80], minutes)
        return f"Reminder set: \"{message}\" in {minutes} minutes"
    except Exception as e:
        return f"Error: Failed to schedule reminder: {type(e).__name__}"


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
