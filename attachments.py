"""Attachment processing — types, image fitting, document text extraction.

Pure functions that take config values as parameters and return results.
No daemon state access.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class Attachment:
    content_type: str    # "image/jpeg", "audio/ogg", etc.
    local_path: str      # Absolute path on disk
    filename: str        # Original filename or ""
    size: int            # Bytes
    is_voice: bool = False  # True = recorded voice message; False = audio file


class ImageTooLarge(Exception):
    pass


def _b64_size(raw_bytes: int) -> int:
    """Standard base64 encoded length (RFC 4648, with padding)."""
    return ((raw_bytes + 2) // 3) * 4


def fit_image(data: bytes, content_type: str, max_bytes: int,
              max_dimension: int, quality_steps: list[int] | None = None,
              path: str = "") -> bytes:
    """Scale dimensions and reduce quality to fit within API limits.

    ``max_bytes`` is the cap on the BASE64-ENCODED payload, because that
    is what providers (Anthropic, OpenAI) measure against their image
    size limits. Comparisons in this function use the base64 length, not
    the raw byte length — a 4.9 MiB raw image becomes ~6.5 MiB base64
    and exceeds Anthropic's 5 MiB cap.

    Strategy: (1) shrink to max_dimension per side, (2) step down JPEG quality.
    Raises ImageTooLarge if nothing works.
    """
    from PIL import Image, ImageOps

    is_jpeg = content_type == "image/jpeg"
    # Image.open returns ImageFile (a subclass) in some Pillow stubs,
    # ImageOps.exif_transpose returns Image — annotate to the broader type
    # so the reassignment passes mypy --strict in both stub flavors.
    img: Image.Image = Image.open(BytesIO(data))
    img = ImageOps.exif_transpose(img)

    # Step 1: scale dimensions if any side exceeds max_dimension
    if max(img.size) > max_dimension:
        log.info("Scaling %dx%d to fit %dpx: %s", img.size[0], img.size[1],
                 max_dimension, path)
        img.thumbnail((max_dimension, max_dimension))
        buf = BytesIO()
        if is_jpeg:
            img.save(buf, format="JPEG", quality=90)
        else:
            img.save(buf, format="PNG")
        data = buf.getvalue()

    if _b64_size(len(data)) <= max_bytes:
        img.close()
        return data

    # Step 2: reduce JPEG quality (only works for JPEG — PNG is lossless)
    if is_jpeg:
        steps = quality_steps if quality_steps is not None else [85, 60, 40]
        for q in steps:
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=q)
            data = buf.getvalue()
            if _b64_size(len(data)) <= max_bytes:
                log.info("JPEG quality %d brought b64 size to %d bytes: %s",
                         q, _b64_size(len(data)), path)
                img.close()
                return data

    img.close()
    raise ImageTooLarge(f"{_b64_size(len(data)) / (1024*1024):.1f}MB after compression (base64)")


def extract_document_text(path: str, content_type: str, filename: str,
                          max_chars: int, max_bytes: int,
                          text_extensions: list[str]) -> str | None:
    """Extract text from a document. Returns None if not a readable format."""
    file_path = Path(path)

    if file_path.stat().st_size > max_bytes:
        return None

    ext = Path(filename).suffix.lower() if filename else ""

    # Plain text — by extension or text/* MIME
    if ext in text_extensions or content_type.startswith("text/"):
        text = file_path.read_bytes().decode("utf-8", errors="replace")
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n[… truncated at {max_chars:,} chars]"
        return text

    return None
