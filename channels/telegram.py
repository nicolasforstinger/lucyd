"""Telegram channel via Bot API (long polling).

Inbound: getUpdates long polling (httpx async).
Outbound: Bot API HTTP calls (httpx async).
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from . import Attachment, InboundMessage

log = logging.getLogger(__name__)

# Telegram Bot API base URL
_API_BASE = "https://api.telegram.org/bot{token}"

# Allowed emoji for setMessageReaction (Bot API 8.2, Feb 2026).
# https://core.telegram.org/bots/api#reactiontypeemoji
ALLOWED_REACTIONS: set[str] = {
    "❤", "👍", "👎", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱",
    "🤬", "😢", "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊", "🤡",
    "🥱", "🥴", "😍", "🐳", "❤\u200d🔥", "🌚", "🌭", "💯", "🤣", "⚡",
    "🍌", "🏆", "💔", "🤨", "😐", "🍓", "🍾", "💋", "🖕", "😈",
    "😴", "😭", "🤓", "👻", "👨\u200d💻", "👀", "🎃", "🙈", "😇", "😨",
    "🤝", "✍", "🤗", "🫡", "🎅", "🎄", "☃", "💅", "🤪", "🗿",
    "🆒", "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾",
    "🤷\u200d♂", "🤷", "🤷\u200d♀", "😡",
}


_MD_LINK_RE = re.compile(r'\[([^\]]+)\]\((https?://[^\)]+)\)')


def _extract_markdown_links(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Extract markdown links, replace with footnote labels.

    Returns (cleaned_text, [(label, url), ...]).
    "[JP store](https://...)" → "JP store [1]"
    """
    links: list[tuple[str, str]] = []
    counter = 0

    def _replace(m: re.Match) -> str:
        nonlocal counter
        counter += 1
        links.append((m.group(1), m.group(2)))
        return f"{m.group(1)} [{counter}]"

    cleaned = _MD_LINK_RE.sub(_replace, text)
    return cleaned, links


def _build_inline_keyboard(links: list[tuple[str, str]]) -> dict | None:
    """Build InlineKeyboardMarkup with numbered URL buttons.

    Returns reply_markup dict or None if no links.
    Layout: up to 4 buttons per row.
    """
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


