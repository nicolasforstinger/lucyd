"""Shared fixtures for Lucyd test suite.

All tests use temporary directories and mock objects.
Nothing touches ~/.lucyd/ or the running daemon.
"""

import multiprocessing
import sys
from pathlib import Path

import pytest

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
    (ws / "HEARTBEAT.md").write_text("# Heartbeat\nAutomation tasks.")
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
                "tiers": {
                    "operational": {
                        "stable": ["SOUL.md", "AGENTS.md", "IDENTITY.md"],
                        "semi_stable": ["HEARTBEAT.md"],
                    }
                },
            },
            "skills": {
                "dir": "skills",
                "always_on": ["compute-routing"],
            },
        },
        "channel": {
            "type": "telegram",
            "telegram": {
                "token_env": "LUCYD_TELEGRAM_TOKEN",
                "allow_from": [123456789],
                "contacts": {"TestUser": 123456789},
            },
        },
        "models": {
            "primary": {
                "provider": "anthropic-compat",
                "model": "claude-opus-4-6",
                "max_tokens": 16384,
                "cost_per_mtok": [5.0, 25.0, 0.5],
                "cache_control": True,
                "thinking_enabled": True,
                "thinking_budget": 10000,
            },
            "subagent": {
                "provider": "anthropic-compat",
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 4096,
                "cost_per_mtok": [1.0, 5.0, 0.1],
            },
            "compaction": {
                "provider": "anthropic-compat",
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 4096,
                "cost_per_mtok": [1.0, 5.0, 0.1],
            },
            "embeddings": {
                "provider": "openai-compat",
                "model": "text-embedding-3-small",
                "base_url": "https://api.openai.com/v1",
            },
        },
        "tools": {
            "enabled": ["read", "write", "edit", "exec", "message"],
        },
        "behavior": {
            "compaction": {
                "threshold_tokens": 150000,
            },
        },
        "paths": {
            "state_dir": "/tmp/test-state",
            "sessions_dir": "/tmp/test-sessions",
            "cost_db": "/tmp/test-cost.db",
            "log_file": "/tmp/test-lucyd.log",
        },
    }


@pytest.fixture
def cost_db(tmp_path):
    """Initialized cost tracking DB."""
    import sqlite3

    db_path = tmp_path / "cost.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS costs (
            timestamp INTEGER,
            session_id TEXT,
            model TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_write_tokens INTEGER,
            cost_usd REAL
        )
    """)
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def tool_registry():
    """ToolRegistry with a sync + async dummy tool registered."""
    from tools import ToolRegistry

    reg = ToolRegistry(truncation_limit=100)

    def sync_tool(text: str = "default") -> str:
        return f"sync:{text}"

    async def async_tool(text: str = "default") -> str:
        return f"async:{text}"

    reg.register("sync_echo", "A sync echo tool", {
        "type": "object",
        "properties": {"text": {"type": "string"}},
    }, sync_tool)
    reg.register("async_echo", "An async echo tool", {
        "type": "object",
        "properties": {"text": {"type": "string"}},
    }, async_tool)
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
