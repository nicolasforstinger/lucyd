"""Shared fixtures for Lucyd test suite.

All tests use temporary directories and mock objects.
Nothing touches ~/.lucyd/ or the running daemon.
"""

import asyncio
import multiprocessing
import os
import sys
from pathlib import Path
from typing import Any

import pytest

# ─── PostgreSQL test constants ──────────────────────────────────
# Used by all tests that access the database via the ``pool`` fixture.

TEST_CLIENT_ID = "test"
TEST_AGENT_ID = "test_agent"

# ─── Force-exit after session completes ──────────────────────────
# pytest-asyncio leaves a non-daemon thread alive after all tests pass,
# preventing the process from exiting.  os._exit() after pytest's own
# exit hooks have run is the only reliable fix.
#
# Under mutmut (detected via MUTANT_UNDER_TEST env var), os._exit() would
# kill mutmut's own process since it runs pytest.main() in-process.
# Instead, explicitly shut down the asyncio event loop to clear the thread.

_pytest_exit_code = 0


def pytest_sessionfinish(session, exitstatus):
    global _pytest_exit_code
    _pytest_exit_code = exitstatus


def _running_under_mutmut() -> bool:
    # MUTANT_UNDER_TEST is set by mutmut (even to '' for clean tests).
    # MUTMUT_RUNNING is our legacy env var.
    return "MUTANT_UNDER_TEST" in os.environ or bool(os.environ.get("MUTMUT_RUNNING"))


def pytest_unconfigure(config):
    """Force-exit to avoid hanging on stale asyncio threads.

    pytest_unconfigure is the last hook — all output (including the
    summary line) has been written by this point.
    Under mutmut: clean up asyncio loop explicitly instead of os._exit().
    """
    if _running_under_mutmut():
        # Shut down asyncio loop to release dangling threads without killing
        # mutmut's parent process.
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.call_soon_threadsafe(loop.stop)
            elif not loop.is_closed():
                loop.close()
        except RuntimeError:
            pass
        return
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_pytest_exit_code)

# Add project root to path so imports work
_root = Path(__file__).parent.parent
sys.path.insert(0, str(_root))

# Mutmut fallback: when running from mutants/, non-target modules (config,
# agentic, etc.) live in the real project root one level up.
_abs_root = Path(__file__).resolve().parent.parent
if _abs_root.name == "mutants" and str(_abs_root.parent) not in sys.path:
    sys.path.append(str(_abs_root.parent))

# Mutmut workaround: mutmut's trampoline imports mutmut.__main__ which calls
# multiprocessing.set_start_method('fork') at module level. This conflicts
# with pytest-asyncio's event loop. Patch to silently ignore duplicate calls.
_orig_set_start_method = multiprocessing.set_start_method
def _safe_set_start_method(*args, **kwargs):
    try:
        _orig_set_start_method(*args, **kwargs)
    except RuntimeError:
        pass
multiprocessing.set_start_method = _safe_set_start_method


@pytest.fixture
def tmp_sessions(tmp_path):
    """Temp directory acting as sessions_dir."""
    d = tmp_path / "sessions"
    d.mkdir()
    return d


