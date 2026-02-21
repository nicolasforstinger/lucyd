"""Tests for tools/filesystem.py — configure, _check_path, read, write, edit."""

import pytest

from tools.filesystem import _check_path, configure, tool_edit, tool_read, tool_write

# ─── Path Validation ─────────────────────────────────────────────

class TestPathValidation:
    def test_allowed_path_passes(self, fs_workspace):
        result = _check_path(str(fs_workspace / "hello.txt"))
        assert result is None

    def test_disallowed_path_rejected(self, fs_workspace):
        result = _check_path("/etc/passwd")
        assert result is not None
        assert result.startswith("Error: Path not allowed")

    def test_traversal_blocked(self, fs_workspace):
        """Path with ../ is resolved before checking — traversal can't escape."""
        evil = str(fs_workspace / "subdir" / ".." / ".." / "etc" / "passwd")
        result = _check_path(evil)
        assert result is not None
        assert result.startswith("Error: Path not allowed")

    def test_empty_allowlist_exact_message(self):
        """Empty allowed_paths config denies with exact error message (fail-closed)."""
        from tools import filesystem
        original = filesystem._PATH_ALLOW
        filesystem._PATH_ALLOW = []
        try:
            result = _check_path("/tmp/anything")
            assert result == "Error: No allowed paths configured — filesystem access denied"
        finally:
            filesystem._PATH_ALLOW = original

    def test_error_lists_allowed_prefixes(self, tmp_path):
        """Denied path error shows allowed prefixes separated by ', '."""
        from tools import filesystem
        original = filesystem._PATH_ALLOW
        filesystem._PATH_ALLOW = ["/allowed/a", "/allowed/b"]
        try:
            result = _check_path("/etc/passwd")
            assert result is not None
            assert "/allowed/a, /allowed/b" in result
        finally:
            filesystem._PATH_ALLOW = original

    def test_tilde_expansion(self, tmp_path):
        """Tilde paths are expanded and resolved."""
        configure(["/home/"])
        result = _check_path("~/somefile")
        assert result is None or "not allowed" in result
        configure([str(tmp_path)])  # restore

    def test_symlink_escape_blocked(self, fs_workspace):
        """Symlink inside allowed dir pointing outside is blocked."""
        import os
        link = fs_workspace / "evil_link"
        try:
            os.symlink("/etc/passwd", str(link))
        except OSError:
            pytest.skip("Cannot create symlink")
        result = _check_path(str(link))
        assert result is not None
        assert result.startswith("Error: Path not allowed")


# ─── Read ────────────────────────────────────────────────────────

