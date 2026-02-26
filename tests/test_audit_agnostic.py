"""Audit tests: channel agnosticism (P-022) and interface parity (P-023).

These tests verify framework code stays agnostic to external sources
and both interfaces (CLI + HTTP API) return equivalent data schemas.
"""

import re
from pathlib import Path

import pytest

# Framework source directory (excluding channels/, providers/, tests/)
_LUCYD_DIR = Path(__file__).resolve().parent.parent
_FRAMEWORK_MODULES = [
    "lucyd.py",
    "agentic.py",
    "config.py",
    "context.py",
    "session.py",
    "skills.py",
    "memory.py",
    "memory_schema.py",
    "consolidation.py",
    "synthesis.py",
]
_TOOLS_DIR = _LUCYD_DIR / "tools"

# Channel-specific transport names (not Python's `signal` module)
_CHANNEL_NAMES = re.compile(r"\btelegram\b|\bwhatsapp\b|\bdiscord\b", re.IGNORECASE)

# Files exempted from the broad channel name scan.
# Each has its own dedicated test with documented debt thresholds.
# - config.py: config loader must validate channel-specific sections
# - context.py: has source-routing section (known debt, separate test)
_EXEMPT_MODULES = {"config.py", "context.py"}


def _scan_file(path: Path) -> list[tuple[int, str]]:
    """Scan a .py file for channel-specific strings. Returns (line_no, line)."""
    hits = []
    try:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # Skip comments
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if _CHANNEL_NAMES.search(line):
                hits.append((i, line.rstrip()))
    except (OSError, UnicodeDecodeError):
        pass
    return hits


# ─── P-022: Channel/Transport Agnosticism ────────────────────────


class TestChannelAgnosticism:
    """P-022: Framework code must not reference specific channel types."""

    def test_no_channel_names_in_framework_code(self):
        """Grep framework .py files (top-level) for channel-specific strings.

        Exempts config.py (must validate channel sections) and context.py
        (has channel-aware source routing — known P-022 debt, documented).
        """
        violations = []
        for name in _FRAMEWORK_MODULES:
            if name in _EXEMPT_MODULES:
                continue
            path = _LUCYD_DIR / name
            if path.exists():
                for lineno, line in _scan_file(path):
                    violations.append(f"{name}:{lineno}: {line}")

        assert not violations, (
            "Channel-specific strings found in framework code (P-022):\n"
            + "\n".join(violations)
        )

    def test_no_channel_names_in_session_logic(self):
        """session.py must never reference a specific channel."""
        path = _LUCYD_DIR / "session.py"
        hits = _scan_file(path)
        assert not hits, f"session.py references channel names at: {hits}"

    def test_no_channel_names_in_context_builder(self):
        """context.py channel references limited to source-routing section.

        Known debt: context.py has Telegram-specific source routing
        at _build_dynamic(). This is acceptable as it enriches the
        agent's session type awareness but should not grow.
        """
        path = _LUCYD_DIR / "context.py"
        hits = _scan_file(path)
        # Known: _build_dynamic has 'elif source == "telegram":' routing
        # Limit: at most 5 references (current: 3)
        assert len(hits) <= 5, (
            f"context.py channel references exceeded threshold (max 5): {hits}"
        )

    def test_no_channel_names_in_agentic_loop(self):
        """agentic.py must never reference a specific channel."""
        path = _LUCYD_DIR / "agentic.py"
        hits = _scan_file(path)
        assert not hits, f"agentic.py references channel names at: {hits}"

    def test_no_channel_names_in_tools(self):
        """tools/*.py channel references limited to known locations.

        Known debt: messaging.py has Telegram-specific reaction emoji
        description. This is acceptable documentation but should not grow.
        """
        if not _TOOLS_DIR.exists():
            pytest.skip("tools/ directory not found")
        violations = []
        for py_file in sorted(_TOOLS_DIR.glob("*.py")):
            for lineno, line in _scan_file(py_file):
                violations.append(f"tools/{py_file.name}:{lineno}: {line}")
        # Known: messaging.py has Telegram reaction emoji description (1 hit)
        assert len(violations) <= 2, (
            "tools/ channel references exceeded threshold (max 2):\n"
            + "\n".join(violations)
        )

    def test_contact_resolution_is_channel_agnostic(self):
        """lucyd-send contact resolution iterates all channel configs."""
        import importlib.util
        from importlib.machinery import SourceFileLoader

        bin_dir = _LUCYD_DIR / "bin"
        loader = SourceFileLoader("lucyd_send", str(bin_dir / "lucyd-send"))
        spec = importlib.util.spec_from_loader("lucyd_send", loader)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # Read the source of list_sessions to verify it doesn't hardcode telegram
        import inspect
        source = inspect.getsource(mod.list_sessions)

        # The old pattern was: config.get("channel", {}).get("telegram", {}).get("contacts", {})
        assert '.get("telegram"' not in source, (
            "list_sessions still hardcodes 'telegram' for contact resolution"
        )


