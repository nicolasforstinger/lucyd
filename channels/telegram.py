#!/usr/bin/env python3
"""Telegram bridge — standalone process that connects Telegram to Lucyd.

Polls Telegram getUpdates → POSTs to daemon HTTP API → sends reply back.

Run:  python3 channels/telegram.py
Config: [telegram] section in lucyd.toml (path from LUCYD_CONFIG env var).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import random
import re
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("bridge.telegram")

# ─── Config (defaults — overridden by load_config) ──────────────

TOKEN = ""
DAEMON_URL = "http://127.0.0.1:8100"
API_BASE = ""
CHUNK_LIMIT = 4000
POLL_TIMEOUT = 30
HTTP_TIMEOUT = 45.0
CONNECT_TIMEOUT = 10.0
RECONNECT_INITIAL = 1.0
RECONNECT_MAX = 10.0
RECONNECT_FACTOR = 2.0
RECONNECT_JITTER = 0.2
MEDIA_GROUP_DELAY = 0.5
MAX_ATTACHMENT_BYTES = 0  # 0 = no limit

# Contact resolution
ID_TO_NAME: dict[int, str] = {}    # user_id → name
ALLOW_FROM: set[int] = set()

# ─── Telegram API ────────────────────────────────────────────────

_client: httpx.AsyncClient | None = None
_bot_id: int = 0
_offset: int = 0


def _daemon_auth_headers() -> dict[str, str]:
    """Build auth headers for daemon HTTP API."""
    token = os.environ.get("LUCYD_HTTP_TOKEN", "")
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(HTTP_TIMEOUT, connect=CONNECT_TIMEOUT),
            headers=_daemon_auth_headers(),
        )
    return _client


async def _reset_client() -> None:
    """Close and recreate the httpx client on connection failure."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def tg_api(method: str, **params: Any) -> Any:
    """Call Telegram Bot API method."""
    client = await _get_client()
    files = params.pop("_files", None)
    if files:
        resp = await client.post(f"{API_BASE}/{method}", data=params, files=files)
    else:
        resp = await client.post(f"{API_BASE}/{method}", json=params)
    data = resp.json()
    if not data.get("ok"):
        desc = data.get("description", f"HTTP {resp.status_code}")
        raise RuntimeError(f"Telegram API error ({method}): {desc}")
    return data.get("result", {})


# ─── Text Processing ─────────────────────────────────────────────

_MD_LINK_RE = re.compile(r'\[([^\]]+)\]\((https?://[^\)]+)\)')


def extract_links(text: str) -> tuple[str, list[tuple[str, str]]]:
    links: list[tuple[str, str]] = []
    counter = 0

    def _replace(m: re.Match[str]) -> str:
        nonlocal counter
        counter += 1
        links.append((m.group(1), m.group(2)))
        return f"{m.group(1)} [{counter}]"

    return _MD_LINK_RE.sub(_replace, text), links


def build_keyboard(links: list[tuple[str, str]]) -> dict[str, Any] | None:
    if not links:
        return None
    rows: list[list[dict[str, str]]] = []
    row: list[dict[str, str]] = []
    for i, (_label, url) in enumerate(links, 1):
        row.append({"text": str(i), "url": url})
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return {"inline_keyboard": rows}


def chunk_text(text: str) -> list[str]:
    if len(text) <= CHUNK_LIMIT:
        return [text]
    chunks = []
    current = ""
    for line in text.split("\n"):
        if current and len(current) + len(line) + 1 > CHUNK_LIMIT:
            chunks.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    if current:
        while len(current) > CHUNK_LIMIT:
            chunks.append(current[:CHUNK_LIMIT])
            current = current[CHUNK_LIMIT:]
        if current:
            chunks.append(current)
    return chunks


# ─── Send Helpers ─────────────────────────────────────────────────


