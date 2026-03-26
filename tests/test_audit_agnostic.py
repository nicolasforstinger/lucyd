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
        """tools/*.py must not reference specific channel types."""
        if not _TOOLS_DIR.exists():
            pytest.skip("tools/ directory not found")
        violations = []
        for py_file in sorted(_TOOLS_DIR.glob("*.py")):
            for lineno, line in _scan_file(py_file):
                violations.append(f"tools/{py_file.name}:{lineno}: {line}")
        assert not violations, (
            "tools/ channel references found (P-022):\n"
            + "\n".join(violations)
        )

    def test_cli_is_pure_http_client(self):
        """lucydctl is a thin HTTP client — no direct file/DB access."""
        import importlib.util
        import inspect
        from importlib.machinery import SourceFileLoader

        bin_dir = _LUCYD_DIR / "bin"
        loader = SourceFileLoader("lucydctl", str(bin_dir / "lucydctl"))
        spec = importlib.util.spec_from_loader("lucydctl", loader)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        source = inspect.getsource(mod)
        assert "sqlite3" not in source, "CLI must not access SQLite directly"


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
            "session_id", "context_tokens", "context_pct", "cost",
            "message_count", "compaction_count", "log_files", "log_bytes",
        }
        assert required_fields.issubset(info.keys()), (
            f"Missing fields: {required_fields - info.keys()}"
        )

    def test_cost_fields_include_cache_tokens(self, tmp_path):
        """HTTP /cost returns cache_read_tokens and cache_write_tokens."""
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
                "max_attachment_bytes": 52428800,
                "rate_limit": 30, "rate_window": 60, "status_rate_limit": 60,
                "rate_limit_cleanup_threshold": 1000,
            },
            "models": {"primary": {"provider": "anthropic-compat", "model": "m"}},
            "memory": {
                "db": "", "search_top_k": 10, "vector_search_limit": 10000,
                "embedding_timeout": 15,
                "consolidation": {"enabled": False, "confidence_threshold": 0.6},
                "recall": {
                    "decay_rate": 0.03, "max_facts_in_context": 20, "max_dynamic_tokens": 1500, "max_episodes_at_start": 3, "archive_messages": 20,
                    "personality": {"priority_vector": 35, "priority_episodes": 25, "priority_facts": 15, "priority_commitments": 40,
                                   "fact_format": "natural", "show_emotional_tone": True, "episode_section_header": "Recent conversations"},
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
            },
            "documents": {"enabled": True, "max_chars": 30000, "max_file_bytes": 10485760,
                          "text_extensions": [".txt", ".md"]},
            "logging": {"suppress": []},
            "vision": {"max_image_bytes": 5242880, "max_dimension": 1568,
                       "jpeg_quality_steps": [85, 60, 40],
                       },
            "behavior": {
                "silent_tokens": ["NO_REPLY"], "typing_indicators": True, "error_message": "error",
                "sqlite_timeout": 30,
                "api_retries": 2, "api_retry_base_delay": 2.0, "message_retries": 2, "message_retry_base_delay": 30.0,
                "agent_timeout_seconds": 600,
                "max_turns_per_message": 50, "max_cost_per_message": 0.0,
                "notify_target": "",
                "compaction": {
                    "threshold_tokens": 150000, "max_tokens": 2048,
                    "prompt": "Summarize for {agent_name}.", "keep_recent_pct": 0.33,
                    "keep_recent_pct_min": 0.05, "keep_recent_pct_max": 0.9,
                    "diary_prompt": "Write a log for {date}.",
                },
            },
            "paths": {
                "state_dir": str(tmp_path / "state"),
                "sessions_dir": str(tmp_path / "sessions"),
                "log_file": str(tmp_path / "lucyd.log"),
            },
        }
        (tmp_path / "ws").mkdir()
        (tmp_path / "ws" / "SOUL.md").write_text("# Test")
        (tmp_path / "state").mkdir()
        config = Config(cfg_data)
        daemon = LucydDaemon(config)
        daemon.provider = None

        # Initialize metering DB with test data
        from metering import MeteringDB
        from dataclasses import dataclass

        @dataclass
        class _Usage:
            input_tokens: int = 1000
            output_tokens: int = 500
            cache_read_tokens: int = 300
            cache_write_tokens: int = 100

        metering_path = str(tmp_path / "metering.db")
        daemon.metering_db = MeteringDB(metering_path, agent_id="test")
        daemon.metering_db.record(
            session_id="s1", model="m", provider="",
            usage=_Usage(), cost_rates=[1.0, 1.0, 0.1],
        )

        result = daemon.metering_db.get_records()

        assert len(result["records"]) == 1
        rec = result["records"][0]
        assert rec["cache_read_tokens"] == 300
        assert rec["cache_write_tokens"] == 100

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
        session.save_state()

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

    def test_cost_endpoint_uses_get_records(self):
        """HTTP /cost uses MeteringDB.get_records() for cost data."""
        import inspect

        from api import HTTPApi
        source = inspect.getsource(HTTPApi._handle_cost)
        assert "get_records" in source, "HTTP must use MeteringDB.get_records()"


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