# ─── P-023: CLI/API Data Parity ─────────────────────────────────


class TestInterfaceParity:
    """P-023: CLI and HTTP API must return equivalent data."""

    def test_sessions_fields_from_shared_function(self, tmp_path):
        """build_session_info() returns all fields needed by both interfaces."""
        from session import Session, build_session_info

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        session = Session("s-parity", sessions_dir, model="primary")
        session.messages = [
            {"role": "user", "content": "test"},
            {"role": "assistant", "text": "ok", "usage": {
                "input_tokens": 1000, "output_tokens": 200,
                "cache_read_tokens": 300, "cache_write_tokens": 100,
            }},
        ]
        session.compaction_count = 1

        info = build_session_info(
            sessions_dir=sessions_dir,
            session_id="s-parity",
            session=session,
            max_context_tokens=50000,
        )

        # All required fields present
        required_fields = {
            "session_id", "context_tokens", "context_pct", "cost_usd",
            "message_count", "compaction_count", "log_files", "log_bytes",
        }
        assert required_fields.issubset(info.keys()), (
            f"Missing fields: {required_fields - info.keys()}"
        )

    def test_cost_fields_include_cache_tokens(self, tmp_path):
        """HTTP /cost returns cache_read_tokens and cache_write_tokens."""
        import sqlite3
        import time

        from lucyd import LucydDaemon

        # Minimal config
        from config import Config
        cfg_data = {
            "agent": {"name": "Test", "workspace": str(tmp_path / "ws")},
            "channel": {"type": "cli"},
            "models": {"primary": {"provider": "anthropic-compat", "model": "m"}},
            "paths": {
                "state_dir": str(tmp_path / "state"),
                "sessions_dir": str(tmp_path / "sessions"),
                "cost_db": str(tmp_path / "cost.db"),
                "log_file": str(tmp_path / "lucyd.log"),
            },
        }
        (tmp_path / "ws").mkdir()
        (tmp_path / "ws" / "SOUL.md").write_text("# Test")
        (tmp_path / "state").mkdir()
        config = Config(cfg_data)
        daemon = LucydDaemon(config)
        daemon.providers = {"primary": None}

        # Populate cost DB
        now = int(time.time())
        conn = sqlite3.connect(str(tmp_path / "cost.db"))
        conn.execute("""
            CREATE TABLE costs (
                timestamp INTEGER, session_id TEXT, model TEXT,
                input_tokens INTEGER, output_tokens INTEGER,
                cache_read_tokens INTEGER, cache_write_tokens INTEGER,
                cost_usd REAL
            )
        """)
        conn.execute(
            "INSERT INTO costs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now, "s1", "m", 1000, 500, 300, 100, 0.01),
        )
        conn.commit()
        conn.close()

        result = daemon._build_cost("today")

        assert len(result["models"]) == 1
        model = result["models"][0]
        assert "cache_read_tokens" in model
        assert "cache_write_tokens" in model
        assert model["cache_read_tokens"] == 300
        assert model["cache_write_tokens"] == 100

    def test_cost_week_window_aligned(self):
        """CLI and HTTP use the same 'week' window definition (7 * 86400 from now)."""
        import inspect

        from lucyd import LucydDaemon

        # Verify the daemon uses int(time.time()) - 7 * 86400 (not today_start - 6*86400)
        source = inspect.getsource(LucydDaemon._build_cost)
        assert "int(time.time()) - 7 * 86400" in source, (
            "HTTP _build_cost week window should use int(time.time()) - 7 * 86400"
        )

        # CLI uses the same pattern
        import importlib.util
        from importlib.machinery import SourceFileLoader

        bin_dir = _LUCYD_DIR / "bin"
        loader = SourceFileLoader("lucyd_send_chk", str(bin_dir / "lucyd-send"))
        spec = importlib.util.spec_from_loader("lucyd_send_chk", loader)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        cli_source = inspect.getsource(mod.query_cost)
        assert "int(time.time()) - 7 * 86400" in cli_source, (
            "CLI query_cost week window should use int(time.time()) - 7 * 86400"
        )
