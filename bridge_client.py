"""Channel-agnostic outbound delivery to the user's primary bridge.

Tools and HTTP endpoints call ``send_to_user`` to deliver a message to
whatever bridge is configured as primary. Each bridge implements the
same ``POST /send`` contract on a conventional localhost port; this
module is the single source of truth for that contract on the daemon
side.
"""
from __future__ import annotations

import logging
from typing import TypedDict

import httpx

log = logging.getLogger(__name__)


class OutboundAttachment(TypedDict):
    """Outbound attachment payload shape — matches what bridges accept."""
    filename: str
    content_type: str
    data_b64: str


# Bridge → conventional outbound port + max attachment size in bytes.
# Adding a bridge: add a row here, implement the /send contract on the
# bridge side, and configure [bridges] primary = "<name>" in lucyd.toml.
BRIDGE_LIMITS: dict[str, dict[str, int]] = {
    "telegram": {"port": 8101, "max_attachment_bytes": 52_428_800},  # 50 MB Bot API cap
    "email":    {"port": 8102, "max_attachment_bytes": 20_971_520},  # 20 MB Proton cap
}


class BridgeDeliveryError(Exception):
    """Raised when delivery to the primary bridge fails."""


async def send_to_user(
    text: str,
    attachments: list[OutboundAttachment],
    primary: str,
    token: str,
    http_client: httpx.AsyncClient,
) -> None:
    """Deliver text + attachments to the user's primary bridge.

    Raises ``BridgeDeliveryError`` if the primary is unconfigured,
    unknown, or the HTTP call fails.
    """
    if not primary:
        raise BridgeDeliveryError("no primary bridge configured")
    info = BRIDGE_LIMITS.get(primary)
    if info is None:
        raise BridgeDeliveryError(f"unknown bridge: {primary}")

    url = f"http://127.0.0.1:{info['port']}/send"
    payload = {"text": text, "attachments": attachments}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    try:
        response = await http_client.post(url, json=payload, headers=headers, timeout=15.0)
        response.raise_for_status()
    except (httpx.RequestError, httpx.HTTPStatusError) as e:
        raise BridgeDeliveryError(f"{primary} delivery failed: {e}") from e