_AI005_EXEMPT = {"config.py", "lucyd.py"}


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
        """LucydDaemon must have self.provider as primary, with get_provider() routing.

        Architecture: self.provider is the primary instance. self._providers is the
        internal routing cache. get_provider("primary") must return self.provider.
        The old self.providers (public dict) is banned.
        """
        source = (_LUCYD_DIR / "lucyd.py").read_text(encoding="utf-8")
        # Old public dict must not exist
        assert re.search(r"\bself\.providers\b", source) is None, (
            "lucyd.py still uses self.providers dict (AI-005)"
        )
        # Primary provider attribute must exist
        assert "self.provider" in source, (
            "lucyd.py missing self.provider attribute (AI-005)"
        )
        # get_provider routing method must exist
        assert "def get_provider" in source, (
            "lucyd.py missing get_provider() method (AI-005)"
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

    def test_get_provider_returns_primary_by_default(self):
        """get_provider("compaction") returns self.provider when no routing override.

        When no [models.routing.compaction] is configured, get_provider() for any
        role must fall back to the primary provider — not create a new instance
        or return None.
        """
        source = (_LUCYD_DIR / "lucyd.py").read_text(encoding="utf-8")
        # get_provider must have a fallback to self.provider
        assert "return self.provider" in source, (
            "get_provider() missing fallback to self.provider (AI-005)"
        )


# ─── Config Schema Integrity ──────────────────────────────────────


class TestConfigSchemaIntegrity:
    """Verify that config properties accessed in lucyd.py are backed by _SCHEMA or @property."""

    def test_config_properties_exist_in_schema_or_class(self):
        """Every self.config.X in lucyd.py must resolve to a _SCHEMA entry or @property on Config.

        Catches: typos in property names, properties removed from config.py but still
        referenced in lucyd.py, and undeclared config access that would raise AttributeError
        at runtime.
        """
        import ast

        # Collect all _SCHEMA keys (annotated assignment: _SCHEMA: dict[str, tuple] = {...})
        config_source = (_LUCYD_DIR / "config.py").read_text(encoding="utf-8")
        config_tree = ast.parse(config_source)
        schema_keys: set[str] = set()
        for node in ast.walk(config_tree):
            if (isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name)
                    and node.target.id == "_SCHEMA"
                    and isinstance(node.value, ast.Dict)):
                for key in node.value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        schema_keys.add(key.value)

        # Collect all @property names on Config class
        property_names: set[str] = set()
        for node in ast.walk(config_tree):
            if isinstance(node, ast.ClassDef) and node.name == "Config":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and any(
                        isinstance(d, ast.Name) and d.id == "property"
                        for d in item.decorator_list
                    ):
                        property_names.add(item.name)

        # Collect all methods on Config class (model_config, raw, etc.)
        method_names: set[str] = set()
        for node in ast.walk(config_tree):
            if isinstance(node, ast.ClassDef) and node.name == "Config":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and not any(
                        isinstance(d, ast.Name) and d.id == "property"
                        for d in item.decorator_list
                    ):
                        method_names.add(item.name)

        all_valid = schema_keys | property_names | method_names

        # Find all self.config.X references in lucyd.py
        lucyd_source = (_LUCYD_DIR / "lucyd.py").read_text(encoding="utf-8")
        # Match self.config.attr_name (not self.config.method() — those are methods)
        config_refs = set(re.findall(r"self\.config\.([a-z_][a-z0-9_]*)", lucyd_source))

        # Filter out known non-property access patterns (dict-like, internal)
        missing = config_refs - all_valid
        assert not missing, (
            "lucyd.py references config properties not in _SCHEMA or @property:\n"
            + "\n".join(f"  - self.config.{name}" for name in sorted(missing))
        )