async def send_text(chat_id: int, text: str) -> None:
    cleaned, links = extract_links(text)
    keyboard = build_keyboard(links)
    chunks = chunk_text(cleaned)
    for i, chunk in enumerate(chunks):
        params: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
        if keyboard and i == len(chunks) - 1:
            params["reply_markup"] = keyboard
        await tg_api("sendMessage", **params)


async def send_attachment(chat_id: int, path: str, *, caption: str = "") -> None:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Attachment not found: {path}")
    ext = p.suffix.lower()
    params: dict[str, Any] = {"chat_id": chat_id}
    if caption:
        params["caption"] = caption
    if ext in (".ogg", ".mp3", ".m4a") or ext.startswith(".audio"):
        with p.open("rb") as f:
            await tg_api("sendVoice", **params, _files={"voice": (p.name, f, "audio/ogg")})
    elif ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
        with p.open("rb") as f:
            await tg_api("sendPhoto", **params, _files={"photo": (p.name, f, "image/jpeg")})
    else:
        with p.open("rb") as f:
            await tg_api("sendDocument", **params, _files={"document": (p.name, f, "application/octet-stream")})


async def _send_with_retry(send_fn: Callable[..., Awaitable[None]],
                           chat_id: int, payload: str, *,
                           retries: int = 2, delay: float = 1.0,
                           **kwargs: Any) -> None:
    """Call a Telegram send function with retry on transient failures.

    Raises the last exception if all attempts fail.
    """
    last_exc: Exception | None = None
    for attempt in range(1 + retries):
        try:
            await send_fn(chat_id, payload, **kwargs)
            return
        except httpx.ConnectError as e:
            last_exc = e
            await _reset_client()
            if attempt < retries:
                log.warning("Telegram send ConnectError (attempt %d/%d), client reset",
                            attempt + 1, 1 + retries)
                await asyncio.sleep(delay * (attempt + 1))
            else:
                log.error("Telegram send failed after %d attempts (ConnectError)",
                          1 + retries, exc_info=True)
        except Exception as e:
            last_exc = e
            if attempt < retries:
                log.warning("Telegram send failed (attempt %d/%d)",
                            attempt + 1, 1 + retries, exc_info=True)
                await asyncio.sleep(delay * (attempt + 1))
            else:
                log.error("Telegram send failed after %d attempts",
                          1 + retries, exc_info=True)
    assert last_exc is not None  # guaranteed: loop only exits here on failure
    raise last_exc


# ─── Inbound: Parse Messages ─────────────────────────────────────


def extract_quote(message: dict[str, Any]) -> str | None:
    reply = message.get("reply_to_message")
    if not reply:
        return None
    tg_quote = message.get("quote")
    if tg_quote and tg_quote.get("text"):
        return str(tg_quote["text"])
    return str(reply.get("text", "") or reply.get("caption", "")) or None


async def download_file(file_id: str, download_dir: Path) -> tuple[str, str, int] | None:
    """Download a Telegram file. Returns (local_path, content_type, size) or None."""
    try:
        info = await tg_api("getFile", file_id=file_id)
        file_path = info.get("file_path", "")
        if not file_path:
            return None
        size = info.get("file_size", 0)
        if MAX_ATTACHMENT_BYTES and size > MAX_ATTACHMENT_BYTES:
            return None
        url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
        client = await _get_client()
        resp = await client.get(url)
        resp.raise_for_status()
        if MAX_ATTACHMENT_BYTES and len(resp.content) > MAX_ATTACHMENT_BYTES:
            return None
        local = download_dir / f"{int(time.time() * 1000)}_{Path(file_path).name}"
        local.write_bytes(resp.content)
        # Guess content type from extension
        ext = Path(file_path).suffix.lower()
        ct_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                  ".gif": "image/gif", ".webp": "image/webp", ".ogg": "audio/ogg",
                  ".mp3": "audio/mpeg", ".mp4": "video/mp4", ".pdf": "application/pdf"}
        ct = ct_map.get(ext, "application/octet-stream")
        return str(local), ct, len(resp.content)
    except Exception as e:
        log.warning("Download failed for %s: %s", file_id, e, exc_info=True)
        return None


