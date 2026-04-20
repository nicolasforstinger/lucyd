"""Tests for the pdf_read tool (tools/pdf.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.pdf import _parse_page_range, tool_pdf_read


def _write_text_pdf(path: Path, page_texts: list[str]) -> None:
    """Create a minimal PDF with extractable text on each page.

    Generates raw PDF bytes with text content streams — no external
    dependencies beyond pypdf (for verification) needed.
    """
    # Build PDF objects
    objects: list[str] = []
    obj_offsets: list[int] = []

    # Obj 1: Catalog
    objects.append("1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")

    # Obj 2: Pages (placeholder — filled after page objects)
    pages_obj_idx = len(objects)
    objects.append("")  # placeholder

    # Obj 3: Font
    font_obj_num = 3
    objects.append(
        f"{font_obj_num} 0 obj\n"
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\n"
        "endobj\n"
    )

    page_obj_nums: list[int] = []
    next_obj = font_obj_num + 1

    for text in page_texts:
        # Content stream
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
        content_obj = next_obj
        objects.append(
            f"{content_obj} 0 obj\n"
            f"<< /Length {len(stream)} >>\n"
            f"stream\n{stream}\nendstream\n"
            f"endobj\n"
        )
        next_obj += 1

        # Page object
        page_obj = next_obj
        page_obj_nums.append(page_obj)
        objects.append(
            f"{page_obj} 0 obj\n"
            f"<< /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 612 792] "
            f"/Contents {content_obj} 0 R "
            f"/Resources << /Font << /F1 {font_obj_num} 0 R >> >> "
            f">>\n"
            f"endobj\n"
        )
        next_obj += 1

    # Fill Pages object
    kids = " ".join(f"{n} 0 R" for n in page_obj_nums)
    objects[pages_obj_idx] = (
        f"2 0 obj\n"
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_obj_nums)} >>\n"
        f"endobj\n"
    )

    # Assemble
    pdf = "%PDF-1.4\n"
    for obj in objects:
        obj_offsets.append(len(pdf))
        pdf += obj

    xref_offset = len(pdf)
    total_objs = next_obj
    pdf += f"xref\n0 {total_objs}\n"
    pdf += "0000000000 65535 f \n"
    for offset in obj_offsets:
        pdf += f"{offset:010d} 00000 n \n"

    pdf += (
        f"trailer\n<< /Size {total_objs} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    )

    path.write_text(pdf)

# ── Page Range Parsing ───────────────────────────────────────────


class TestParsePageRange:
    """Unit tests for _parse_page_range."""

    def test_single_page(self) -> None:
        indices, err = _parse_page_range("3", total=10)
        assert err is None
        assert indices == [2]

    def test_range(self) -> None:
        indices, err = _parse_page_range("2-5", total=10)
        assert err is None
        assert indices == [1, 2, 3, 4]

    def test_comma_separated(self) -> None:
        indices, err = _parse_page_range("1,3,5", total=10)
        assert err is None
        assert indices == [0, 2, 4]

    def test_mixed_ranges_and_singles(self) -> None:
        indices, err = _parse_page_range("1-3,7,9-10", total=10)
        assert err is None
        assert indices == [0, 1, 2, 6, 8, 9]

    def test_out_of_range_pages_skipped(self) -> None:
        """Pages beyond total are silently excluded."""
        indices, err = _parse_page_range("8-15", total=10)
        assert err is None
        assert indices == [7, 8, 9]

    def test_all_out_of_range_returns_empty(self) -> None:
        indices, err = _parse_page_range("20", total=10)
        assert err is None
        assert indices == []

    def test_invalid_range_returns_error(self) -> None:
        _, err = _parse_page_range("abc", total=10)
        assert err is not None
        assert "Invalid page number" in err

    def test_reversed_range_returns_error(self) -> None:
        _, err = _parse_page_range("5-2", total=10)
        assert err is not None
        assert "Invalid page range" in err

    def test_zero_page_returns_error(self) -> None:
        _, err = _parse_page_range("0", total=10)
        assert err is not None
        assert "1-indexed" in err

    def test_deduplication(self) -> None:
        """Overlapping ranges don't produce duplicate indices."""
        indices, err = _parse_page_range("1-3,2-4", total=10)
        assert err is None
        assert indices == [0, 1, 2, 3]

    def test_empty_parts_ignored(self) -> None:
        """Trailing commas or empty segments are ignored."""
        indices, err = _parse_page_range("1,,3,", total=10)
        assert err is None
        assert indices == [0, 2]


