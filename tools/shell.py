"""Shell execution tool — exec."""

from __future__ import annotations

import asyncio
import os
import signal

_DEFAULT_TIMEOUT = 120
_MAX_TIMEOUT = 600

# Environment variable patterns to filter out of child processes
_SECRET_PREFIXES = ("LUCYD_",)
_SECRET_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_CREDENTIALS", "_ID", "_CODE", "_PASS")


def configure(default_timeout: int = 120, max_timeout: int = 600) -> None:
    global _DEFAULT_TIMEOUT, _MAX_TIMEOUT
    _DEFAULT_TIMEOUT = default_timeout
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
    """Execute a shell command and return stdout + stderr."""
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
            proc.communicate(), timeout=timeout
        )
    except TimeoutError:
        try:
            # Kill entire process group to prevent orphans
            os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
        except Exception:
            try:
                proc.kill()
                await proc.wait()
            except Exception:  # noqa: S110 — last-resort kill after timeout; nothing more to do
                pass
        return f"Error: Command timed out after {timeout}s"
    except Exception:
        return "Error: Command execution failed"

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


TOOLS = [
    {
        "name": "exec",
        "description": "Execute a shell command. Returns stdout, stderr, and exit code.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default: 120, max: 600)"},
            },
            "required": ["command"],
        },
        "function": tool_exec,
    },
]