async def extract_attachments(message: dict[str, Any], download_dir: Path) -> list[dict[str, Any]]:
    """Extract and download attachments. Returns list of base64-encoded dicts for HTTP API."""
    attachments: list[dict[str, Any]] = []
    download_dir.mkdir(parents=True, exist_ok=True)

    from collections.abc import Callable
    Extractor = Callable[[dict[str, Any]], Any]
    media_types: list[tuple[str, Extractor, str | None]] = [
        ("photo", lambda m: m.get("photo", [])[-1] if m.get("photo") else None, "image/jpeg"),
        ("voice", lambda m: m.get("voice"), None),
        ("document", lambda m: m.get("document"), None),
        ("video", lambda m: m.get("video"), "video/mp4"),
        ("audio", lambda m: m.get("audio"), "audio/mpeg"),
        ("sticker", lambda m: m.get("sticker"), "image/webp"),
    ]

    for _name, extractor, default_ct in media_types:
        media = extractor(message)
        if not media:
            continue
        file_id = media.get("file_id", "")
        if not file_id:
            continue
        result = await download_file(file_id, download_dir)
        if result:
            local_path, ct, size = result
            ct = media.get("mime_type", ct or default_ct or "application/octet-stream")
            data = base64.b64encode(Path(local_path).read_bytes()).decode("ascii")
            att = {
                "content_type": ct,
                "filename": media.get("file_name", Path(local_path).name),
                "data": data,
            }
            if _name == "voice":
                att["is_voice"] = True
            attachments.append(att)
            # Clean up local file after encoding
            Path(local_path).unlink(missing_ok=True)

    return attachments


# ─── Inbound: Poll Loop ──────────────────────────────────────────


async def inbound_loop() -> None:
    """Poll Telegram → POST to daemon → deliver response."""
    global _bot_id, _offset

    # Verify token
    me = await tg_api("getMe")
    _bot_id = me.get("id", 0)
    log.info("Telegram bridge connected: @%s (id=%d)", me.get("username", ""), _bot_id)

    download_dir = Path(os.environ.get("LUCYD_TELEGRAM_DOWNLOAD_DIR", "/tmp/lucyd-telegram-bridge"))
    backoff = RECONNECT_INITIAL

    # Media group buffering
    pending_groups: dict[str, list[dict[str, Any]]] = {}
    pending_since: dict[str, float] = {}

    while True:
        try:
            poll_timeout = 1 if pending_groups else POLL_TIMEOUT
            updates = await tg_api("getUpdates", offset=_offset, timeout=poll_timeout,
                                   allowed_updates=["message"])

            for update in updates:
                uid = update.get("update_id", 0)
                if uid >= _offset:
                    _offset = uid + 1

                message = update.get("message")
                if not message:
                    continue

                mg_id = message.get("media_group_id")
                if mg_id:
                    pending_groups.setdefault(mg_id, []).append(message)
                    pending_since.setdefault(mg_id, time.monotonic())
                else:
                    await process_message(message, download_dir)

            # Flush media groups
            now = time.monotonic()
            for gid in [g for g, t in pending_since.items() if now - t >= MEDIA_GROUP_DELAY]:
                messages = pending_groups.pop(gid)
                pending_since.pop(gid)
                messages.sort(key=lambda m: m.get("message_id", 0))
                # Merge: combine captions + attachments from all messages
                combined_text = "\n".join(m.get("caption", "") for m in messages if m.get("caption"))
                first = messages[0]
                first_with_text = dict(first)
                if combined_text:
                    first_with_text["text"] = combined_text
                    first_with_text["caption"] = combined_text
                # Collect all attachments from all messages in group
                all_atts = []
                for m in messages:
                    all_atts.extend(await extract_attachments(m, download_dir))
                await process_message(first_with_text, download_dir, pre_attachments=all_atts)

            backoff = RECONNECT_INITIAL

        except asyncio.CancelledError:
            return
        except Exception as e:
            jitter = backoff * RECONNECT_JITTER * (random.random() * 2 - 1)  # noqa: S311
            wait = backoff + jitter
            log.warning("Poll error (%s), reconnecting in %.1fs", e, wait, exc_info=True)
            await asyncio.sleep(wait)
            backoff = min(backoff * RECONNECT_FACTOR, RECONNECT_MAX)