# ─── SessionManager Encapsulation ─────────────────────────────────


class TestSessionManagerEncapsulation:
    """P-044: Verify lucyd.py does not access SessionManager internals."""

    def test_no_session_mgr_private_access(self):
        """lucyd.py must not access session_mgr._sessions or session_mgr._index.

        These are implementation details of SessionManager. The coordinator
        should only use public methods: get(), create(), close(), list_ids().
        """
        source = (_LUCYD_DIR / "lucyd.py").read_text(encoding="utf-8")
        violations = []
        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "session_mgr._sessions" in line:
                violations.append(f"line {i}: {stripped}")
            if "session_mgr._index" in line:
                violations.append(f"line {i}: {stripped}")
        assert not violations, (
            "lucyd.py accesses SessionManager internals (P-044):\n"
            + "\n".join(violations)
        )


# ─── Stale Test Artifacts (P-045) ────────────────────────────────


class TestStaleTestArtifacts:
    """P-045: Detect dead patches, orphaned set_X imports, and phantom mocks.

    Tests that interact with production code via removed/no-op interfaces
    report green while verifying nothing. These checks catch them.
    """

    def test_no_set_x_imports_in_tests(self):
        """Tests must not import set_X wrapper functions from tool modules.

        All tool configuration goes through configure(). The old set_X()
        functions were removed. Importing them means the test is using
        a deleted API or a re-added wrapper that shouldn't exist.
        """
        import re as _re
        violations = []
        tests_dir = _LUCYD_DIR / "tests"
        for py_file in sorted(tests_dir.glob("*.py")):
            if py_file.name == "__pycache__":
                continue
            try:
                source = py_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for i, line in enumerate(source.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if _re.search(r"from tools\.\w+ import .* set_", line):
                    violations.append(f"{py_file.name}:{i}: {line.strip()}")
        assert not violations, (
            "Tests import set_X() functions from tool modules (P-045):\n"
            + "\n".join(violations)
            + "\nMigrate to configure() calls."
        )

    def test_no_set_x_functions_in_tools(self):
        """Tool modules must not define set_X() public functions.

        All dependencies go through configure(). If a set_X() function
        exists, it's either a backward-compat wrapper (remove it) or
        a new setter that should be a configure() parameter.
        """
        import re as _re
        violations = []
        tools_dir = _LUCYD_DIR / "tools"
        for py_file in sorted(tools_dir.glob("*.py")):
            if py_file.name.startswith("__"):
                continue
            try:
                source = py_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for i, line in enumerate(source.splitlines(), 1):
                if _re.match(r"^def set_[a-z]", line):
                    violations.append(f"{py_file.name}:{i}: {line.strip()}")
        assert not violations, (
            "Tool modules define set_X() functions (P-045):\n"
            + "\n".join(violations)
            + "\nMerge into configure() parameters."
        )

    def test_no_dead_patches_for_removed_functions(self):
        """Tests must not patch functions that are no-ops or don't exist.

        Specifically checks for patches targeting known-removed functions.
        This list is maintained as functions are removed during refactoring.
        """
        # Functions that were removed or replaced with no-ops
        _REMOVED_TARGETS = {
            "tools.status.set_current_session",
            "tools.memory_read.set_memory",
            "tools.memory_read.set_structured_memory",
            "skills.set_skill_loader",
        }
        violations = []
        tests_dir = _LUCYD_DIR / "tests"
        for py_file in sorted(tests_dir.glob("*.py")):
            try:
                source = py_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for i, line in enumerate(source.splitlines(), 1):
                for target in _REMOVED_TARGETS:
                    if f'patch("{target}"' in line or f"patch('{target}'" in line:
                        violations.append(f"{py_file.name}:{i}: patch(\"{target}\")")
        assert not violations, (
            "Tests patch removed/no-op functions (P-045):\n"
            + "\n".join(violations)
            + "\nRemove the stale patches."
        )
