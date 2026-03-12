"""Audit tests: channel agnosticism (P-022), interface parity (P-023),
single provider architecture (AI-005).

These tests verify framework code stays agnostic to external sources,
both interfaces (CLI + HTTP API) return equivalent data schemas,
and the single-provider architecture is not violated.
"""

import inspect
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

        # Minimal config
        from config import Config
        from lucyd import LucydDaemon
        cfg_data = {
            "agent": {
                "name": "Test", "workspace": str(tmp_path / "ws"),
                "context": {"stable": [], "semi_stable": []},
                "skills": {"dir": "skills", "always_on": []},
            },
            "channel": {"type": "cli", "debounce_ms": 500},
            "http": {
                "enabled": False, "host": "127.0.0.1", "port": 8100, "token_env": "",
                "download_dir": "/tmp/lucyd-http", "max_body_bytes": 10485760,
                "callback_url": "", "callback_token_env": "", "callback_timeout": 10,
                "rate_limit": 30, "rate_window": 60, "status_rate_limit": 60,
                "rate_limit_cleanup_threshold": 1000,
            },
            "models": {"primary": {"provider": "anthropic-compat", "model": "m"}},
            "memory": {
                "db": "", "search_top_k": 10, "vector_search_limit": 10000,
                "fts_min_results": 3, "embedding_timeout": 15,
                "consolidation": {"enabled": False, "min_messages": 4, "confidence_threshold": 0.6, "max_extraction_chars": 50000},
                "recall": {
                    "decay_rate": 0.03, "max_facts_in_context": 20, "max_dynamic_tokens": 1500, "max_episodes_at_start": 3,
                    "personality": {"priority_vector": 35, "priority_episodes": 25, "priority_facts": 15, "priority_commitments": 40,
                                   "fact_format": "natural", "show_emotional_tone": True, "episode_section_header": "Recent conversations",
                                   "synthesis_style": "structured",
                                   "synthesis_prompt_narrative": "", "synthesis_prompt_factual": ""},
                },
                "maintenance": {"stale_threshold_days": 90},
                "indexer": {"include_patterns": ["memory/*.md"], "exclude_dirs": [], "chunk_size_chars": 1600, "chunk_overlap_chars": 320, "embed_batch_limit": 100},
            },
            "tools": {
                "enabled": ["read", "write", "edit", "exec"],
                "plugins_dir": "plugins.d", "output_truncation": 30000,
                "subagent_deny": [], "subagent_max_turns": 0, "subagent_timeout": 0,
                "exec_timeout": 120, "exec_max_timeout": 600,
                "filesystem": {"allowed_paths": ["/tmp/"], "default_read_limit": 2000},
                "web_search": {"provider": "", "api_key_env": "", "timeout": 15},
                "web_fetch": {"timeout": 15},
                "tts": {"provider": "", "api_key_env": "", "timeout": 60, "api_url": ""},
                "scheduling": {"max_scheduled": 50, "max_delay": 86400},
            },
            "stt": {"backend": "", "voice_label": "voice message", "voice_fail_msg": "voice message — transcription failed",
                    "audio_label": "audio transcription", "audio_fail_msg": "audio transcription — failed"},
            "documents": {"enabled": True, "max_chars": 30000, "max_file_bytes": 10485760,
                          "text_extensions": [".txt", ".md"]},
            "logging": {"max_bytes": 10485760, "backup_count": 3, "suppress": []},
            "vision": {"max_image_bytes": 5242880, "max_dimension": 1568, "default_caption": "image",
                       "too_large_msg": "image too large to display", "jpeg_quality_steps": [85, 60, 40],
                       "caption_max_chars": 200},
            "behavior": {
                "silent_tokens": ["NO_REPLY"], "typing_indicators": True, "error_message": "error",
                "sqlite_timeout": 30,
                "api_retries": 2, "api_retry_base_delay": 2.0, "message_retries": 2, "message_retry_base_delay": 30.0,
                "audit_truncation_limit": 500, "agent_timeout_seconds": 600,
                "max_turns_per_message": 50, "max_cost_per_message": 0.0,
                "queue_capacity": 1000, "queue_poll_interval": 1.0, "quote_max_chars": 200,
                "telemetry_max_age": 30.0, "passive_notify_refs": [], "primary_sender": "",
                "compaction": {
                    "threshold_tokens": 150000, "max_tokens": 2048,
                    "prompt": "Summarize for {agent_name}.", "keep_recent_pct": 0.33,
                    "keep_recent_pct_min": 0.05, "keep_recent_pct_max": 0.9,
                    "min_messages": 4, "tool_result_max_chars": 2000, "warning_pct": 0.8,
                    "diary_prompt": "Write a log for {date}.",
                    "verify_enabled": True, "verify_max_turn_labels": 3, "verify_grounding_threshold": 0.5,
                },
            },
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
        daemon.provider = None

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

    def test_cli_list_sessions_uses_build_session_info(self):
        """CLI list_sessions() must call build_session_info(), not reimplement.

        Structural guard: if someone re-introduces duplicated state-file reading
        or cost querying into the CLI, this test catches it immediately.
        """
        import importlib.util
        import inspect
        from importlib.machinery import SourceFileLoader

        bin_dir = _LUCYD_DIR / "bin"
        loader = SourceFileLoader("lucyd_send_parity", str(bin_dir / "lucyd-send"))
        spec = importlib.util.spec_from_loader("lucyd_send_parity", loader)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        source = inspect.getsource(mod.list_sessions)

        # Must call the shared function
        assert "build_session_info(" in source, (
            "CLI list_sessions() must call build_session_info() — "
            "do not reimplement session data gathering inline (P-023)"
        )

        # Must NOT contain the old inline patterns that indicate duplicated logic
        assert "state.get(\"messages\"" not in source, (
            "CLI list_sessions() still reads messages from state file directly — "
            "use build_session_info() instead (P-023)"
        )
        assert "msg.get(\"role\") == \"assistant\"" not in source, (
            "CLI list_sessions() still walks messages for context tokens — "
            "use build_session_info() instead (P-023)"
        )
        assert "cost_db_query" not in source, (
            "CLI list_sessions() still queries cost DB directly — "
            "use build_session_info() instead (P-023)"
        )

    def test_cli_and_api_session_data_equivalent(self, tmp_path):
        """CLI and HTTP API paths produce the same data for the same session.

        Functional parity: set up session state, then verify build_session_info()
        (used by both CLI and HTTP) returns consistent values whether called with
        a live Session object (HTTP path) or without one (CLI path, reads state file).
        """
        from session import Session, build_session_info

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        # Create a live session with known state
        session = Session("s-parity-eq", sessions_dir, model="primary")
        session.messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "text": "hi", "usage": {
                "input_tokens": 2000, "output_tokens": 500,
                "cache_read_tokens": 800, "cache_write_tokens": 200,
            }},
        ]
        session.compaction_count = 2
        session._save_state()

        # HTTP path: live session object
        http_info = build_session_info(
            sessions_dir=sessions_dir,
            session_id="s-parity-eq",
            session=session,
            max_context_tokens=100000,
        )

        # CLI path: no live session, reads from state file
        cli_info = build_session_info(
            sessions_dir=sessions_dir,
            session_id="s-parity-eq",
            max_context_tokens=100000,
        )

        # Core data fields must match
        for field in ("session_id", "context_tokens", "context_pct",
                      "message_count", "compaction_count", "log_files", "log_bytes"):
            assert http_info[field] == cli_info[field], (
                f"Parity mismatch on '{field}': HTTP={http_info[field]}, CLI={cli_info[field]}"
            )

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


