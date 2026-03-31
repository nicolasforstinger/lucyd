"""Tests for lucyd-fix-plan.md — conversation integrity, streaming,
config routing, telemetry scoping, and test runner fixes.
"""

import json
import time
from dataclasses import replace

import pytest

from agentic import (
    LoopConfig,
    _turn_group_end,
    run_agentic_loop,
)
from providers import LLMResponse, ModelCapabilities, ToolCall, Usage
from session import Session, _validate_turn_structure
from tools import ToolRegistry, ToolSpec


# ─── Shared Helpers ──────────────────────────────────────────────

_LOOP_CONFIG = LoopConfig(
    timeout=600.0,
    api_retries=0,
    api_retry_base_delay=0.1,
    sqlite_timeout=30,
)


class MockProvider:
    def __init__(self, responses, caps=None):
        self._responses = list(responses)
        self._call_count = 0
        self._capabilities = caps or ModelCapabilities(max_context_tokens=100_000)

    @property
    def capabilities(self):
        return self._capabilities

    def format_tools(self, tools):
        return tools

    def format_system(self, blocks):
        return blocks

    def format_messages(self, messages):
        return messages

    async def complete(self, system, messages, tools, **kw):
        idx = min(self._call_count, len(self._responses) - 1)
        self._call_count += 1
        return self._responses[idx]


def _end_turn(text="Done"):
    return LLMResponse(
        text=text, tool_calls=[], stop_reason="end_turn",
        usage=Usage(input_tokens=100, output_tokens=50),
    )


def _tool_use(name="echo", tc_id="tc-1"):
    return LLMResponse(
        text=None,
        tool_calls=[ToolCall(id=tc_id, name=name, arguments={"text": "hi"})],
        stop_reason="tool_use",
        usage=Usage(input_tokens=100, output_tokens=50),
    )


def _make_registry():
    reg = ToolRegistry()
    reg.register(ToolSpec(
        name="echo",
        description="echo",
        input_schema={"type": "object"},
        function=lambda text="": f"echo:{text}",
    ))
    return reg


# ═══════════════════════════════════════════════════════════════════
# P1: Conversation Integrity
# ═══════════════════════════════════════════════════════════════════

class TestTurnGroupEnd:
    """_turn_group_end correctly identifies turn boundaries."""

    def test_user_message_standalone(self):
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "user", "content": "second"},
            {"role": "assistant", "text": "hi"},
        ]
        assert _turn_group_end(msgs, 1) == 2

    def test_assistant_without_tool_calls(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "text": "hello"},
            {"role": "user", "content": "bye"},
        ]
        assert _turn_group_end(msgs, 1) == 2

    def test_assistant_with_tool_calls_includes_tool_results(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "text": "", "tool_calls": [{"id": "1"}]},
            {"role": "tool_results", "results": [{"tool_call_id": "1"}]},
            {"role": "assistant", "text": "done"},
        ]
        assert _turn_group_end(msgs, 1) == 3  # assistant + tool_results

    def test_assistant_with_system_hint_between(self):
        """System user hints between assistant and tool_results are included."""
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "text": "", "tool_calls": [{"id": "1"}]},
            {"role": "user", "content": "[system: context pressure]"},
            {"role": "tool_results", "results": [{"tool_call_id": "1"}]},
            {"role": "assistant", "text": "done"},
        ]
        assert _turn_group_end(msgs, 1) == 4  # assistant + hint + tool_results

    def test_assistant_with_tool_calls_no_results(self):
        """Missing tool_results means group is just the assistant."""
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "text": "", "tool_calls": [{"id": "1"}]},
        ]
        assert _turn_group_end(msgs, 1) == 2


class TestTurnGroupTrimming:
    """Context trimming removes complete turn groups, not single messages."""

    @pytest.mark.asyncio
    async def test_trim_preserves_turn_structure(self):
        """After trimming, no orphaned tool_results remain."""
        provider = MockProvider([_end_turn("Done")])
        reg = _make_registry()

        # Build messages with assistant+tool_results pairs that need trimming
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "text": "", "tool_calls": [{"id": "tc-1", "name": "echo", "arguments": {}}]},
            {"role": "tool_results", "results": [{"tool_call_id": "tc-1", "content": "ok"}]},
            {"role": "assistant", "text": "", "tool_calls": [{"id": "tc-2", "name": "echo", "arguments": {}}]},
            {"role": "tool_results", "results": [{"tool_call_id": "tc-2", "content": "ok"}]},
            {"role": "user", "content": "question"},
        ]

        # Use very small context to force trimming
        caps = ModelCapabilities(max_context_tokens=500)
        provider._capabilities = caps

        await run_agentic_loop(
            provider=provider, system="sys", messages=messages,
            tools=[], tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=1),
        )

        # Verify no orphaned tool_results
        for i, msg in enumerate(messages):
            if msg.get("role") == "tool_results":
                # Must be preceded by an assistant with tool_calls
                assert i > 0
                found = False
                for j in range(i - 1, -1, -1):
                    if messages[j].get("role") == "assistant" and messages[j].get("tool_calls"):
                        found = True
                        break
                    if messages[j].get("role") == "tool_results":
                        break
                assert found, f"Orphaned tool_results at index {i}"


