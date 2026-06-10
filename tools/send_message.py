"""send_message — proactive outbound to the user's primary bridge.

Gated to ``talker == "agent"`` via ``ToolSpec.talkers`` so it never
appears in user/operator/system turns. Used during agent:self turns
(typically following ``schedule_self_task``) to push a result message
to the user.

After successful bridge delivery, the tool also appends the outbound
as an AssistantMessage to the user's session via
``SessionManager.append_outbound_to_user`` so the user's follow-up
reply has full context. The cross-session lock is acquired via the
pipeline's ``get_session_lock`` to serialize against any in-flight
user turn.
"""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import bridge_client
from bridge_client import BRIDGE_LIMITS, BridgeDeliveryError, OutboundAttachment
from . import ToolSpec

if TYPE_CHECKING:
    import asyncio

    import httpx

    from session import SessionManager

log = logging.getLogger(__name__)


# DI-injected at daemon startup via configure(). Module-level state
# matches the existing tool DI convention (see tools/reminder.py).
_bridges_primary: str = ""
_http_auth_token: str = ""
_user_session_key: str = ""
_allowed_paths: list[str] = []
_http_client: httpx.AsyncClient | None = None
_session_mgr: SessionManager | None = None
_pipeline_lock_factory: Callable[[str], asyncio.Lock] | None = None


def configure(
    *,
    bridges_primary: str = "",
    http_auth_token: str = "",
    user_session_key: str = "",
    allowed_paths: list[str] | None = None,
    http_client: httpx.AsyncClient | None = None,
    session_mgr: SessionManager | None = None,
    pipeline_lock_factory: Callable[[str], asyncio.Lock] | None = None,
    **_: object,
) -> None:
    """Wire dependencies. Called once at daemon startup."""
    global _bridges_primary, _http_auth_token, _user_session_key, _allowed_paths
    global _http_client, _session_mgr, _pipeline_lock_factory
    _bridges_primary = bridges_primary
    _http_auth_token = http_auth_token
    _user_session_key = user_session_key
    _allowed_paths = allowed_paths or []
    _http_client = http_client
    _session_mgr = session_mgr
    _pipeline_lock_factory = pipeline_lock_factory


def _check_path(path: str) -> str | None:
    """Return error string if path is outside allowed_paths, else None."""
    p = Path(path).resolve()
    for allowed in _allowed_paths:
        try:
            p.relative_to(Path(allowed).resolve())
            return None
        except ValueError:
            continue
    return f"Error: Permission denied (path not in allowed_paths): {path}"


def _bytes_for(byte_value: int) -> str:
    """Render '52428800' as '50 MB' for human-readable tool errors."""
    return f"{byte_value // (1024 * 1024)} MB"