# ─── AI-005: Single Provider Architecture ─────────────────────────


# Retired concepts — grep these to catch re-introduction
_MULTI_MODEL_PATTERNS = re.compile(
    r"\bself\.providers\b"
    r"|\broute_model\b"
    r"|\bmodel_override\b"
    r"|\b_default_model\b"
    r"|\bcontext_tiers\b"
    r"|\btier_overrides\b"
    r"|\b_files_for_tier\b"
    r"|\bcompaction_model\b"
    r"|\bconsolidation_model\b"
    r"|\bsubagent_model\b"
    r"|\ball_model_names\b"
)

# tts.py has its own _default_model_id (TTS model, not LLM routing)
_AI005_EXEMPT = {"tools/tts.py", "tts.py"}


def _scan_for_multi_model(path: Path) -> list[tuple[int, str]]:
    """Scan a .py file for multi-model routing patterns."""
    hits = []
    try:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if _MULTI_MODEL_PATTERNS.search(line):
                hits.append((i, line.rstrip()))
    except (OSError, UnicodeDecodeError):
        pass
    return hits


class TestSingleProviderArchitecture:
    """AI-005: Single provider architecture — no multi-model routing."""

    def test_no_multi_model_in_framework(self):
        """Framework modules must not contain multi-model routing patterns."""
        violations = []
        for name in _FRAMEWORK_MODULES:
            if name in _AI005_EXEMPT:
                continue
            path = _LUCYD_DIR / name
            if path.exists():
                for lineno, line in _scan_for_multi_model(path):
                    violations.append(f"{name}:{lineno}: {line}")
        assert not violations, (
            "Multi-model routing patterns found in framework code (AI-005):\n"
            + "\n".join(violations)
        )

    def test_no_multi_model_in_tools(self):
        """tools/*.py must not contain multi-model routing patterns."""
        if not _TOOLS_DIR.exists():
            pytest.skip("tools/ directory not found")
        violations = []
        for py_file in sorted(_TOOLS_DIR.glob("*.py")):
            rel = f"tools/{py_file.name}"
            if rel in _AI005_EXEMPT:
                continue
            for lineno, line in _scan_for_multi_model(py_file):
                violations.append(f"{rel}:{lineno}: {line}")
        assert not violations, (
            "Multi-model routing patterns found in tools (AI-005):\n"
            + "\n".join(violations)
        )

    def test_daemon_uses_singular_provider(self):
        """LucydDaemon must have self.provider (not self.providers)."""
        source = (_LUCYD_DIR / "lucyd.py").read_text(encoding="utf-8")
        assert "self.providers" not in source, (
            "lucyd.py still uses self.providers dict (AI-005)"
        )
        assert "self.provider" in source, (
            "lucyd.py missing self.provider attribute (AI-005)"
        )

    def test_process_message_no_tier_or_model_override(self):
        """_process_message() must not accept tier or model_override params."""
        from lucyd import LucydDaemon

        sig = inspect.signature(LucydDaemon._process_message)
        params = set(sig.parameters.keys())
        assert "tier" not in params, (
            "_process_message has 'tier' parameter (AI-005)"
        )
        assert "model_override" not in params, (
            "_process_message has 'model_override' parameter (AI-005)"
        )

    def test_subagent_tool_no_model_param(self):
        """tool_sessions_spawn must not accept a model parameter."""
        from tools import agents as agents_mod

        sig = inspect.signature(agents_mod.tool_sessions_spawn)
        params = set(sig.parameters.keys())
        assert "model" not in params, (
            "tool_sessions_spawn has 'model' parameter (AI-005)"
        )

    def test_context_builder_no_tier_param(self):
        """ContextBuilder.build() must not accept a tier parameter."""
        from context import ContextBuilder

        sig = inspect.signature(ContextBuilder.build)
        params = set(sig.parameters.keys())
        assert "tier" not in params, (
            "ContextBuilder.build() has 'tier' parameter (AI-005)"
        )