# ── Tool: pdf_read ───────────────────────────────────────────────


class TestPdfRead:
    """Integration tests for tool_pdf_read."""

    def test_file_not_found(self, tmp_path: object) -> None:
        import pathlib
        d = pathlib.Path(str(tmp_path))

        from tools.pdf import _PATH_ALLOW
        _PATH_ALLOW.clear()
        _PATH_ALLOW.append(str(d))

        result = tool_pdf_read(str(d / "nonexistent.pdf"))
        assert "not found" in result.lower()

    def test_path_not_allowed(self) -> None:
        from tools.pdf import _PATH_ALLOW
        _PATH_ALLOW.clear()
        _PATH_ALLOW.append("/allowed")

        result = tool_pdf_read("/forbidden/doc.pdf")
        assert "not allowed" in result.lower()

    def test_not_a_pdf(self, tmp_path: object) -> None:
        """Non-PDF file → error from pypdf."""
        import pathlib
        p = pathlib.Path(str(tmp_path)) / "fake.pdf"
        p.write_bytes(b"not a pdf")

        from tools.pdf import _PATH_ALLOW
        _PATH_ALLOW.clear()
        _PATH_ALLOW.append(str(p.parent))

        result = tool_pdf_read(str(p))
        assert "error" in result.lower()

    @pytest.mark.skipif(
        not __import__("importlib").util.find_spec("pypdf"),
        reason="pypdf not installed",
    )
    def test_blank_pdf_reports_empty_pages(self, tmp_path: object) -> None:
        """Blank PDF → all pages reported as image-based."""
        import pathlib

        from pypdf import PdfWriter

        p = pathlib.Path(str(tmp_path)) / "blank.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        writer.write(str(p))

        from tools.pdf import _PATH_ALLOW
        _PATH_ALLOW.clear()
        _PATH_ALLOW.append(str(p.parent))

        result = tool_pdf_read(str(p))
        assert "1 page(s) total" in result
        assert "no extractable text" in result

    @pytest.mark.skipif(
        not __import__("importlib").util.find_spec("pypdf"),
        reason="pypdf not installed",
    )
    def test_text_pdf_extraction(self, tmp_path: object) -> None:
        """PDF with text → text extracted with page headers."""
        import pathlib

        p = pathlib.Path(str(tmp_path)) / "text.pdf"
        _write_text_pdf(p, ["Hello from page one", "Hello from page two"])

        from tools.pdf import _PATH_ALLOW
        _PATH_ALLOW.clear()
        _PATH_ALLOW.append(str(p.parent))

        result = tool_pdf_read(str(p))
        assert "2 page(s) total" in result
        assert "--- page 1 ---" in result
        assert "Hello from page one" in result
        assert "--- page 2 ---" in result
        assert "Hello from page two" in result

    @pytest.mark.skipif(
        not __import__("importlib").util.find_spec("pypdf"),
        reason="pypdf not installed",
    )
    def test_page_range_selection(self, tmp_path: object) -> None:
        """pages='2' → only page 2 extracted."""
        import pathlib

        p = pathlib.Path(str(tmp_path)) / "multi.pdf"
        _write_text_pdf(p, ["Page one content", "Page two content", "Page three content"])

        from tools.pdf import _PATH_ALLOW
        _PATH_ALLOW.clear()
        _PATH_ALLOW.append(str(p.parent))

        result = tool_pdf_read(str(p), pages="2")
        assert "3 page(s) total" in result
        assert "reading pages 2" in result
        assert "--- page 2 ---" in result
        assert "Page two content" in result
        assert "Page one content" not in result
        assert "Page three content" not in result

    @pytest.mark.skipif(
        not __import__("importlib").util.find_spec("pypdf"),
        reason="pypdf not installed",
    )
    def test_out_of_range_pages_error(self, tmp_path: object) -> None:
        """Requesting pages beyond total → error."""
        import pathlib

        from pypdf import PdfWriter

        p = pathlib.Path(str(tmp_path)) / "short.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        writer.write(str(p))

        from tools.pdf import _PATH_ALLOW
        _PATH_ALLOW.clear()
        _PATH_ALLOW.append(str(p.parent))

        result = tool_pdf_read(str(p), pages="5-10")
        assert "out of range" in result.lower()
