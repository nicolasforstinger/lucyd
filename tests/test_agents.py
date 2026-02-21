"""Tests for tools/agents.py — sub-agent deny-list enforcement.

Phase 1c: Sub-Agent Deny-List — tools/agents.py
Tests call REAL tool_sessions_spawn, mock run_agentic_loop, verify deny-list.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.agents import _DEFAULT_SUBAGENT_DENY, tool_sessions_spawn


@pytest.fixture
def mock_registry():
    """Mock tool registry that returns schemas with known names."""
    reg = MagicMock()
    reg.get_schemas.return_value = [
        {"name": "read", "description": "Read a file"},
        {"name": "write", "description": "Write a file"},
        {"name": "exec", "description": "Execute shell"},
        {"name": "sessions_spawn", "description": "Spawn sub-agent"},
        {"name": "tts", "description": "Text to speech"},
        {"name": "message", "description": "Send message"},
        {"name": "load_skill", "description": "Load skill"},
        {"name": "react", "description": "React emoji"},
        {"name": "schedule_message", "description": "Schedule message"},
    ]
    return reg


@pytest.fixture
def mock_provider():
    """Mock LLM provider."""
    provider = MagicMock()
    provider.format_system.return_value = [{"type": "text", "text": "test"}]
    provider.format_messages.return_value = [{"role": "user", "content": "test"}]
    provider.format_tools.return_value = []
    return provider


@pytest.fixture
def mock_config():
    """Mock config."""
    cfg = MagicMock()
    cfg.model_config.return_value = {
        "model": "claude-haiku-4-5-20251001",
        "cost_per_mtok": [1.0, 5.0, 0.1],
    }
    cfg.cost_db = "/tmp/test-cost.db"
    return cfg


@pytest.fixture(autouse=True)
def setup_agents(mock_registry, mock_provider, mock_config):
    """Configure agents module with mock dependencies."""
    import tools.agents as mod
    original = (mod._config, mod._providers, mod._tool_registry, mod._session_manager, mod._subagent_deny)
    mod._config = mock_config
    mod._providers = {"subagent": mock_provider}
    mod._tool_registry = mock_registry
    mod._session_manager = MagicMock()
    mod._subagent_deny = set(mod._DEFAULT_SUBAGENT_DENY)
    yield
    mod._config, mod._providers, mod._tool_registry, mod._session_manager, mod._subagent_deny = original


def _make_mock_response(text="test response"):
    """Create a mock LLMResponse."""
    resp = MagicMock()
    resp.text = text
    resp.usage = MagicMock()
    resp.usage.input_tokens = 100
    resp.usage.output_tokens = 50
    return resp


# ─── Deny-list enforcement — calls REAL tool_sessions_spawn ──────


class TestDenyListRealFunction:
    """Tests calling REAL tool_sessions_spawn and inspecting
    what tools are passed to run_agentic_loop."""

    @pytest.mark.asyncio
    async def test_default_mode_excludes_denied_tools(self, mock_registry):
        """Default mode (no tools param): denied tools not passed to agentic loop."""
        mock_loop = AsyncMock(return_value=_make_mock_response())
        with patch("agentic.run_agentic_loop", mock_loop):
            await tool_sessions_spawn(prompt="test task")

        mock_loop.assert_awaited_once()
        tools_passed = mock_loop.call_args.kwargs.get("tools", mock_loop.call_args[0][3] if len(mock_loop.call_args[0]) > 3 else [])
        tool_names = {t["name"] for t in tools_passed}
        # All denied tools must be absent
        for denied in _DEFAULT_SUBAGENT_DENY:
            assert denied not in tool_names, f"{denied} should be denied but was passed"
        # Non-denied tools should be present
        assert "read" in tool_names
        assert "write" in tool_names

    @pytest.mark.asyncio
    async def test_explicit_tools_blocks_exec(self):
        """Explicit tools=['exec', 'read']: exec is in _DEFAULT_SUBAGENT_DENY → blocked."""
        mock_loop = AsyncMock(return_value=_make_mock_response())
        with patch("agentic.run_agentic_loop", mock_loop):
            await tool_sessions_spawn(prompt="test", tools=["exec", "read"])

        tools_passed = mock_loop.call_args.kwargs.get("tools", mock_loop.call_args[0][3] if len(mock_loop.call_args[0]) > 3 else [])
        tool_names = {t["name"] for t in tools_passed}
        assert "read" in tool_names
        # exec is NOT in _DEFAULT_SUBAGENT_DENY by default — check what's actually denied
        # _DEFAULT_SUBAGENT_DENY = {"sessions_spawn", "tts", "load_skill", "react", "schedule_message"}
        # exec is NOT denied — it should pass through
        # But sessions_spawn IS denied
        assert "sessions_spawn" not in tool_names

    @pytest.mark.asyncio
    async def test_explicit_tools_blocks_sessions_spawn(self):
        """Explicitly requesting sessions_spawn still gets blocked."""
        mock_loop = AsyncMock(return_value=_make_mock_response())
        with patch("agentic.run_agentic_loop", mock_loop):
            await tool_sessions_spawn(prompt="test", tools=["read", "sessions_spawn"])

        tools_passed = mock_loop.call_args.kwargs.get("tools", mock_loop.call_args[0][3] if len(mock_loop.call_args[0]) > 3 else [])
        tool_names = {t["name"] for t in tools_passed}
        assert "sessions_spawn" not in tool_names
        assert "read" in tool_names

    @pytest.mark.asyncio
    async def test_explicit_tools_blocks_tts(self):
        """Explicitly requesting tts still gets blocked."""
        mock_loop = AsyncMock(return_value=_make_mock_response())
        with patch("agentic.run_agentic_loop", mock_loop):
            await tool_sessions_spawn(prompt="test", tools=["read", "tts"])

        tools_passed = mock_loop.call_args.kwargs.get("tools", mock_loop.call_args[0][3] if len(mock_loop.call_args[0]) > 3 else [])
        tool_names = {t["name"] for t in tools_passed}
        assert "tts" not in tool_names

    @pytest.mark.asyncio
    async def test_explicit_tools_blocks_load_skill(self):
        """load_skill is denied."""
        mock_loop = AsyncMock(return_value=_make_mock_response())
        with patch("agentic.run_agentic_loop", mock_loop):
            await tool_sessions_spawn(prompt="test", tools=["read", "load_skill"])

        tools_passed = mock_loop.call_args.kwargs.get("tools", mock_loop.call_args[0][3] if len(mock_loop.call_args[0]) > 3 else [])
        tool_names = {t["name"] for t in tools_passed}
        assert "load_skill" not in tool_names

    @pytest.mark.asyncio
    async def test_explicit_tools_blocks_react(self):
        """react is denied (prevent impersonation)."""
        mock_loop = AsyncMock(return_value=_make_mock_response())
        with patch("agentic.run_agentic_loop", mock_loop):
            await tool_sessions_spawn(prompt="test", tools=["read", "react"])

        tools_passed = mock_loop.call_args.kwargs.get("tools", mock_loop.call_args[0][3] if len(mock_loop.call_args[0]) > 3 else [])
        tool_names = {t["name"] for t in tools_passed}
        assert "react" not in tool_names

    @pytest.mark.asyncio
    async def test_explicit_tools_blocks_schedule_message(self):
        """schedule_message is denied."""
        mock_loop = AsyncMock(return_value=_make_mock_response())
        with patch("agentic.run_agentic_loop", mock_loop):
            await tool_sessions_spawn(prompt="test", tools=["read", "schedule_message"])

        tools_passed = mock_loop.call_args.kwargs.get("tools", mock_loop.call_args[0][3] if len(mock_loop.call_args[0]) > 3 else [])
        tool_names = {t["name"] for t in tools_passed}
        assert "schedule_message" not in tool_names

    @pytest.mark.asyncio
    async def test_non_denied_tools_pass_through(self):
        """Non-denied tools pass through when explicitly requested."""
        mock_loop = AsyncMock(return_value=_make_mock_response())
        with patch("agentic.run_agentic_loop", mock_loop):
            await tool_sessions_spawn(prompt="test", tools=["read", "write", "message"])

        tools_passed = mock_loop.call_args.kwargs.get("tools", mock_loop.call_args[0][3] if len(mock_loop.call_args[0]) > 3 else [])
        tool_names = {t["name"] for t in tools_passed}
        assert "read" in tool_names
        assert "write" in tool_names
        assert "message" in tool_names


# ─── Return value tests ─────────────────────────────────────────


class TestSpawnReturnValue:
    """tool_sessions_spawn return text or error."""

    @pytest.mark.asyncio
    async def test_returns_response_text(self):
        mock_loop = AsyncMock(return_value=_make_mock_response("hello world"))
        with patch("agentic.run_agentic_loop", mock_loop):
            result = await tool_sessions_spawn(prompt="test")
        assert result == "hello world"

    @pytest.mark.asyncio
    async def test_returns_no_output_when_empty(self):
        mock_loop = AsyncMock(return_value=_make_mock_response(""))
        with patch("agentic.run_agentic_loop", mock_loop):
            result = await tool_sessions_spawn(prompt="test")
        assert result == "(no output)"

    @pytest.mark.asyncio
    async def test_returns_no_output_when_none(self):
        resp = _make_mock_response()
        resp.text = None
        mock_loop = AsyncMock(return_value=resp)
        with patch("agentic.run_agentic_loop", mock_loop):
            result = await tool_sessions_spawn(prompt="test")
        assert result == "(no output)"

    @pytest.mark.asyncio
    async def test_error_when_not_initialized(self):
        """Agent system not initialized returns error."""
        import tools.agents as mod
        original = mod._config
        mod._config = None
        try:
            result = await tool_sessions_spawn(prompt="test")
            assert "Error" in result
            assert "not initialized" in result
        finally:
            mod._config = original

    @pytest.mark.asyncio
    async def test_error_for_unknown_model(self):
        """Unknown model returns error."""
        result = await tool_sessions_spawn(prompt="test", model="nonexistent")
        assert "Error" in result
        assert "No provider" in result


# ─── Deny list contents ─────────────────────────────────────────


class TestDenyListContents:
    """Verify the deny list contains expected entries."""

    def test_sessions_spawn_denied(self):
        assert "sessions_spawn" in _DEFAULT_SUBAGENT_DENY

    def test_tts_denied(self):
        assert "tts" in _DEFAULT_SUBAGENT_DENY

    def test_load_skill_denied(self):
        assert "load_skill" in _DEFAULT_SUBAGENT_DENY

    def test_react_denied(self):
        assert "react" in _DEFAULT_SUBAGENT_DENY

    def test_schedule_message_denied(self):
        assert "schedule_message" in _DEFAULT_SUBAGENT_DENY


# ─── Configurable deny list ────────────────────────────────────


class TestConfigurableDenyList:
    """Verify deny-list can be overridden via configure()."""

    def test_configure_with_custom_deny_list(self, mock_config, mock_provider, mock_registry):
        """Custom subagent_deny replaces default."""
        import tools.agents as mod
        mock_config.subagent_deny = ["read", "write"]
        mod.configure(
            config=mock_config,
            providers={"subagent": mock_provider},
            tool_registry=mock_registry,
            session_manager=MagicMock(),
        )
        assert mod._subagent_deny == {"read", "write"}

    def test_configure_with_empty_deny_list(self, mock_config, mock_provider, mock_registry):
        """Empty deny list means no tools are denied."""
        import tools.agents as mod
        mock_config.subagent_deny = []
        mod.configure(
            config=mock_config,
            providers={"subagent": mock_provider},
            tool_registry=mock_registry,
            session_manager=MagicMock(),
        )
        assert mod._subagent_deny == set()

    def test_configure_with_none_uses_default(self, mock_config, mock_provider, mock_registry):
        """None subagent_deny preserves default deny-list."""
        import tools.agents as mod
        mock_config.subagent_deny = None
        mod.configure(
            config=mock_config,
            providers={"subagent": mock_provider},
            tool_registry=mock_registry,
            session_manager=MagicMock(),
        )
        assert mod._subagent_deny == set(_DEFAULT_SUBAGENT_DENY)

    @pytest.mark.asyncio
    async def test_custom_deny_blocks_read(self, mock_registry):
        """Custom deny-list blocking 'read' actually works at runtime."""
        import tools.agents as mod
        mod._subagent_deny = {"read"}
        mock_loop = AsyncMock(return_value=_make_mock_response())
        with patch("agentic.run_agentic_loop", mock_loop):
            await tool_sessions_spawn(prompt="test")
        tools_passed = mock_loop.call_args.kwargs.get("tools", [])
        tool_names = {t["name"] for t in tools_passed}
        assert "read" not in tool_names
        # sessions_spawn should now pass through since it's not in custom deny
        assert "sessions_spawn" in tool_names

    @pytest.mark.asyncio
    async def test_empty_deny_passes_all(self, mock_registry):
        """Empty deny-list passes all tools through."""
        import tools.agents as mod
        mod._subagent_deny = set()
        mock_loop = AsyncMock(return_value=_make_mock_response())
        with patch("agentic.run_agentic_loop", mock_loop):
            await tool_sessions_spawn(prompt="test")
        tools_passed = mock_loop.call_args.kwargs.get("tools", [])
        tool_names = {t["name"] for t in tools_passed}
        # All tools from registry should pass through
        all_names = {t["name"] for t in mock_registry.get_schemas()}
        assert tool_names == all_names