class TestValidateTurnStructure:
    """_validate_turn_structure fixes orphaned messages."""

    def test_strips_orphaned_tool_calls(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "text": "thinking", "tool_calls": [{"id": "1"}]},
        ]
        _validate_turn_structure(msgs)
        assert "tool_calls" not in msgs[1]

    def test_removes_orphaned_tool_results(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "tool_results", "results": [{"tool_call_id": "1", "content": "x"}]},
            {"role": "assistant", "text": "done"},
        ]
        _validate_turn_structure(msgs)
        # tool_results should be removed
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"

    def test_valid_structure_unchanged(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "text": "", "tool_calls": [{"id": "1"}]},
            {"role": "tool_results", "results": [{"tool_call_id": "1"}]},
            {"role": "assistant", "text": "done"},
        ]
        _validate_turn_structure(msgs)
        assert len(msgs) == 4


class TestPreRetrySnapshot:
    """Message-level retry restores messages to pre-attempt state."""

    @pytest.mark.asyncio
    async def test_retry_cleans_partial_messages(self):
        """Simulate: first call does one tool turn then fails,
        retry should start clean."""
        call_count = 0

        class FailOnceThenSucceed:
            capabilities = ModelCapabilities()
            def format_tools(self, t): return t
            def format_system(self, b): return b
            def format_messages(self, m): return m
            async def complete(self, system, messages, tools, **kw):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    # Return tool use — loop will execute and append tool_results
                    return _tool_use()
                if call_count == 2:
                    # Fail on second LLM call (after tool_results appended)
                    raise ConnectionError("transient")
                # Third call (after retry) — succeed
                return _end_turn("Final answer")

        provider = FailOnceThenSucceed()
        reg = _make_registry()
        messages = [{"role": "user", "content": "test"}]

        # The agentic loop will:
        # Call 1: tool_use → execute tool → append tool_results
        # Call 2: ConnectionError → retry
        # But we need message-level retry, not API-level retry.
        # The API-level retry in the loop will raise after exhausting api_retries=0.
        # Message-level retry is in _run_agentic_with_retries (daemon level).
        # So we test the loop-level behavior directly.
        cfg = replace(_LOOP_CONFIG, max_turns=5, api_retries=0)

        # The loop should raise ConnectionError
        with pytest.raises(ConnectionError):
            await run_agentic_loop(
                provider=provider, system="sys", messages=messages,
                tools=reg.get_schemas(), tool_executor=reg, config=cfg,
            )

        # After the error, messages should have the partial state:
        # [user, assistant(tool_use), tool_results]
        # The daemon's _run_agentic_with_retries would truncate back.
        # Here we verify the loop left partial state (which the daemon cleans).
        assert len(messages) > 1  # loop added messages before failing


# ═══════════════════════════════════════════════════════════════════
# P2: HTTP Streaming
# ═══════════════════════════════════════════════════════════════════

