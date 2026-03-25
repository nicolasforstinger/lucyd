#!/usr/bin/env python3
"""Telegram bridge — standalone process that connects Telegram to Lucyd.

Inbound:  Polls Telegram getUpdates → POSTs to daemon HTTP API
Outbound: Accepts delivery requests from daemon relay → sends via Telegram

Run:  python3 channels/telegram.py
Env:  LUCYD_TELEGRAM_TOKEN (required)
      LUCYD_URL            (default: http://127.0.0.1:8100)
      LUCYD_BRIDGE_PORT    (default: 8101)
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
from pathlib import Path

import httpx

try:
    from aiohttp import web
except ImportError:
    sys.exit("aiohttp required: pip install aiohttp")

log = logging.getLogger("bridge.telegram")

# ─── Config ──────────────────────────────────────────────────────

TOKEN = os.environ.get("LUCYD_TELEGRAM_TOKEN", "")
DAEMON_URL = os.environ.get("LUCYD_URL", "http://127.0.0.1:8100")
BRIDGE_PORT = int(os.environ.get("LUCYD_BRIDGE_PORT", "8101"))

# Telegram API
API_BASE = f"https://api.telegram.org/bot{TOKEN}"
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
CONTACTS: dict[str, int] = {}      # name → user_id
ID_TO_NAME: dict[int, str] = {}    # user_id → name
ALLOW_FROM: set[int] = set()

# ─── Telegram API ────────────────────────────────────────────────

_client: httpx.AsyncClient | None = None
_bot_id: int = 0
_offset: int = 0


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(HTTP_TIMEOUT, connect=CONNECT_TIMEOUT))
    return _client


async def tg_api(method: str, **params) -> dict:
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

    def _replace(m: re.Match) -> str:
        nonlocal counter
        counter += 1
        links.append((m.group(1), m.group(2)))
        return f"{m.group(1)} [{counter}]"

    return _MD_LINK_RE.sub(_replace, text), links


def build_keyboard(links: list[tuple[str, str]]) -> dict | None:
    if not links:
        return None
    rows: list[list[dict]] = []
    row: list[dict] = []
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


def resolve_target(target: str) -> int:
    if isinstance(target, str) and not target.lstrip("-").isdigit():
        chat_id = CONTACTS.get(target.lower())
        if chat_id is None:
            raise ValueError(f"Unknown contact: {target!r}")
        return chat_id
    return int(target)


async def send_text(chat_id: int, text: str) -> None:
    cleaned, links = extract_links(text)
    keyboard = build_keyboard(links)
    chunks = chunk_text(cleaned)
    for i, chunk in enumerate(chunks):
        params: dict = {"chat_id": chat_id, "text": chunk}
        if keyboard and i == len(chunks) - 1:
            params["reply_markup"] = keyboard
        await tg_api("sendMessage", **params)


async def send_attachment(chat_id: int, path: str) -> None:
    p = Path(path)
    if not p.exists():
        log.warning("Attachment not found: %s", path)
        return
    ext = p.suffix.lower()
    if ext in (".ogg", ".mp3", ".m4a") or ext.startswith(".audio"):
        with p.open("rb") as f:
            await tg_api("sendVoice", chat_id=chat_id, _files={"voice": (p.name, f, "audio/ogg")})
    elif ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
        with p.open("rb") as f:
            await tg_api("sendPhoto", chat_id=chat_id, _files={"photo": (p.name, f, "image/jpeg")})
    else:
        with p.open("rb") as f:
            await tg_api("sendDocument", chat_id=chat_id, _files={"document": (p.name, f, "application/octet-stream")})


# ─── Inbound: Parse Messages ─────────────────────────────────────


def extract_quote(message: dict) -> str | None:
    reply = message.get("reply_to_message")
    if not reply:
        return None
    tg_quote = message.get("quote")
    if tg_quote and tg_quote.get("text"):
        return tg_quote["text"]
    return reply.get("text", "") or reply.get("caption", "") or None


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
        log.warning("Download failed for %s: %s", file_id, e)
        return None


async def extract_attachments(message: dict, download_dir: Path) -> list[dict]:
    """Extract and download attachments. Returns list of base64-encoded dicts for HTTP API."""
    attachments = []
    download_dir.mkdir(parents=True, exist_ok=True)

    media_types = [
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


async def inbound_loop():
    """Poll Telegram → POST to daemon → deliver response."""
    global _bot_id, _offset

    # Verify token
    me = await tg_api("getMe")
    _bot_id = me.get("id", 0)
    log.info("Telegram bridge connected: @%s (id=%d)", me.get("username", ""), _bot_id)

    download_dir = Path(os.environ.get("LUCYD_TELEGRAM_DOWNLOAD_DIR", "/tmp/lucyd-telegram-bridge"))
    backoff = RECONNECT_INITIAL

    # Media group buffering
    pending_groups: dict[str, list[dict]] = {}
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
            log.warning("Poll error (%s), reconnecting in %.1fs", e, wait)
            await asyncio.sleep(wait)
            backoff = min(backoff * RECONNECT_FACTOR, RECONNECT_MAX)


async def process_message(message: dict, download_dir: Path,
                          pre_attachments: list[dict] | None = None) -> None:
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
    body: dict = {"message": text, "sender": sender}
    if attachments:
        body["attachments"] = attachments

    try:
        client = await _get_client()
        resp = await client.post(f"{DAEMON_URL}/api/v1/chat", json=body, timeout=300)
        data = resp.json()
        reply = data.get("reply", "")
        if reply:
            await send_text(chat_id, reply)
        for att_path in data.get("attachments", []):
            await send_attachment(chat_id, att_path)
    except Exception as e:
        log.error("Daemon request failed: %s", e)


# ─── Delivery Server (outbound from daemon) ──────────────────────


async def handle_send(request: web.Request) -> web.Response:
    body = await request.json()
    chat_id = resolve_target(body["target"])
    text = body.get("text", "")
    attachments = body.get("attachments") or []
    if text:
        await send_text(chat_id, text)
    for att_path in attachments:
        if isinstance(att_path, str):
            await send_attachment(chat_id, att_path)
    return web.json_response({"ok": True})


async def handle_typing(request: web.Request) -> web.Response:
    body = await request.json()
    chat_id = resolve_target(body["target"])
    await tg_api("sendChatAction", chat_id=chat_id, action="typing")
    return web.json_response({"ok": True})


_stream_state: dict[int, dict] = {}


async def handle_stream(request: web.Request) -> web.Response:
    body = await request.json()
    chat_id = resolve_target(body["target"])
    text = body.get("text", "")
    done = body.get("done", False)

    state = _stream_state.get(chat_id)

    if text and state is None:
        try:
            resp = await tg_api("sendMessage", chat_id=chat_id, text=text)
            _stream_state[chat_id] = {
                "message_id": resp.get("message_id"),
                "text": text,
                "edit_count": 0,
            }
        except Exception:
            pass
    elif text and state is not None:
        state["text"] += text
        state["edit_count"] += 1
        if state["edit_count"] % 10 == 0 or done:
            msg_id = state.get("message_id")
            if msg_id:
                try:
                    await tg_api("editMessageText", chat_id=chat_id,
                                 message_id=msg_id, text=state["text"][:4096])
                except Exception:
                    pass

    if done:
        if state and state.get("message_id"):
            try:
                await tg_api("editMessageText", chat_id=chat_id,
                             message_id=state["message_id"], text=state["text"][:4096])
            except Exception:
                pass
        _stream_state.pop(chat_id, None)

    return web.json_response({"ok": True})


async def delivery_server():
    """HTTP server accepting outbound requests from daemon relay."""
    app = web.Application()
    app.router.add_post("/send", handle_send)
    app.router.add_post("/typing", handle_typing)
    app.router.add_post("/stream", handle_stream)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", BRIDGE_PORT)
    await site.start()
    log.info("Delivery server listening on 127.0.0.1:%d", BRIDGE_PORT)

    # Keep running
    await asyncio.Event().wait()


# ─── Config Loading ──────────────────────────────────────────────


def load_config():
    """Load contacts and settings from lucyd.toml if available."""
    global CONTACTS, ID_TO_NAME, ALLOW_FROM

    config_path = os.environ.get("LUCYD_CONFIG", "")
    if not config_path:
        # Try common locations
        for p in ["lucyd.toml", "/config/lucyd.toml"]:
            if Path(p).exists():
                config_path = p
                break

    if not config_path or not Path(config_path).exists():
        return

    try:
        import tomllib
        with open(config_path, "rb") as f:
            data = tomllib.load(f)

        tg = data.get("channel", {}).get("telegram", {})
        allow = tg.get("allow_from", [])
        if allow:
            ALLOW_FROM = set(allow)
        contacts = tg.get("contacts", {})
        for name, uid in contacts.items():
            CONTACTS[name.lower()] = uid
            ID_TO_NAME[uid] = name
        log.info("Loaded %d contacts from config", len(CONTACTS))
    except Exception as e:
        log.warning("Failed to load config: %s", e)


# ─── Main ─────────────────────────────────────────────────────────


async def main():
    if not TOKEN:
        sys.exit("LUCYD_TELEGRAM_TOKEN not set")

    load_config()

    await asyncio.gather(
        inbound_loop(),
        delivery_server(),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(main())