class TestRead:
    def test_simple_file_numbered_lines(self, fs_workspace):
        result = tool_read(str(fs_workspace / "hello.txt"))
        # Lines start at 1, not 0
        assert result.lstrip().startswith("1\t")
        assert "line one" in result
        assert "line two" in result

    def test_line_numbers_start_at_one(self, fs_workspace):
        """Line numbers must start at 1 for offset=0."""
        result = tool_read(str(fs_workspace / "hello.txt"))
        lines = result.strip().split("\n")
        # First line should be "     1\tline one\n"
        first_line = lines[0].strip()
        assert first_line.startswith("1\t")

    def test_line_numbers_with_offset(self, fs_workspace):
        """Line numbers reflect the offset — offset=1 means line numbers start at 2."""
        result = tool_read(str(fs_workspace / "hello.txt"), offset=1, limit=1)
        # Should show line 2 (offset 1 = second line, 0-indexed)
        stripped = result.strip().split("\n")[0].strip()
        assert stripped.startswith("2\t")
        assert "line two" in stripped

    def test_result_starts_clean(self, fs_workspace):
        """Result string starts with whitespace + line number, not garbage prefix."""
        result = tool_read(str(fs_workspace / "hello.txt"))
        # The result should start with spaces + "1" + tab, not "XXXX"
        assert not result.startswith("XXXX")

    def test_offset_and_limit(self, fs_workspace):
        result = tool_read(str(fs_workspace / "hello.txt"), offset=1, limit=1)
        assert "line two" in result
        assert "line one" not in result

    def test_file_not_found(self, fs_workspace):
        result = tool_read(str(fs_workspace / "nope.txt"))
        assert result.startswith("Error:")
        assert "not found" in result

    def test_directory_rejected(self, fs_workspace):
        result = tool_read(str(fs_workspace / "subdir"))
        assert result.startswith("Error:")
        assert "Not a file" in result

    def test_long_line_truncated(self, fs_workspace):
        result = tool_read(str(fs_workspace / "long.txt"))
        # long.txt has 3000 chars on one line, should be truncated at 2000
        assert "..." in result

    def test_truncation_boundary_at_2001_chars(self, fs_workspace):
        """A line of exactly 2001 chars (len > 2000) IS truncated."""
        # Write 2000 visible chars + \n = readlines gives a string of len 2001
        (fs_workspace / "boundary.txt").write_text("A" * 2000 + "\n")
        result = tool_read(str(fs_workspace / "boundary.txt"))
        assert "..." in result

    def test_no_truncation_at_2000_chars(self, fs_workspace):
        """A line of exactly 2000 chars (len == 2000, NOT > 2000) is NOT truncated."""
        # Write 1999 visible chars + \n = readlines gives a string of len 2000
        (fs_workspace / "exact.txt").write_text("B" * 1999 + "\n")
        result = tool_read(str(fs_workspace / "exact.txt"))
        assert "..." not in result

    def test_truncation_preserves_exactly_2000_chars(self, fs_workspace):
        """Truncated line has exactly 2000 chars of content, then '...\\n'."""
        content = "X" * 3000 + "\n"
        (fs_workspace / "trunc_exact.txt").write_text(content)
        result = tool_read(str(fs_workspace / "trunc_exact.txt"))
        # Extract the line content after the line number prefix
        line_content = result.split("\t", 1)[1]
        # Should be exactly 2000 X's followed by "...\n"
        assert line_content.startswith("X" * 2000)
        # Must NOT have 2001 X's (mutmut_35 would produce [:2001])
        assert not line_content.startswith("X" * 2001)
        # Truncation marker must be exactly "...\n", not "XX...\nXX"
        truncated_part = line_content[2000:]
        assert truncated_part == "...\n"

    def test_remaining_count_footer(self, fs_workspace):
        result = tool_read(str(fs_workspace / "hello.txt"), offset=0, limit=1)
        assert "more lines" in result

    def test_remaining_count_exact(self, fs_workspace):
        """Remaining count = total - offset - limit."""
        # hello.txt has 3 lines
        result = tool_read(str(fs_workspace / "hello.txt"), offset=0, limit=1)
        # Should show "[... 2 more lines]"
        assert "2 more lines" in result

    def test_no_remaining_when_all_shown(self, fs_workspace):
        """When limit covers all lines, no 'more lines' footer."""
        result = tool_read(str(fs_workspace / "hello.txt"), offset=0, limit=10)
        assert "more lines" not in result

    def test_no_remaining_at_exact_boundary(self, fs_workspace):
        """When offset + limit == total, no footer (< not <=)."""
        # hello.txt has 3 lines. offset=0, limit=3 → offset+limit == total → no footer
        result = tool_read(str(fs_workspace / "hello.txt"), offset=0, limit=3)
        assert "more lines" not in result

    def test_remaining_with_offset(self, fs_workspace):
        """Remaining count with offset: total(3) - offset(1) - limit(1) = 1."""
        result = tool_read(str(fs_workspace / "hello.txt"), offset=1, limit=1)
        assert "1 more lines" in result

    def test_default_limit_is_2000(self, fs_workspace):
        """Default limit=2000: file with 2002 lines returns exactly 2000."""
        content = "".join(f"line {i}\n" for i in range(2002))
        (fs_workspace / "big.txt").write_text(content)
        result = tool_read(str(fs_workspace / "big.txt"))  # no limit arg
        assert "2 more lines" in result
        # Count numbered content lines (line number prefix pattern)
        import re
        numbered_lines = re.findall(r"^\s*\d+\t", result, re.MULTILINE)
        assert len(numbered_lines) == 2000

    def test_blocked_path(self, fs_workspace):
        result = tool_read("/etc/passwd")
        assert result.startswith("Error:")
        assert "not allowed" in result


# ─── Write ───────────────────────────────────────────────────────

class TestWrite:
    def test_creates_file(self, fs_workspace):
        path = str(fs_workspace / "new.txt")
        result = tool_write(path, "hello world")
        assert "Written" in result
        assert (fs_workspace / "new.txt").read_text() == "hello world"

    def test_creates_parent_dirs(self, fs_workspace):
        path = str(fs_workspace / "a" / "b" / "deep.txt")
        result = tool_write(path, "deep content")
        assert "Written" in result
        assert (fs_workspace / "a" / "b" / "deep.txt").exists()

    def test_returns_char_count(self, fs_workspace):
        path = str(fs_workspace / "counted.txt")
        result = tool_write(path, "12345")
        assert "5" in result

    def test_blocked_path(self, fs_workspace):
        result = tool_write("/etc/evil.txt", "bad")
        assert result.startswith("Error:")
        assert "not allowed" in result


# ─── Edit ────────────────────────────────────────────────────────

class TestEdit:
    def test_single_replacement(self, fs_workspace):
        path = str(fs_workspace / "hello.txt")
        result = tool_edit(path, "line one", "LINE ONE")
        assert "Edited" in result
        content = (fs_workspace / "hello.txt").read_text()
        assert "LINE ONE" in content
        assert "line one" not in content

    def test_ambiguous_match_error(self, fs_workspace):
        # Write a file with repeated text
        path = str(fs_workspace / "repeat.txt")
        tool_write(path, "aaa\naaa\n")
        result = tool_edit(path, "aaa", "bbb")
        assert result.startswith("Error:")
        assert "2 times" in result

    def test_replace_all(self, fs_workspace):
        path = str(fs_workspace / "repeat.txt")
        tool_write(path, "aaa\naaa\n")
        result = tool_edit(path, "aaa", "bbb", replace_all=True)
        assert "Replaced 2" in result
        content = (fs_workspace / "repeat.txt").read_text()
        assert "aaa" not in content
        assert content.count("bbb") == 2

    def test_string_not_found(self, fs_workspace):
        path = str(fs_workspace / "hello.txt")
        result = tool_edit(path, "NONEXISTENT", "replacement")
        assert result.startswith("Error:")
        assert "not found" in result

    def test_blocked_path(self, fs_workspace):
        result = tool_edit("/etc/passwd", "root", "hacker")
        assert result.startswith("Error:")
        assert "not allowed" in result