async def process_message(message: dict[str, Any], download_dir: Path,
                          pre_attachments: list[dict[str, Any]] | None = None) -> None:
    """Process a single Telegram message → POST to daemon → deliver reply."""
    from_user = message.get("from", {})
    user_id = from_user.get("id", 0)
    chat_id = message.get("chat", {}).get("id", user_id)

    # Filter
    if user_id == _bot_id:
        return
    if ALLOW_FROM and user_id not in ALLOW_FROM:
        return

    # Resolve sender
    sender = ID_TO_NAME.get(user_id, "")
    if not sender:
        sender = from_user.get("username") or from_user.get("first_name") or str(user_id)

    # Extract text
    text = message.get("text", "") or message.get("caption", "") or ""

    # Extract quote context
    quote = extract_quote(message)
    if quote:
        text = f"[replying to: {quote}]\n{text}"

    # Extract attachments
    attachments = pre_attachments or await extract_attachments(message, download_dir)

    if not text and not attachments:
        return

    # Send typing
    try:
        await tg_api("sendChatAction", chat_id=chat_id, action="typing")
    except Exception:
        pass

    # POST to daemon
    body: dict[str, Any] = {"message": text, "sender": sender, "channel_id": "telegram"}
    if attachments:
        body["attachments"] = attachments

    # POST to daemon
    try:
        client = await _get_client()
        resp = await client.post(f"{DAEMON_URL}/api/v1/chat", json=body, timeout=300)
    except Exception as e:
        log.error("Daemon POST failed for %s: %s", sender, e, exc_info=True)
        return

    if resp.status_code != 200:
        log.error("Daemon returned %d for %s: %s", resp.status_code, sender,
                  resp.text[:200])
        return

    data = resp.json()
    reply = data.get("reply", "")

    # Deliver to Telegram (with retry + client reset on ConnectError)
    failures: list[str] = []
    outbound_atts = data.get("attachments", [])

    # When there's text + exactly one attachment, send as caption to avoid
    # two separate notifications.  Caption is used up by the attachment.
    caption_text = ""
    if reply and len(outbound_atts) == 1:
        caption_text = reply
        reply = ""  # consumed — don't send as separate message

    if reply:
        try:
            await _send_with_retry(send_text, chat_id, reply)
        except Exception as e:
            log.error("Text delivery failed for %s: %s", sender, e)
            failures.append(f"text: {e}")

    for att in outbound_atts:
        local_path: str | None = None
        try:
            local_path = _decode_outbound_attachment(att, download_dir)
            await _send_with_retry(send_attachment, chat_id, local_path,
                                   caption=caption_text)
            caption_text = ""  # only on first attachment
        except Exception as e:
            fname = att.get("filename", "?") if isinstance(att, dict) else att
            log.error("Attachment delivery failed for %s (%s): %s", sender, fname, e)
            failures.append(f"attachment {fname}: {e}")
        finally:
            # Clean up temp file after send (success or failure)
            if local_path:
                Path(local_path).unlink(missing_ok=True)

    if failures:
        await _notify_delivery_failure(sender, failures)