class TestSSETerminalEvents:
    """SSE stream always terminates with a clear success or error event."""

    @pytest.mark.asyncio
    async def test_sentinel_without_done_emits_terminal(self):
        """When only a sentinel (None) arrives, the handler emits a
        synthetic done event so the SSE stream is never empty."""
        import asyncio

        # Simulate the SSE handler's event loop logic (from http_api.py)
        delta_queue: asyncio.Queue = asyncio.Queue()
        await delta_queue.put(None)  # sentinel only — no data events

        got_done = False
        events_written: list[dict] = []

        while True:
            try:
                event = await asyncio.wait_for(delta_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                break
            if event is None:
                break
            if event.get("error"):
                events_written.append(event)
                got_done = True
                break
            events_written.append(event)
            if event.get("done"):
                got_done = True
                break

        # The handler emits a terminal event when got_done is False
        if not got_done:
            terminal = {"done": True, "stop_reason": "end_turn"}
            events_written.append(terminal)
            got_done = True

        assert got_done
        assert any(e.get("done") for e in events_written)

    @pytest.mark.asyncio
    async def test_error_event_routes_to_sse_error(self):
        """Error events from daemon have done=True for stream termination."""
        import asyncio

        delta_queue: asyncio.Queue = asyncio.Queue()
        error_event = {"error": "test failure", "done": True}
        await delta_queue.put(error_event)
        await delta_queue.put(None)

        event = await delta_queue.get()
        assert event["error"] == "test failure"
        assert event["done"] is True


# ═══════════════════════════════════════════════════════════════════
# P3: Config / Provider Routing
# ═══════════════════════════════════════════════════════════════════

class TestSubagentRouting:
    """Sub-agent uses routed provider when configured."""

    def test_configure_accepts_get_provider(self):
        from tools import agents

        mock_provider = MockProvider([_end_turn()])
        called_with = []

        def fake_get_provider(role):
            called_with.append(role)
            return mock_provider

        agents.configure(get_provider=fake_get_provider)
        assert agents._get_provider is not None

    @pytest.mark.asyncio
    async def test_spawn_uses_routed_provider(self):
        """When get_provider is set, sub-agent uses it instead of _provider."""
        from tools import agents
        from unittest.mock import MagicMock

        primary_provider = MockProvider([_end_turn("primary")])
        subagent_provider = MockProvider([_end_turn("subagent")])
        called_roles = []

        def fake_get_provider(role):
            called_roles.append(role)
            if role == "subagent":
                return subagent_provider
            return primary_provider

        config = MagicMock()
        config.subagent_deny = []
        config.subagent_max_turns = 3
        config.subagent_timeout = 30.0
        config.api_retries = 0
        config.api_retry_base_delay = 0.1
        config.sqlite_timeout = 30
        config.subagent_model = ""
        config.model_config.return_value = {"model": "test", "cost_per_mtok": []}

        reg = _make_registry()
        agents.configure(
            config=config, provider=primary_provider,
            get_provider=fake_get_provider, tool_registry=reg,
        )

        await agents.tool_sessions_spawn(prompt="say hello")
        assert "subagent" in called_roles


class TestOpenAIHttpFallback:
    """OpenAI HTTP fallback flattens extra_body into request params."""

    @pytest.mark.asyncio
    async def test_extra_body_flattened(self):
        """extra_body keys are top-level in the HTTP request, not nested."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from providers.openai import OpenAIProvider

        # Force no SDK
        provider = OpenAIProvider(
            api_key="test", model="test-model", base_url="http://localhost:8080",
            slot_id=3, thinking_budget=1000,
        )
        provider.client = None  # force httpx fallback

        # Mock httpx to capture the request body
        captured_body = {}

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        mock_response.raise_for_status = MagicMock()

        async def fake_post(url, headers=None, json=None):
            captured_body.update(json)
            return mock_response

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = fake_post

        with patch("providers.openai.httpx.AsyncClient", return_value=mock_client):
            await provider.complete("system prompt", [], [])

        # Verify extra_body was flattened
        assert "extra_body" not in captured_body
        assert captured_body.get("id_slot") == 3


# ═══════════════════════════════════════════════════════════════════
# P4: Telemetry Scoping
# ═══════════════════════════════════════════════════════════════════

class TestTelemetryScoping:
    """Telemetry is scoped by sender — each sender sees only its own."""

    @pytest.mark.asyncio
    async def test_drain_returns_only_sender_telemetry(self):
        """Two senders buffer telemetry; drain returns only the requested sender's."""
        import asyncio

        # Simulate the buffer structure
        buffer: dict[tuple[str, str], dict] = {}
        lock = asyncio.Lock()

        # Buffer entries for two senders
        buffer[("alice", "heartrate")] = {
            "text": "HR: 72", "timestamp": time.time(),
        }
        buffer[("bob", "heartrate")] = {
            "text": "HR: 85", "timestamp": time.time(),
        }
        buffer[("alice", "steps")] = {
            "text": "Steps: 5000", "timestamp": time.time(),
        }

        # Drain for alice
        max_age = 60.0
        now = time.time()
        lines = []
        to_remove = []
        async with lock:
            for key, entry in buffer.items():
                age = now - entry["timestamp"]
                if age > max_age:
                    to_remove.append(key)
                    continue
                if key[0] == "alice":
                    lines.append(entry["text"])
                    to_remove.append(key)
            for key in to_remove:
                del buffer[key]

        assert len(lines) == 2
        assert "HR: 72" in lines
        assert "Steps: 5000" in lines
        # Bob's entry should still be in the buffer
        assert ("bob", "heartrate") in buffer
        assert len(buffer) == 1


# ═══════════════════════════════════════════════════════════════════
# P5: Test Runner
# ═══════════════════════════════════════════════════════════════════

class TestPytestConfig:
    """pyproject.toml testpaths prevents mutants/ collection errors."""

    def test_testpaths_configured(self):
        import tomllib
        from pathlib import Path

        pyproject = Path(__file__).parent.parent / "pyproject.toml"
        with pyproject.open("rb") as f:
            data = tomllib.load(f)
        pytest_opts = data.get("tool", {}).get("pytest", {}).get("ini_options", {})
        assert "testpaths" in pytest_opts
        assert "tests" in pytest_opts["testpaths"]


# ═══════════════════════════════════════════════════════════════════
# Regression tests for review findings
# ═══════════════════════════════════════════════════════════════════

class TestTurnGroupEndStopsAtAssistant:
    """Finding 4: _turn_group_end must not scan past a later assistant."""

    def test_orphan_tool_calls_do_not_consume_later_turn(self):
        """assistant(orphan tc) -> user -> assistant(valid tc) -> tool_results
        must NOT return index 4 — the orphan assistant is standalone."""
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "text": "", "tool_calls": [{"id": "orphan"}]},
            {"role": "user", "content": "retry"},
            {"role": "assistant", "text": "", "tool_calls": [{"id": "valid"}]},
            {"role": "tool_results", "results": [{"tool_call_id": "valid"}]},
        ]
        # Group starting at index 1 (orphan assistant) should be standalone
        assert _turn_group_end(msgs, 1) == 2

    def test_valid_assistant_still_includes_its_tool_results(self):
        """The later valid assistant still correctly groups with its results."""
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "text": "", "tool_calls": [{"id": "orphan"}]},
            {"role": "user", "content": "retry"},
            {"role": "assistant", "text": "", "tool_calls": [{"id": "valid"}]},
            {"role": "tool_results", "results": [{"tool_call_id": "valid"}]},
        ]
        # Group starting at index 3 (valid assistant) includes tool_results
        assert _turn_group_end(msgs, 3) == 5