async def tool_send_message(
    text: str = "",
    attachments: list[str] | str | None = None,
) -> dict[str, Any]:
    """Send a proactive message to the user via the primary bridge."""
    text = (text or "").strip()

    # Coerce attachments: schema declares array<string>, but models occasionally
    # emit a JSON-encoded array string (e.g. '["/tmp/x.mp3"]'). Parse that
    # specific shape so a recurrent malformed-call doesn't dead-end into a
    # confusing path-allowlist rejection on the literal '[' character.
    paths: list[str]
    if attachments is None:
        paths = []
    elif isinstance(attachments, list):
        paths = list(attachments)
    elif isinstance(attachments, str):
        try:
            decoded = json.loads(attachments)
        except json.JSONDecodeError:
            return {
                "text": (
                    "Error: attachments must be an array of file paths, not a string. "
                    'Pass [\"/path/to/file\"], not \"[\\\"/path/to/file\\\"]\".'
                ),
                "attachments": [],
            }
        if not isinstance(decoded, list) or not all(isinstance(p, str) for p in decoded):
            return {
                "text": (
                    "Error: attachments must be an array of file paths (strings)."
                ),
                "attachments": [],
            }
        paths = decoded
    else:
        return {
            "text": (
                "Error: attachments must be an array of file paths (strings)."
            ),
            "attachments": [],
        }

    if not text and not paths:
        return {"text": "Error: text or attachments required", "attachments": []}

    if not _bridges_primary:
        return {"text": "Error: no primary bridge configured", "attachments": []}

    if _http_client is None:
        return {"text": "Error: outbound http client not configured", "attachments": []}

    info = BRIDGE_LIMITS.get(_bridges_primary)
    if info is None:
        return {
            "text": f"Error: unknown primary bridge '{_bridges_primary}'",
            "attachments": [],
        }
    cap = info["max_attachment_bytes"]

    # Validate paths and sizes BEFORE encoding.
    for path in paths:
        err = _check_path(path)
        if err:
            return {"text": err, "attachments": []}
        p = Path(path)
        if not p.exists() or not p.is_file():
            return {"text": f"Error: file not found: {path}", "attachments": []}
        size = p.stat().st_size
        if size > cap:
            return {
                "text": (
                    f"Error: attachment {path} is {_bytes_for(size)}; "
                    f"{_bridges_primary} limit is {_bytes_for(cap)}. "
                    f"Move the file to /mnt/share/ (NAS, in allowed_paths) and "
                    f"call send_message again with the path quoted in your text. "
                    f"Future: VPN/Nextcloud share links once configured."
                ),
                "attachments": [],
            }

    # Build outbound payloads.
    outbound: list[OutboundAttachment] = []
    refs: list[dict[str, Any]] = []
    for path in paths:
        p = Path(path)
        ct, _enc = mimetypes.guess_type(p.name)
        ct = ct or "application/octet-stream"
        data = p.read_bytes()
        outbound.append({
            "filename": p.name,
            "content_type": ct,
            "data_b64": base64.b64encode(data).decode("ascii"),
        })
        refs.append({
            "filename": p.name,
            "size": len(data),
            "content_type": ct,
            "path": str(p),
        })

    # Deliver to bridge.
    try:
        await bridge_client.send_to_user(
            text=text,
            attachments=outbound,
            primary=_bridges_primary,
            token=_http_auth_token,
            http_client=_http_client,
        )
    except BridgeDeliveryError as e:
        log.warning("send_message bridge delivery failed: %s", e)
        return {"text": f"Error: bridge delivery failed: {e}", "attachments": []}

    # Append to user session for continuity. Hold the user session's
    # lock so we serialize against any in-flight user turn.
    if _session_mgr and _pipeline_lock_factory and _user_session_key:
        meta = {
            "from": "agent_self_send_message",
            "fired_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        try:
            async with _pipeline_lock_factory(_user_session_key):
                await _session_mgr.append_outbound_to_user(
                    target_key=_user_session_key,
                    text=text,
                    attachment_refs=refs,
                    source_metadata=meta,
                )
        except Exception as e:
            log.warning("send_message session append failed (delivery succeeded): %s", e)
            return {
                "text": (
                    f"Sent ({len(refs)} attachment(s)) but failed to append to "
                    f"user session for follow-up continuity: {e}. "
                    f"Consider memory_write to record this outbound."
                ),
                "attachments": [],
            }

    return {
        "text": f"Sent: {len(refs)} attachment(s), {len(text)} chars of text",
        "attachments": [],
    }


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="send_message",
        description=(
            "Send a proactive message (text and optional file attachments) to the "
            "user via their channel. Available ONLY when you've been triggered "
            "without a user waiting — typically during a scheduled self-task. "
            "NOT for replies to user messages; your normal response handles those "
            "automatically. If an attachment exceeds the bridge's size cap, the "
            "tool will fail with the cap and direct you to move the file to "
            "/mnt/share/ (NAS) and link to it in your text instead."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Message text"},
                "attachments": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "description": "Absolute file path within allowed_paths",
                    },
                    "default": [],
                },
            },
            "required": ["text"],
        },
        function=tool_send_message,
        talkers=frozenset({"agent"}),
    ),
]