def _decode_outbound_attachment(att: dict[str, str], download_dir: Path) -> str:
    """Decode a base64-encoded outbound attachment to a local temp file.

    Returns the local file path.
    """
    data_b64 = att.get("data", "")
    if not data_b64:
        raise ValueError("Attachment missing 'data' field")
    data = base64.b64decode(data_b64)
    filename = att.get("filename", "attachment")
    ts = int(time.time() * 1000)
    local_path = download_dir / f"{ts}_{filename}"
    download_dir.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(data)
    return str(local_path)


async def _notify_delivery_failure(sender: str, failures: list[str]) -> None:
    """POST delivery failure to daemon's /notify endpoint.

    The daemon routes /notify messages to the primary session so the agent
    learns about the failure on the next turn.
    """
    detail = "; ".join(failures)
    message = f"Delivery to {sender} failed: {detail}"
    try:
        client = await _get_client()
        await client.post(
            f"{DAEMON_URL}/api/v1/notify",
            json={"message": message, "source": "telegram"},
            timeout=10,
        )
    except Exception as e:
        log.error("Failed to notify daemon of delivery failure: %s", e)


# ─── Config Loading ──────────────────────────────────────────────


def load_config() -> None:
    """Load bridge config from [telegram] section in lucyd.toml."""
    global TOKEN, DAEMON_URL, API_BASE, CHUNK_LIMIT, POLL_TIMEOUT
    global HTTP_TIMEOUT, CONNECT_TIMEOUT, RECONNECT_INITIAL, RECONNECT_MAX
    global RECONNECT_FACTOR, RECONNECT_JITTER, MEDIA_GROUP_DELAY
    global MAX_ATTACHMENT_BYTES, ID_TO_NAME, ALLOW_FROM

    import tomllib

    config_path = os.environ.get("LUCYD_CONFIG", "")
    if not config_path:
        sys.exit("LUCYD_CONFIG environment variable is not set.")

    path = Path(config_path)
    if not path.exists():
        sys.exit(f"Config file not found: {config_path}")

    with path.open("rb") as f:
        data = tomllib.load(f)

    tg = data.get("telegram")
    if not tg:
        sys.exit(f"No [telegram] section in {config_path}")

    token_env = tg.get("token_env", "LUCYD_TELEGRAM_TOKEN")
    TOKEN = os.environ.get(token_env, "")
    API_BASE = f"https://api.telegram.org/bot{TOKEN}"

    allow = tg.get("allow_from", [])
    if allow:
        ALLOW_FROM = set(allow)

    CHUNK_LIMIT = tg.get("text_chunk_limit", CHUNK_LIMIT)
    POLL_TIMEOUT = tg.get("poll_timeout", POLL_TIMEOUT)
    HTTP_TIMEOUT = tg.get("http_timeout", HTTP_TIMEOUT)
    CONNECT_TIMEOUT = tg.get("http_connect_timeout", CONNECT_TIMEOUT)
    RECONNECT_INITIAL = tg.get("reconnect_initial", RECONNECT_INITIAL)
    RECONNECT_MAX = tg.get("reconnect_max", RECONNECT_MAX)
    RECONNECT_FACTOR = tg.get("reconnect_factor", RECONNECT_FACTOR)
    RECONNECT_JITTER = tg.get("reconnect_jitter", RECONNECT_JITTER)
    MEDIA_GROUP_DELAY = tg.get("media_group_delay", MEDIA_GROUP_DELAY)
    MAX_ATTACHMENT_BYTES = tg.get("max_attachment_bytes", MAX_ATTACHMENT_BYTES)

    # [telegram.contacts] section
    contacts = tg.get("contacts", {})
    for name, uid in contacts.items():
        ID_TO_NAME[uid] = name

    log.info("Loaded telegram config from %s (%d contacts)", config_path, len(contacts))


# ─── Main ─────────────────────────────────────────────────────────


async def main() -> None:
    load_config()

    if not TOKEN:
        sys.exit("LUCYD_TELEGRAM_TOKEN not set")

    await inbound_loop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(main())
