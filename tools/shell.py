"""Shell execution tool — exec.

Security boundary: exec runs at the daemon's own uid (1000). It is NOT a
sandbox against a determined caller — same-uid means a command can read the
daemon's own secrets via /proc/<pid>/environ, alternate path spellings, or an
inline interpreter, so no command-string filter is real protection. The
container is the security boundary. What IS enforced here is real but narrow:
the child process's environment is scrubbed of secret-shaped vars (_safe_env),
the command runs with a timeout in its own process group (killed as a group on
timeout), and there is no shell escalation beyond uid 1000.

To close the file-read vector for `/config/.env`, lock it to root:0600 on the
host — the entrypoint sources it as root and exports the vars before dropping
to uid 1000, and config._load_dotenv tolerates an unreadable .env (it relies on
the already-exported env). The /proc same-uid vector remains by design.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import TYPE_CHECKING

from . import ToolSpec

if TYPE_CHECKING:
    from config import Config

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 120
_MAX_TIMEOUT = 600

# Environment variable patterns to filter out of child processes
_SECRET_PREFIXES = ("LUCYD_",)
_SECRET_SUFFIXES = ("_KEY", "_KEY_ID", "_TOKEN", "_SECRET", "_PASSWORD", "_CREDENTIALS", "_CODE", "_PASS")


def configure(default_timeout: int | None = None, max_timeout: int | None = None,
              config: Config | None = None, **_: object) -> None:
    global _DEFAULT_TIMEOUT, _MAX_TIMEOUT
    if config is not None:
        if default_timeout is None:
            default_timeout = config.exec_timeout
        if max_timeout is None:
            max_timeout = config.exec_max_timeout
    if default_timeout is not None:
        _DEFAULT_TIMEOUT = default_timeout
    if max_timeout is not None:
        _MAX_TIMEOUT = max_timeout


def _safe_env() -> dict[str, str]:
    """Build environment dict with secret variables filtered out."""
    env = {}
    for key, val in os.environ.items():
        if any(key.startswith(p) for p in _SECRET_PREFIXES):
            continue
        if any(key.endswith(s) for s in _SECRET_SUFFIXES):
            continue
        env[key] = val
    return env


async def tool_exec(command: str, timeout: int | None = None) -> str:
    """Execute a shell command and return stdout + stderr.

    No command-string path filtering: exec runs at the daemon's uid, so such a
    filter is trivially bypassable and gives false confidence. See the module
    docstring for the actual security boundary.
    """
    if timeout is None:
        timeout = _DEFAULT_TIMEOUT
    timeout = min(timeout, _MAX_TIMEOUT)

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_safe_env(),
            start_new_session=True,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
    except TimeoutError:
        try:
            # Kill entire process group to prevent orphans
            os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
        except (OSError, ProcessLookupError):
            log.debug("killpg failed during timeout cleanup (pid=%s)", proc.pid, exc_info=True)
            try:
                proc.kill()
                await proc.wait()
            except (OSError, ProcessLookupError):
                log.debug("proc.kill() also failed (pid=%s)", proc.pid, exc_info=True)
        return f"Error: Command timed out after {timeout}s"
    except OSError as e:
        return f"Error: Command execution failed: {e}"

    result = ""
    out = stdout.decode("utf-8", errors="replace") if stdout else ""
    err = stderr.decode("utf-8", errors="replace") if stderr else ""

    if out:
        result += out
    if err:
        if result:
            result += "\n"
        result += f"STDERR:\n{err}"

    exit_code = proc.returncode
    if exit_code != 0:
        result += f"\n[exit code: {exit_code}]"

    return result or "(no output)"


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="exec",
        description=(
            "Execute a shell command. Returns stdout, stderr, and exit code. "
            "Working directory is the daemon's startup directory — use absolute paths. "
            "Secret environment variables are filtered (LUCYD_* and any ending in "
            "_KEY, _TOKEN, _SECRET, _PASSWORD are removed). "
            "Output is truncated at 30000 characters."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default: 120, max: 600)"},
            },
            "required": ["command"],
        },
        function=tool_exec,
    ),
]
