"""Telegram channel via Bot API (long polling).

Inbound: getUpdates long polling (httpx async).
Outbound: Bot API HTTP calls (httpx async).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx

from . import Attachment, InboundMessage

log = logging.getLogger(__name__)

# Reconnect policy: 1s initial -> 10s max, factor 2, 20% jitter
_RECONNECT_INITIAL = 1.0
_RECONNECT_MAX = 10.0
_RECONNECT_FACTOR = 2.0
_RECONNECT_JITTER = 0.2

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


class TelegramChannel:
    def __init__(
        self,
        token: str,
        allow_from: list[int] | None = None,
        chunk_limit: int = 4000,
        contacts: dict[str, int] | None = None,
        download_dir: str = "/tmp/lucyd-telegram",  # noqa: S108 — config default; overridden by lucyd.toml
    ):
        self.token = token
        self.base_url = _API_BASE.format(token=token)
        self.allow_from = set(allow_from) if allow_from else set()
        self.chunk_limit = chunk_limit
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)

        # Name -> user_id for outbound resolution
        self._contacts: dict[str, int] = {}
        # Reverse: user_id -> name for inbound sender resolution
        self._id_to_name: dict[int, str] = {}
        if contacts:
            for name, user_id in contacts.items():
                self._contacts[name.lower()] = user_id
                self._id_to_name[user_id] = name
                log.debug("Contact: %s -> %d", name, user_id)

        self._bot_id: int = 0
        self._bot_username: str = ""
        self._offset: int = 0  # getUpdates offset
        # Last message_id per chat for reaction support
        self._last_message_ids: dict[int, int] = {}

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

    async def connect(self) -> None:
        """Verify bot token and log identity."""
        try:
            me = await self._api("getMe")
            self._bot_id = me.get("id", 0)
            self._bot_username = me.get("username", "")
            log.info("Telegram bot connected: @%s (id=%d)", self._bot_username, self._bot_id)
        except Exception as e:
            log.error("Cannot connect to Telegram Bot API: %s", e)
            raise ConnectionError(f"Telegram Bot API unreachable: {e}") from e

    async def receive(self) -> AsyncIterator[InboundMessage]:
        """Long-polling loop. Auto-reconnects on failure."""
        backoff = _RECONNECT_INITIAL
        while True:
            try:
                async for msg in self._poll_loop():
                    yield msg
                    backoff = _RECONNECT_INITIAL
            except asyncio.CancelledError:
                return
            except Exception as e:
                jitter = backoff * _RECONNECT_JITTER * (random.random() * 2 - 1)  # noqa: S311 — timing jitter, not cryptographic
                wait = backoff + jitter
                log.warning("Telegram poll disconnected (%s), reconnecting in %.1fs", e, wait)
                await asyncio.sleep(wait)
                backoff = min(backoff * _RECONNECT_FACTOR, _RECONNECT_MAX)

    async def _poll_loop(self) -> AsyncIterator[InboundMessage]:
        """Single polling session — yields messages until error."""
        while True:
            updates = await self._api(
                "getUpdates",
                offset=self._offset,
                timeout=30,
                allowed_updates=["message"],
            )

            for update in updates:
                update_id = update.get("update_id", 0)
                if update_id >= self._offset:
                    self._offset = update_id + 1

                message = update.get("message")
                if not message:
                    continue

                parsed = await self._parse_message(message)
                if parsed is not None:
                    yield parsed

    async def _parse_message(self, message: dict) -> InboundMessage | None:
        """Parse a Telegram message dict into InboundMessage, or None to skip."""
        from_user = message.get("from", {})
        user_id = from_user.get("id", 0)
        chat_id = message.get("chat", {}).get("id", 0)
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

        # Track message_id for reactions
        self._last_message_ids[chat_id] = message_id

        # Extract text
        text = message.get("text", "") or message.get("caption", "") or ""

        # Extract attachments
        attachments = await self._extract_attachments(message)

        if not text and not attachments:
            return None

        # Store message_id as float in timestamp field.
        # Round-trip: daemon does int(ts * 1000) -> ts * 1000.
        # Channel recovers: message_id = ts // 1000.
        timestamp = float(message_id)

        return InboundMessage(
            text=text,
            sender=sender,
            timestamp=timestamp,
            source="telegram",
            attachments=attachments or None,
        )

    async def _extract_attachments(self, message: dict) -> list[Attachment]:
        """Download and return attachments from a Telegram message."""
        attachments = []

        # Photos — take largest resolution
        photos = message.get("photo")
        if photos:
            best = photos[-1]  # Largest size
            att = await self._download_file(
                best.get("file_id", ""),
                content_type="image/jpeg",
                size=best.get("file_size", 0),
            )
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
                attachments.append(att)

        # Documents
        doc = message.get("document")
        if doc:
            att = await self._download_file(
                doc.get("file_id", ""),
                content_type=doc.get("mime_type", "application/octet-stream"),
                filename=doc.get("file_name", ""),
                size=doc.get("file_size", 0),
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
            local_path = self.download_dir / f"{int(time.time())}_{filename}"

            local_path.write_bytes(resp.content)
            actual_size = size or len(resp.content)

            return Attachment(
                content_type=content_type,
                local_path=str(local_path),
                filename=filename,
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
                chunks = self._chunk_text(text)
                for chunk in chunks:
                    await self._api("sendMessage", chat_id=chat_id, text=chunk)
        elif text:
            chunks = self._chunk_text(text)
            for chunk in chunks:
                await self._api("sendMessage", chat_id=chat_id, text=chunk)

    async def _send_voice(self, chat_id: int, path: Path) -> None:
        """Send a voice/audio file."""
        with open(path, "rb") as f:
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
        with open(path, "rb") as f:
            await self._api(
                "sendPhoto",
                **params,
                _files={"photo": (path.name, f, "image/jpeg")},
            )

    async def _send_document(self, chat_id: int, path: Path) -> None:
        """Send a document."""
        with open(path, "rb") as f:
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