@pytest.fixture
def tmp_workspace(tmp_path):
    """Temp workspace with standard context files."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "SOUL.md").write_text("# Soul\nI am TestAgent.")
    (ws / "AGENTS.md").write_text("# Agents\nBehavior rules.")
    (ws / "USER.md").write_text("# User\nTestUser.")
    (ws / "IDENTITY.md").write_text("# Identity\nTestAgent.")
    (ws / "TOOLS.md").write_text("# Tools\nAvailable tools.")
    (ws / "MEMORY.md").write_text("# Memory\nLong-term memories.")
    return ws


@pytest.fixture
def minimal_toml_data():
    """Minimal valid config data (as parsed dict, not raw TOML)."""
    return {
        "agent": {
            "name": "TestAgent",
            "workspace": "/tmp/test-workspace",
            "context": {
                "stable": ["SOUL.md", "AGENTS.md"],
                "semi_stable": ["MEMORY.md"],
            },
            "skills": {
                "dir": "skills",
                "always_on": ["compute-routing"],
            },
        },
        "http": {
            "enabled": False,
            "host": "127.0.0.1",
            "port": 8100,
            "token_env": "",
            "download_dir": "/tmp/lucyd-http",
            "max_body_bytes": 10485760,
            "max_attachment_bytes": 52428800,
            "rate_limit": 30,
            "rate_window": 60,
            "status_rate_limit": 60,
            "rate_limit_cleanup_threshold": 1000,
        },
        "models": {
            "primary": {
                "provider": "anthropic",
                "model": "claude-opus-4-6",
                "max_tokens": 16384,
                "cost_per_mtok": [5.0, 25.0, 0.5, 6.25],
                "cache_control": True,
                "thinking_enabled": True,
                "thinking_budget": 10000,
            },
            "embeddings": {
                "provider": "openai",
                "model": "text-embedding-3-small",
                "base_url": "https://api.openai.com/v1",
                "cost_per_mtok": [0.02, 0.0, 0.0, 0.0],
                "currency": "USD",
            },
        },
        "memory": {
            "search_top_k": 10,
            "vector_search_limit": 10000,
            "embedding_timeout": 15,
            "consolidation": {
                "enabled": False,
                "confidence_threshold": 0.6,
            },
            "recall": {
                "decay_rate": 0.03,
                "max_facts_in_context": 20,
                "max_dynamic_tokens": 1500,
                "max_episodes_at_start": 3,
                "archive_messages": 20,
                "personality": {
                    "priority_vector": 35,
                    "priority_episodes": 25,
                    "priority_facts": 15,
                    "priority_commitments": 40,
                    "fact_format": "natural",
                    "show_emotional_tone": True,
                    "episode_section_header": "Recent conversations",
                },
            },
            "maintenance": {"stale_threshold_days": 90},
            "indexer": {
                "include_patterns": ["memory/*.md", "MEMORY.md"],
                "exclude_dirs": [],
                "chunk_size_chars": 1600,
                "chunk_overlap_chars": 320,
                "embed_batch_limit": 100,
            },
        },
        "tools": {
            "enabled": ["read", "write", "edit", "exec"],
            "plugins_dir": "plugins.d",
            "output_truncation": 30000,
            "subagent_deny": [],
            "subagent_max_turns": 0,
            "subagent_timeout": 0,
            "exec_timeout": 120,
            "exec_max_timeout": 600,
            "filesystem": {
                "allowed_paths": ["/tmp/"],
                "default_read_limit": 2000,
            },
            "web_search": {"provider": "", "api_key_env": "", "timeout": 15},
            "web_fetch": {"timeout": 15},
        },
        "documents": {
            "enabled": True,
            "max_chars": 30000,
            "max_file_bytes": 10485760,
            "text_extensions": [".txt", ".md", ".csv", ".json", ".xml",
                                ".yaml", ".yml", ".html", ".htm", ".py",
                                ".js", ".ts", ".sh", ".toml", ".ini",
                                ".cfg", ".log", ".sql", ".css"],
        },
        "logging": {"max_bytes": 10485760, "backup_count": 3, "suppress": []},
        "vision": {
            "max_image_bytes": 5242880,
            "max_dimension": 1568,
            "jpeg_quality_steps": [85, 60, 40],
        },
        "behavior": {
            "silent_tokens": ["NO_REPLY"],
            "typing_indicators": True,
            "error_message": "connection error",
            "api_retries": 2,
            "api_retry_base_delay": 2.0,
            "message_retries": 2,
            "message_retry_base_delay": 30.0,
            "agent_timeout_seconds": 600,
            "max_turns_per_message": 50,
            "max_cost_per_message": 0.0,
            "notify_target": "",
            "compaction": {
                "threshold_tokens": 150000,
                "max_tokens": 2048,
                "prompt": "Summarize this conversation for {agent_name}. "
                         "Keep it under {max_tokens} tokens.",
                "keep_recent_pct": 0.33,
                "keep_recent_pct_min": 0.05,
                "keep_recent_pct_max": 0.9,
                "diary_prompt": "Write a log for {date}.",
            },
        },
        "paths": {
            "state_dir": "/tmp/test-state",
            "sessions_dir": "/tmp/test-sessions",
            "log_file": "/tmp/test-lucyd.log",
        },
    }


@pytest.fixture
def tool_registry():
    """ToolRegistry with a sync + async dummy tool registered."""
    from tools import ToolRegistry, ToolSpec

    reg = ToolRegistry(truncation_limit=100)

    def sync_tool(text: str = "default") -> str:
        return f"sync:{text}"

    async def async_tool(text: str = "default") -> str:
        return f"async:{text}"

    reg.register(ToolSpec(
        name="sync_echo",
        description="A sync echo tool",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
        },
        function=sync_tool,
    ))
    reg.register(ToolSpec(
        name="async_echo",
        description="An async echo tool",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
        },
        function=async_tool,
    ))
    return reg


@pytest.fixture
def skill_workspace(tmp_path):
    """tmp_path/skills/ with 2 example SKILL.md files."""
    skills = tmp_path / "skills"
    skills.mkdir()

    # Skill with description
    s1 = skills / "compute-routing"
    s1.mkdir()
    (s1 / "SKILL.md").write_text(
        "---\n"
        "name: compute-routing\n"
        "description: Route compute tasks to appropriate models\n"
        "---\n"
        "# Compute Routing\n\nUse Haiku for routine, Opus for judgment.\n"
    )

    # Skill without description
    s2 = skills / "bare-skill"
    s2.mkdir()
    (s2 / "SKILL.md").write_text(
        "---\n"
        "name: bare-skill\n"
        "---\n"
        "Body of the bare skill.\n"
    )

    return tmp_path


@pytest.fixture
def fs_workspace(tmp_path):
    """tmp_path with test files + filesystem sandboxed to it."""
    from tools import filesystem

    (tmp_path / "hello.txt").write_text("line one\nline two\nline three\n")
    (tmp_path / "long.txt").write_text("x" * 3000 + "\n")
    sub = tmp_path / "subdir"
    sub.mkdir()
    (sub / "nested.txt").write_text("nested content\n")

    filesystem.configure([str(tmp_path)])
    yield tmp_path
    # Restore default after test
    filesystem.configure([])


# ─── PostgreSQL pool fixture ────────────────────────────────────


@pytest.fixture
async def pool() -> Any:
    """asyncpg pool connected to test Postgres, tables cleaned after each test."""
    import asyncpg  # type: ignore[import-untyped]
    import db as lucyd_db

    dsn = os.environ.get(
        "TEST_DATABASE_URL",
        "postgres://lucyd:lucyd@localhost:5432/lucyd_test",
    )
    p: Any = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
    await lucyd_db.ensure_schema(p)
    try:
        yield p
    finally:
        async with p.acquire() as conn:
            for schema in ("sessions", "knowledge", "metering", "search"):
                tables = await conn.fetch(
                    "SELECT tablename FROM pg_tables WHERE schemaname = $1",
                    schema,
                )
                for t in tables:
                    await conn.execute(
                        f"TRUNCATE {schema}.{t['tablename']} CASCADE"
                    )
        await p.close()


@pytest.fixture
async def cost_db(pool: Any) -> Any:
    """MeteringDB backed by the test PostgreSQL pool."""
    from metering import MeteringDB
    return MeteringDB(pool, client_id=TEST_CLIENT_ID, agent_id=TEST_AGENT_ID)