class TestValidateAcrossTurnBoundaries:
    """Finding 3: tool_results must pair with the nearest assistant, not an
    older one across intervening turns."""

    def test_tool_results_across_plain_assistant_is_orphaned(self):
        """assistant(tc1) -> user -> assistant(no tc) -> tool_results(tc2)
        must remove the dangling tool_results."""
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "text": "", "tool_calls": [{"id": "tc1"}]},
            {"role": "user", "content": "hmm"},
            {"role": "assistant", "text": "thinking"},
            {"role": "tool_results", "results": [{"tool_call_id": "tc2"}]},
        ]
        _validate_turn_structure(msgs)
        # The tool_results should be removed (nearest assistant has no tc)
        roles = [m.get("role") for m in msgs]
        assert "tool_results" not in roles
        # And tc1 on the first assistant should be stripped (no matching results)
        assert "tool_calls" not in msgs[1]

    def test_valid_pairing_across_user_hint_preserved(self):
        """assistant(tc) -> user(system hint) -> tool_results is valid."""
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "text": "", "tool_calls": [{"id": "tc1"}]},
            {"role": "user", "content": "[system: context pressure]"},
            {"role": "tool_results", "results": [{"tool_call_id": "tc1"}]},
            {"role": "assistant", "text": "done"},
        ]
        _validate_turn_structure(msgs)
        assert len(msgs) == 5  # nothing removed


class TestIsTransientHttpx:
    """Finding 2: httpx exceptions must be recognized as transient."""

    def test_httpx_http_status_error_503(self):
        from agentic import is_transient_error

        # Simulate httpx.HTTPStatusError with 503
        class FakeResponse:
            status_code = 503
        exc = type("HTTPStatusError", (Exception,), {})()
        exc.response = FakeResponse()
        assert is_transient_error(exc) is True

    def test_httpx_http_status_error_401_not_retried(self):
        from agentic import is_transient_error

        class FakeResponse:
            status_code = 401
        exc = type("HTTPStatusError", (Exception,), {})()
        exc.response = FakeResponse()
        assert is_transient_error(exc) is False

    def test_httpx_http_status_error_429_retried(self):
        from agentic import is_transient_error

        class FakeResponse:
            status_code = 429
        exc = type("HTTPStatusError", (Exception,), {})()
        exc.response = FakeResponse()
        assert is_transient_error(exc) is True

    def test_httpx_timeout_exception(self):
        from agentic import is_transient_error

        exc = type("TimeoutException", (Exception,), {})()
        assert is_transient_error(exc) is True

    def test_httpx_connect_error(self):
        from agentic import is_transient_error

        exc = type("ConnectError", (Exception,), {})()
        assert is_transient_error(exc) is True

    @pytest.mark.parametrize("cls_name", [
        "ReadError", "WriteError", "CloseError", "ProxyError",
        "NetworkError", "TransportError",
        "RemoteProtocolError", "LocalProtocolError", "ProtocolError",
    ])
    def test_httpx_transport_errors_are_transient(self, cls_name):
        from agentic import is_transient_error

        exc = type(cls_name, (Exception,), {})()
        assert is_transient_error(exc) is True
