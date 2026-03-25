"""Shared data types for the Lucyd framework."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Attachment:
    content_type: str    # "image/jpeg", "audio/ogg", etc.
    local_path: str      # Absolute path on disk
    filename: str        # Original filename or ""
    size: int            # Bytes
    is_voice: bool = False  # True = recorded voice message; False = audio file


@dataclass
class InboundMessage:
    text: str
    sender: str           # Contact name, username, "cli", etc.
    timestamp: float
    source: str           # "telegram", "cli", etc.
    group_id: str | None = None
    group_name: str | None = None
    quote: str | None = None
    attachments: list[Attachment] | None = None
    message_id: int | None = None
