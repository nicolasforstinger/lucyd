"""PDF reading tool — text extraction with explicit page control.

Replaces the baked-in PDF pipeline that silently extracted text and
fell back to image rendering.  The agent now controls extraction
explicitly: which pages, and gets transparent reporting of page count,
truncation, and scanned (imageless) pages.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from . import ToolSpec

log = logging.getLogger(__name__)

# Set at startup via configure()
_PATH_ALLOW: list[str] = []


def configure(config: Any = None, **_: Any) -> None:
    """Pull filesystem allowed-paths from config."""
    global _PATH_ALLOW
    if config is not None:
        _PATH_ALLOW = config.filesystem_allowed_paths


def _check_path(file_path: str) -> str | None:
    """Validate file path against allowlist.  Returns error or None."""
    try:
        resolved = str(Path(file_path).expanduser().resolve())
    except Exception:
        return f"Error: Invalid path: {file_path}"
    if not _PATH_ALLOW:
        return "Error: No allowed paths configured — filesystem access denied"
    for prefix in _PATH_ALLOW:
        if resolved == prefix or resolved.startswith(prefix + os.sep):
            return None
    return f"Error: Path not allowed: {file_path}"


def _parse_page_range(pages: str, total: int) -> tuple[list[int], str | None]:
    """Parse ``'1-5'`` or ``'3,7-9'`` into 0-based page indices.

    Returns ``(indices, error_or_None)``.
    """
    indices: list[int] = []
    for part in pages.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            tokens = part.split("-", 1)
            try:
                start, end = int(tokens[0]), int(tokens[1])
            except ValueError:
                return [], f"Error: Invalid page range: {part!r}"
            if start < 1 or end < start:
                return [], f"Error: Invalid page range: {part!r} (pages are 1-indexed)"
            for i in range(start - 1, min(end, total)):
                if i not in indices:
                    indices.append(i)
        else:
            try:
                p = int(part)
            except ValueError:
                return [], f"Error: Invalid page number: {part!r}"
            if p < 1:
                return [], f"Error: Invalid page number: {p} (pages are 1-indexed)"
            idx = p - 1
            if idx < total and idx not in indices:
                indices.append(idx)
    return indices, None


# Upper bound on characters returned per call.  The tool-registry
# truncation (default 30 000 chars) catches anything beyond this.
_MAX_CHARS = 100_000


def tool_pdf_read(file_path: str, pages: str = "") -> str:
    """Read a PDF and extract text from the requested pages."""
    err = _check_path(file_path)
    if err:
        return err

    p = Path(file_path).expanduser()
    if not p.exists():
        return f"Error: File not found: {file_path}"
    if not p.is_file():
        return f"Error: Not a file: {file_path}"

    try:
        from pypdf import PdfReader
    except ImportError:
        return "Error: pypdf is not installed"

    try:
        reader = PdfReader(str(p))
    except Exception as e:
        return f"Error: Cannot read PDF: {e}"

    total_pages = len(reader.pages)
    if total_pages == 0:
        return "PDF has 0 pages."

    # Determine which pages to extract
    if pages:
        indices, parse_err = _parse_page_range(pages, total_pages)
        if parse_err:
            return parse_err
        if not indices:
            return f"Error: Requested pages are out of range (PDF has {total_pages} pages)"
    else:
        indices = list(range(total_pages))

    # Extract text page by page
    parts: list[str] = []
    empty_pages: list[int] = []
    total_chars = 0
    truncated = False

    for idx in indices:
        page_num = idx + 1
        page_text = reader.pages[idx].extract_text() or ""
        if not page_text.strip():
            empty_pages.append(page_num)
            continue

        if total_chars + len(page_text) > _MAX_CHARS:
            remaining = _MAX_CHARS - total_chars
            if remaining > 0:
                parts.append(f"--- page {page_num} ---")
                parts.append(page_text[:remaining])
            parts.append(
                f"[truncated at {_MAX_CHARS:,} chars — "
                f"use pages parameter to read specific sections]"
            )
            truncated = True
            break

        parts.append(f"--- page {page_num} ---")
        parts.append(page_text)
        total_chars += len(page_text)

    # Build header
    header = f"PDF: {total_pages} page(s) total"
    if pages:
        header += f", reading pages {pages}"

    result_parts = [header]

    if empty_pages:
        page_list = ", ".join(str(p) for p in empty_pages)
        result_parts.append(
            f"Pages with no extractable text (likely scanned/image-based): {page_list}"
        )

    if truncated:
        result_parts.append(
            f"[output truncated — extracted {total_chars:,} chars before limit]"
        )

    result_parts.append("")  # blank line before content

    if parts:
        result_parts.extend(parts)
    elif empty_pages:
        result_parts.append(
            "[All requested pages are image-based with no extractable text]"
        )

    return "\n".join(result_parts)


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="pdf_read",
        description=(
            "Read a PDF file and extract text content. "
            "Returns page count and extracted text. "
            "Large PDFs are truncated — use pages (e.g. '1-5', '3', '10-20') to read in sections. "
            "Reports which pages have no extractable text (scanned/image-based)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to the PDF file",
                },
                "pages": {
                    "type": "string",
                    "description": (
                        "Page range to read, e.g. '1-5', '3', '7-9'. "
                        "Omit to read all pages."
                    ),
                    "default": "",
                },
            },
            "required": ["file_path"],
        },
        function=tool_pdf_read,
    ),
]
