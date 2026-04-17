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

    def test_no_bundled_cli_client(self):
        """The daemon ships no CLI client — API-only operation."""
        bin_dir = _LUCYD_DIR / "bin"
        assert not (bin_dir / "lucydctl").exists(), (
            "lucydctl has been removed; the API is the single interface"
        )


# ─── P-023: CLI/API Data Parity ─────────────────────────────────


class TestInterfaceParity:
    """P-023: CLI and HTTP API must return equivalent data."""

    @pytest.mark.asyncio
    async def test_sessions_fields_from_shared_function(self, pool):
        """build_session_info() returns all fields needed by both interfaces."""
        from session import SessionManager, build_session_info

        TEST_CLIENT_ID = "test"
        TEST_AGENT_ID = "test_agent"

        mgr = SessionManager(pool, TEST_CLIENT_ID, TEST_AGENT_ID)
        session = await mgr.get_or_create("parity-user", model="primary")

        session.messages = [
            {"role": "user", "content": "test"},
            {"role": "agent", "text": "ok", "usage": {
                "input_tokens": 1000, "output_tokens": 200,
                "cache_read_tokens": 300, "cache_write_tokens": 100,
            }},
        ]
        session.compaction_count = 1
        await session.save_state()

        info = await build_session_info(
            pool, TEST_CLIENT_ID, TEST_AGENT_ID,
            session_id=session.id,
            session=session,
            max_context_tokens=50000,
        )

        # All required fields present
        required_fields = {
            "session_id", "context_tokens", "context_pct", "cost",
            "message_count", "compaction_count",
        }
        assert required_fields.issubset(info.keys()), (
            f"Missing fields: {required_fields - info.keys()}"
        )

    @pytest.mark.asyncio
    async def test_cost_fields_include_cache_tokens(self, pool):
        """HTTP /cost returns cache_read_tokens and cache_write_tokens."""
        from metering import MeteringDB
        from dataclasses import dataclass

        TEST_CLIENT_ID = "test"
        TEST_AGENT_ID = "test_agent"

        @dataclass
        class _Usage:
            input_tokens: int = 1000
            output_tokens: int = 500
            cache_read_tokens: int = 300
            cache_write_tokens: int = 100

        metering_db = MeteringDB(pool, client_id=TEST_CLIENT_ID, agent_id=TEST_AGENT_ID)
        await metering_db.record(
            session_id="s1", model="m", provider="",
            usage=_Usage(), cost_rates=[1.0, 1.0, 0.1],
        )

        result = await metering_db.get_records()

        assert len(result["records"]) == 1
        rec = result["records"][0]
        assert rec["cache_read_tokens"] == 300
        assert rec["cache_write_tokens"] == 100

    @pytest.mark.asyncio
    async def test_cli_and_api_session_data_equivalent(self, pool):
        """CLI and HTTP API paths produce the same data for the same session.

        Functional parity: set up session state, then verify build_session_info()
        (used by both CLI and HTTP) returns consistent values whether called with
        a live Session object (HTTP path) or without one (CLI path, reads from DB).
        """
        from session import SessionManager, build_session_info

        TEST_CLIENT_ID = "test"
        TEST_AGENT_ID = "test_agent"

        mgr = SessionManager(pool, TEST_CLIENT_ID, TEST_AGENT_ID)
        session = await mgr.get_or_create("parity-eq-user", model="primary")

        # Populate with known state
        session.messages = [
            {"role": "user", "content": "hello"},
            {"role": "agent", "text": "hi", "usage": {
                "input_tokens": 2000, "output_tokens": 500,
                "cache_read_tokens": 800, "cache_write_tokens": 200,
            }},
        ]
        session.compaction_count = 2
        await session.save_state()

        # HTTP path: live session object
        http_info = await build_session_info(
            pool, TEST_CLIENT_ID, TEST_AGENT_ID,
            session_id=session.id,
            session=session,
            max_context_tokens=100000,
        )

        # CLI path: no live session, reads from DB
        cli_info = await build_session_info(
            pool, TEST_CLIENT_ID, TEST_AGENT_ID,
            session_id=session.id,
            max_context_tokens=100000,
        )

        # Core data fields must match
        for field in ("session_id", "context_tokens", "context_pct",
                      "message_count", "compaction_count"):
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
        """process_message() must not accept tier or model_override params."""
        from pipeline import MessagePipeline

        sig = inspect.signature(MessagePipeline.process_message)
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
