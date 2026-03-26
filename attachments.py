"""Attachment processing — image fitting, document text extraction.

Pure functions that take config values as parameters and return results.
No daemon state access.
"""

from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path

log = logging.getLogger(__name__)


class ImageTooLarge(Exception):
    """Raised when an image can't be fit within API limits."""


def fit_image(data: bytes, content_type: str, max_bytes: int,
              max_dimension: int, quality_steps: list[int] | None = None,
              path: str = "") -> bytes:
    """Scale dimensions and reduce quality to fit within API limits.

    Strategy: (1) shrink to max_dimension per side, (2) step down JPEG quality.
    Raises ImageTooLarge if nothing works.
    """
    from PIL import Image, ImageOps  # type: ignore[import-unresolved]

    is_jpeg = content_type == "image/jpeg"
    img = Image.open(BytesIO(data))
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

    if len(data) <= max_bytes:
        img.close()
        return data

    # Step 2: reduce JPEG quality (only works for JPEG — PNG is lossless)
    if is_jpeg:
        steps = quality_steps if quality_steps is not None else [85, 60, 40]
        for q in steps:
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=q)
            data = buf.getvalue()
            if len(data) <= max_bytes:
                log.info("JPEG quality %d brought size to %d bytes: %s", q, len(data), path)
                img.close()
                return data

    img.close()
    raise ImageTooLarge(f"{len(data) / (1024*1024):.1f}MB after compression")


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

    # PDF
    if content_type == "application/pdf" or ext == ".pdf":
        try:
            from pypdf import PdfReader  # type: ignore[import-unresolved]
        except ImportError:
            return None
        reader = PdfReader(path)
        parts = []
        total = 0
        for page in reader.pages:
            page_text = page.extract_text() or ""
            if total + len(page_text) > max_chars:
                parts.append(page_text[:max_chars - total])
                parts.append(f"\n[… truncated at {max_chars:,} chars]")
                break
            parts.append(page_text)
            total += len(page_text)
        return "\n".join(parts) or None

    return None