class TelegramChannel:
    # How long to wait for more items in a media group (seconds).
    _MEDIA_GROUP_DELAY = 0.5

    def __init__(self, config: dict):
        import os

        from config import ConfigError

        token_env = config.get("token_env", "")
        if not token_env:
            raise ConfigError("[channel.telegram] token_env is required")
        token = os.environ.get(token_env, "")
        if not token:
            raise ConfigError(
                f"Telegram token not found in env var: {token_env}"
            )

        self.token = token
        self.base_url = _API_BASE.format(token=token)
        allow_from = config.get("allow_from", [])
        self.allow_from = set(allow_from) if allow_from else set()
        self.chunk_limit = config.get("text_chunk_limit", 4000)
        dl_dir = config.get("download_dir", "/tmp/lucyd-telegram")  # noqa: S108 — config default
        self.download_dir = Path(dl_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self._reconnect_initial = float(config.get("reconnect_initial", 1.0))
        self._reconnect_max = float(config.get("reconnect_max", 10.0))
        self._reconnect_factor = float(config.get("reconnect_factor", 2.0))
        self._reconnect_jitter = float(config.get("reconnect_jitter", 0.2))

        # Name -> user_id for outbound resolution
        self._contacts: dict[str, int] = {}
        # Reverse: user_id -> name for inbound sender resolution
        self._id_to_name: dict[int, str] = {}
        contacts = config.get("contacts", {})
        if contacts:
            for name, user_id in contacts.items():
                self._contacts[name.lower()] = user_id
                self._id_to_name[user_id] = name
                log.debug("Contact: %s -> %d", name, user_id)

        self._bot_id: int = 0
        self._offset: int = 0  # getUpdates offset

        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
        return self._client

    async def _api(self, method: str, **params) -> dict:
        """Call Telegram Bot API method."""
        client = await self._get_client()
        url = f"{self.base_url}/{method}"

        # Separate files from regular params
        files = params.pop("_files", None)

        if files:
            resp = await client.post(url, data=params, files=files)
        else:
            resp = await client.post(url, json=params)

        # Parse JSON first — Telegram returns error descriptions even on 4xx.
        # Don't rely on raise_for_status() which discards the body.
        try:
            data = resp.json()
        except (ValueError, KeyError) as exc:
            resp.raise_for_status()
            raise RuntimeError(f"Telegram API error ({method}): non-JSON response {resp.status_code}") from exc

        if not data.get("ok"):
            desc = data.get("description", f"HTTP {resp.status_code}")
            raise RuntimeError(f"Telegram API error ({method}): {desc}")

        return data.get("result", {})

    async def disconnect(self) -> None:
        """Close httpx client and clean up downloaded files."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
        # Clean transient download files
        if self.download_dir.exists():
            for f in self.download_dir.iterdir():
                try:
                    if f.is_file():
                        f.unlink()
                except OSError:
                    pass

    async def connect(self) -> None:
        """Verify bot token and log identity."""
        try:
            me = await self._api("getMe")
            self._bot_id = me.get("id", 0)
            log.info("Telegram bot connected: @%s (id=%d)", me.get("username", ""), self._bot_id)
        except Exception as e:
            log.error("Cannot connect to Telegram Bot API: %s", e)
            raise ConnectionError(f"Telegram Bot API unreachable: {e}") from e

    async def receive(self) -> AsyncIterator[InboundMessage]:
        """Long-polling loop. Auto-reconnects on failure."""
        backoff = self._reconnect_initial
        while True:
            try:
                async for msg in self._poll_loop():
                    yield msg
                    backoff = self._reconnect_initial
            except asyncio.CancelledError:
                return
            except Exception as e:
                jitter = backoff * self._reconnect_jitter * (random.random() * 2 - 1)  # noqa: S311 — timing jitter, not cryptographic
                wait = backoff + jitter
                log.warning("Telegram poll disconnected (%s), reconnecting in %.1fs", e, wait)
                await asyncio.sleep(wait)
                backoff = min(backoff * self._reconnect_factor, self._reconnect_max)

    async def _poll_loop(self) -> AsyncIterator[InboundMessage]:
        """Single polling session — yields messages until error.

        Buffers media-group updates (albums sent as multiple Updates sharing
        the same ``media_group_id``) and merges them into a single
        ``InboundMessage`` with all attachments combined.
        """
        pending_groups: dict[str, list[dict[str, Any]]] = {}
        pending_since: dict[str, float] = {}

        while True:
            # Short-poll while we have groups waiting to be flushed.
            poll_timeout = 1 if pending_groups else 30

            updates = await self._api(
                "getUpdates",
                offset=self._offset,
                timeout=poll_timeout,
                allowed_updates=["message"],
            )

            for update in updates:
                update_id = update.get("update_id", 0)
                if update_id >= self._offset:
                    self._offset = update_id + 1

                message = update.get("message")
                if not message:
                    continue

                mg_id = message.get("media_group_id")
                if mg_id:
                    if mg_id not in pending_groups:
                        pending_groups[mg_id] = []
                        pending_since[mg_id] = time.monotonic()
                    pending_groups[mg_id].append(message)
                else:
                    parsed = await self._parse_message(message)
                    if parsed is not None:
                        yield parsed

            # Flush media groups that have waited long enough.
            now = time.monotonic()
            flush_ids = [
                gid for gid, since in pending_since.items()
                if now - since >= self._MEDIA_GROUP_DELAY
            ]
            for gid in flush_ids:
                messages = pending_groups.pop(gid)
                pending_since.pop(gid)
                merged = await self._merge_media_group(messages)
                if merged is not None:
                    yield merged

    @staticmethod
    def _extract_quote(message: dict) -> str | None:
        """Extract quoted/replied-to context from a Telegram message."""
        reply_msg = message.get("reply_to_message")
        if not reply_msg:
            return None

        # Prefer Telegram's quote selection (partial text highlight)
        tg_quote = message.get("quote")
        if tg_quote and tg_quote.get("text"):
            return tg_quote["text"]

        quote_text = reply_msg.get("text", "") or reply_msg.get("caption", "")
        if quote_text:
            return quote_text

        # Media fallback for non-text messages
        if reply_msg.get("voice"):
            return "[voice message]"
        if reply_msg.get("photo"):
            return "[photo]"
        if reply_msg.get("video"):
            return "[video]"
        if reply_msg.get("sticker"):
            emoji = reply_msg["sticker"].get("emoji", "")
            return f"[sticker {emoji}]" if emoji else "[sticker]"
        if reply_msg.get("document"):
            name = reply_msg["document"].get("file_name", "")
            return f"[document: {name}]" if name else "[document]"
        if reply_msg.get("audio"):
            return "[audio]"
        return None

    async def _parse_message(self, message: dict) -> InboundMessage | None:
        """Parse a Telegram message dict into InboundMessage, or None to skip."""
        from_user = message.get("from", {})
        user_id = from_user.get("id", 0)
        message_id = message.get("message_id", 0)

        # Skip messages from the bot itself
        if user_id == self._bot_id:
            return None

        # Filter by allow list
        if self.allow_from and user_id not in self.allow_from:
            log.debug("Ignoring message from non-allowed user: %d", user_id)
            return None

        # Resolve sender name
        sender = self._id_to_name.get(user_id, "")
        if not sender:
            sender = from_user.get("username") or from_user.get("first_name") or str(user_id)

        # Extract text
        text = message.get("text", "") or message.get("caption", "") or ""

        # Extract attachments
        attachments = await self._extract_attachments(message)

        if not text and not attachments:
            return None

        quote_text = self._extract_quote(message)

        # Store message_id as float in timestamp field.
        # Round-trip: daemon does int(ts * 1000) -> ts * 1000.
        # Channel recovers: message_id = ts // 1000.
        timestamp = float(message_id)

        return InboundMessage(
            text=text,
            sender=sender,
            timestamp=timestamp,
            source="telegram",
            quote=quote_text or None,
            attachments=attachments or None,
        )

    async def _merge_media_group(self, messages: list[dict[str, Any]]) -> InboundMessage | None:
        """Merge multiple Telegram messages from a media group into one InboundMessage.

        Takes metadata (sender, timestamp, quote) from the first message.
        Combines captions into one text block, attachments into one list.
        """
        if not messages:
            return None

        # Sort by message_id to preserve album order.
        messages.sort(key=lambda m: m.get("message_id", 0))

        first = messages[0]
        from_user = first.get("from", {})
        user_id = from_user.get("id", 0)

        # Apply the same access checks as _parse_message.
        if user_id == self._bot_id:
            return None
        if self.allow_from and user_id not in self.allow_from:
            return None

        sender = self._id_to_name.get(user_id, "")
        if not sender:
            sender = from_user.get("username") or from_user.get("first_name") or str(user_id)

        # Collect captions and attachments from all messages in the group.
        all_attachments: list[Attachment] = []
        captions: list[str] = []

        for msg in messages:
            caption = msg.get("caption", "")
            if caption:
                captions.append(caption)
            atts = await self._extract_attachments(msg)
            all_attachments.extend(atts)

        if not all_attachments and not captions:
            return None

        text = "\n".join(captions) if captions else ""

        # Quote context from the first message (albums reply as a unit).
        quote_text = self._extract_quote(first)

        # Use the first message_id as timestamp.
        message_id = first.get("message_id", 0)
        timestamp = float(message_id)

        return InboundMessage(
            text=text,
            sender=sender,
            timestamp=timestamp,
            source="telegram",
            quote=quote_text or None,
            attachments=all_attachments or None,
        )

    async def _extract_attachments(self, message: dict) -> list[Attachment]:
        """Download and return attachments from a Telegram message."""
        attachments = []

        # Photos — try largest resolution first, fall back to smaller variants
        photos = message.get("photo")
        if photos:
            att = None
            for variant in reversed(photos):
                att = await self._download_file(
                    variant.get("file_id", ""),
                    content_type="image/jpeg",
                    size=variant.get("file_size", 0),
                )
                if att:
                    break
            if att:
                attachments.append(att)

        # Voice messages
        voice = message.get("voice")
        if voice:
            att = await self._download_file(
                voice.get("file_id", ""),
                content_type=voice.get("mime_type", "audio/ogg"),
                size=voice.get("file_size", 0),
            )
            if att:
                att.is_voice = True
                attachments.append(att)

        # Documents — fall back to thumbnail for image documents
        doc = message.get("document")
        if doc:
            att = await self._download_file(
                doc.get("file_id", ""),
                content_type=doc.get("mime_type", "application/octet-stream"),
                filename=doc.get("file_name", ""),
                size=doc.get("file_size", 0),
            )
            if not att and doc.get("thumbnail"):
                mime = doc.get("mime_type", "")
                if mime.startswith("image/"):
                    thumb = doc["thumbnail"]
                    log.info("Full document too large, downloading thumbnail: %s",
                             doc.get("file_name", ""))
                    att = await self._download_file(
                        thumb.get("file_id", ""),
                        content_type="image/jpeg",
                        filename=doc.get("file_name", ""),
                        size=thumb.get("file_size", 0),
                    )
            if att:
                attachments.append(att)

        # Video
        video = message.get("video")
        if video:
            att = await self._download_file(
                video.get("file_id", ""),
                content_type=video.get("mime_type", "video/mp4"),
                size=video.get("file_size", 0),
            )
            if att:
                attachments.append(att)

        # Audio (music files, not voice)
        audio = message.get("audio")
        if audio:
            att = await self._download_file(
                audio.get("file_id", ""),
                content_type=audio.get("mime_type", "audio/mpeg"),
                filename=audio.get("file_name", ""),
                size=audio.get("file_size", 0),
            )
            if att:
                attachments.append(att)

        # Stickers
        sticker = message.get("sticker")
        if sticker:
            att = await self._download_file(
                sticker.get("file_id", ""),
                content_type="image/webp",
                size=sticker.get("file_size", 0),
            )
            if att:
                attachments.append(att)

        return attachments

    async def _download_file(
        self,
        file_id: str,
        content_type: str = "application/octet-stream",
        filename: str = "",
        size: int = 0,
    ) -> Attachment | None:
        """Download a file from Telegram servers to local disk."""
        if not file_id:
            return None

        try:
            file_info = await self._api("getFile", file_id=file_id)
            file_path = file_info.get("file_path", "")
            if not file_path:
                log.warning("No file_path returned for file_id: %s", file_id)
                return None

            download_url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"

            client = await self._get_client()
            resp = await client.get(download_url)
            resp.raise_for_status()

            # Determine local filename
            if not filename:
                filename = Path(file_path).name
            local_path = self.download_dir / f"{int(time.time() * 1000)}_{Path(filename).name}"

            local_path.write_bytes(resp.content)
            actual_size = size or len(resp.content)

            return Attachment(
                content_type=content_type,
                local_path=str(local_path),
                filename=Path(filename).name,
                size=actual_size,
            )
        except Exception as e:
            log.warning("Failed to download Telegram file %s: %s", file_id, e)
            return None

    def _resolve_target(self, target: str) -> int:
        """Resolve target name to Telegram chat_id.

        Resolution chain: contact name -> user_id.
        Includes self-send detection.
        """
        # Try contact name lookup (case-insensitive)
        if isinstance(target, str) and not target.lstrip("-").isdigit():
            chat_id = self._contacts.get(target.lower())
            if chat_id is None:
                contacts = ", ".join(c.title() for c in self._contacts) or "none configured"
                raise ValueError(
                    f"Unknown contact: {target!r}. Available contacts: {contacts}"
                )
        else:
            chat_id = int(target)

        # Block self-send
        if chat_id == self._bot_id:
            raise ValueError(
                f"Self-send blocked — target resolves to bot's own ID ({self._bot_id})."
            )

        return chat_id

    async def send(self, target: str, text: str, attachments: list[str] | None = None) -> None:
        """Send message via Telegram Bot API. Chunks long text; sends attachments."""
        chat_id = self._resolve_target(target)

        if attachments:
            # Send attachments
            text_sent = False
            for path in attachments:
                p = Path(path)
                if not p.exists():
                    log.warning("Attachment file not found: %s", path)
                    continue
                mime = _guess_mime(p)
                if mime.startswith("audio/") or p.suffix.lower() in (".ogg", ".mp3", ".m4a"):
                    await self._send_voice(chat_id, p)
                elif mime.startswith("image/"):
                    caption = text if text and not text_sent and not attachments[1:] else ""
                    await self._send_photo(chat_id, p, caption=caption)
                    if caption:
                        text_sent = True
                else:
                    await self._send_document(chat_id, p)
            # Send text separately if it wasn't consumed as a caption
            if text and not text_sent:
                cleaned, link_list = _extract_markdown_links(text)
                reply_markup = _build_inline_keyboard(link_list)
                chunks = self._chunk_text(cleaned)
                for i, chunk in enumerate(chunks):
                    params: dict = {"chat_id": chat_id, "text": chunk}
                    if reply_markup and i == len(chunks) - 1:
                        params["reply_markup"] = reply_markup
                    await self._api("sendMessage", **params)
        elif text:
            cleaned, link_list = _extract_markdown_links(text)
            reply_markup = _build_inline_keyboard(link_list)
            chunks = self._chunk_text(cleaned)
            for i, chunk in enumerate(chunks):
                params = {"chat_id": chat_id, "text": chunk}
                if reply_markup and i == len(chunks) - 1:
                    params["reply_markup"] = reply_markup
                await self._api("sendMessage", **params)

    async def _send_voice(self, chat_id: int, path: Path) -> None:
        """Send a voice/audio file."""
        with path.open("rb") as f:
            await self._api(
                "sendVoice",
                chat_id=chat_id,
                _files={"voice": (path.name, f, "audio/ogg")},
            )

    async def _send_photo(self, chat_id: int, path: Path, caption: str = "") -> None:
        """Send a photo."""
        params: dict = {"chat_id": chat_id}
        if caption:
            params["caption"] = caption
        with path.open("rb") as f:
            await self._api(
                "sendPhoto",
                **params,
                _files={"photo": (path.name, f, "image/jpeg")},
            )

    async def _send_document(self, chat_id: int, path: Path) -> None:
        """Send a document."""
        with path.open("rb") as f:
            await self._api(
                "sendDocument",
                chat_id=chat_id,
                _files={"document": (path.name, f, _guess_mime(path))},
            )

    async def send_typing(self, target: str) -> None:
        """Send typing indicator."""
        try:
            chat_id = self._resolve_target(target)
            await self._api("sendChatAction", chat_id=chat_id, action="typing")
        except Exception as e:
            log.debug("Typing indicator failed (non-critical): %s", e)

    async def send_reaction(self, target: str, emoji: str, ts: int) -> None:
        """Send reaction to a message.

        The ts parameter carries message_id * 1000 (from the daemon's
        int(timestamp * 1000) conversion). We recover message_id by // 1000.
        """
        if emoji not in ALLOWED_REACTIONS:
            raise ValueError(
                f"Emoji {emoji!r} is not a valid Telegram reaction. "
                f"Allowed: {' '.join(sorted(ALLOWED_REACTIONS))}"
            )
        chat_id = self._resolve_target(target)
        message_id = ts // 1000
        if message_id <= 0:
            raise ValueError(f"Invalid message_id for reaction: ts={ts}")
        await self._api(
            "setMessageReaction",
            chat_id=chat_id,
            message_id=message_id,
            reaction=[{"type": "emoji", "emoji": emoji}],
        )

    def _chunk_text(self, text: str) -> list[str]:
        """Split text on newline boundaries within chunk limit."""
        if len(text) <= self.chunk_limit:
            return [text]

        chunks = []
        current = ""
        for line in text.split("\n"):
            if current and len(current) + len(line) + 1 > self.chunk_limit:
                chunks.append(current)
                current = line
            else:
                current = current + "\n" + line if current else line

        if current:
            while len(current) > self.chunk_limit:
                chunks.append(current[:self.chunk_limit])
                current = current[self.chunk_limit:]
            if current:
                chunks.append(current)

        return chunks


def _guess_mime(path: Path) -> str:
    """Guess MIME type from file extension."""
    ext = path.suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".mp4": "video/mp4",
        ".ogg": "audio/ogg",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".pdf": "application/pdf",
        ".txt": "text/plain",
    }.get(ext, "application/octet-stream")
