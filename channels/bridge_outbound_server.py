"""Shared aiohttp app builder for the bridge ``POST /send`` outbound contract.

Every channel bridge (telegram, email, future whatsapp) starts an
aiohttp listener on its conventional localhost port. The listener
exposes one route — ``POST /send`` — that the daemon calls to
deliver outbound messages.

Bridges customize delivery by passing in their own ``send_text`` /
``send_attachment`` async callables.
"""
from __future__ import annotations

import base64
import hmac
import logging
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

from aiohttp import web

log = logging.getLogger(__name__)

# The recipient the bridge addresses: a telegram chat_id (int) or an email
# address (str). Opaque to this module — passed straight to the send callables.
_Recipient = TypeVar("_Recipient", int, str)


def build_outbound_app(
    *,
    token: str,
    recipient: _Recipient,
    send_text: Callable[[_Recipient, str], Awaitable[None]],
    send_attachment: Callable[..., Awaitable[None]],  # (recipient, path, *, caption="") -> None
    max_attachment_bytes: int,
) -> web.Application:
    """Build the aiohttp app implementing POST /send for a bridge.

    ``token`` is the bearer credential the daemon must present
    (typically LUCYD_HTTP_TOKEN). ``recipient`` is the user's channel id
    or address derived from config — single-tenant single-user means it's
    static.

    ``max_attachment_bytes`` is the bridge's published attachment cap
    (from ``bridge_client.BRIDGE_LIMITS``). The aiohttp app's
    ``client_max_size`` is set to that cap × 4/3 (base64 expansion)
    plus 64 KB JSON-envelope headroom, so a request that fits the
    bridge's advertised limit doesn't get 413'd by aiohttp's 1 MB
    default body cap.
    """

    async def _handle_send(request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization", "")
        expected = f"Bearer {token}"
        if not token or not hmac.compare_digest(auth, expected):
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            body = await request.json()
        except ValueError:
            return web.json_response({"error": "invalid json"}, status=400)

        text = (body.get("text") or "").strip()
        attachments = body.get("attachments") or []
        if not text and not attachments:
            return web.json_response(
                {"error": "text or attachments required"}, status=400,
            )

        # Decode attachments to tempfiles and dispatch.
        tmpfiles: list[Path] = []
        try:
            for att in attachments:
                filename = att.get("filename") or "attachment"
                data_b64 = att.get("data_b64") or ""
                if not data_b64:
                    continue
                try:
                    data = base64.b64decode(data_b64, validate=True)
                except (ValueError, TypeError):
                    return web.json_response(
                        {"error": f"invalid base64 in attachment {filename}"},
                        status=400,
                    )
                with tempfile.NamedTemporaryFile(
                    delete=False, prefix="lucyd-outbound-",
                    suffix=f"-{Path(filename).name}",
                ) as f:
                    f.write(data)
                    tmpfiles.append(Path(f.name))

            if attachments and tmpfiles:
                # Use the text as the caption on the first attachment;
                # additional attachments get empty captions to avoid duplication.
                for i, p in enumerate(tmpfiles):
                    caption = text if i == 0 else ""
                    await send_attachment(recipient, str(p), caption=caption)
            elif text:
                await send_text(recipient, text)

            return web.json_response({"delivered": True}, status=200)
        finally:
            for p in tmpfiles:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    log.warning("Failed to delete tempfile %s", p)

    client_max_size = (max_attachment_bytes * 4 // 3) + 65_536
    app = web.Application(client_max_size=client_max_size)
    app.router.add_post("/send", _handle_send)
    return app
