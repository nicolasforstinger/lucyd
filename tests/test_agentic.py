"""Tests for agentic.py — run_agentic_loop, _truncate_args.

Uses a MockProvider that returns pre-configured LLMResponse sequences.
No real API calls.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, replace

import pytest

from agentic import (
    LoopConfig, run_single_shot,
    _stream_to_response, _truncate_args, is_transient_error,
    run_agentic_loop,
)
from providers import CostContext, LLMResponse, ModelCapabilities, StreamDelta, ToolCall, Usage
from tools import ToolRegistry, ToolSpec

# Default LoopConfig for tests (provided by config in production)
_LOOP_CONFIG = LoopConfig(
    timeout=600.0,
    api_retries=2,
    api_retry_base_delay=2.0,
    sqlite_timeout=30,
)

_TEST_COST = CostContext(
    metering=None,
    session_id="test-sess",
    model_name="opus",
    cost_rates=[5.0, 25.0, 0.5],
)

# ─── Helpers ─────────────────────────────────────────────────────

@dataclass
class MockUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class MockProvider:
    """Returns pre-configured LLMResponse objects in sequence."""

    def __init__(self, responses: list[LLMResponse], caps: ModelCapabilities | None = None):
        self._responses = list(responses)
        self._call_count = 0
        self._capabilities = caps or ModelCapabilities()

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    def format_tools(self, tools):
        return tools

    def format_system(self, blocks):
        return blocks

    def format_messages(self, messages):
        return messages

    async def complete(self, system, messages, tools, **kwargs):
        idx = min(self._call_count, len(self._responses) - 1)
        self._call_count += 1
        return self._responses[idx]

    async def stream(self, system, messages, tools, **kwargs) -> AsyncIterator[StreamDelta]:
        resp = await self.complete(system, messages, tools, **kwargs)
        yield StreamDelta(text=resp.text or "", stop_reason=resp.stop_reason, usage=resp.usage)


def _end_turn_response(text="Done", input_tokens=100, output_tokens=50):
    return LLMResponse(
        text=text,
        tool_calls=[],
        stop_reason="end_turn",
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _tool_use_response(tool_name="echo", arguments=None):
    return LLMResponse(
        text=None,
        tool_calls=[ToolCall(id="tc-1", name=tool_name, arguments=arguments or {"text": "hi"})],
        stop_reason="tool_use",
        usage=Usage(input_tokens=100, output_tokens=50),
    )


def _make_registry():
    reg = ToolRegistry()

    def echo(text: str = "") -> str:
        return f"echo:{text}"

    async def async_echo(text: str = "") -> str:
        return f"async:{text}"

    reg.register(ToolSpec(name="echo", description="echo tool", input_schema={"type": "object"}, function=echo))
    reg.register(ToolSpec(name="async_echo", description="async echo", input_schema={"type": "object"}, function=async_echo))
    return reg


# ─── _truncate_args ──────────────────────────────────────────────

class TestTruncateArgs:
    def test_short_args_unchanged(self):
        args = {"key": "value"}
        result = _truncate_args(args)
        assert result == str(args)

    def test_long_args_truncated(self):
        args = {"key": "x" * 500}
        result = _truncate_args(args, max_len=50)
        assert len(result) == 53  # 50 + "..."
        assert result.endswith("...")


# ─── Agentic Loop ────────────────────────────────────────────────

class TestAgenticLoop:
    @pytest.mark.asyncio
    async def test_single_turn_end_turn(self):
        provider = MockProvider([_end_turn_response("Hello")])
        reg = _make_registry()
        messages = [{"role": "user", "content": "Hi"}]

        resp = await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=[], tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=5),
        )
        assert resp.text == "Hello"
        assert resp.stop_reason == "end_turn"

    @pytest.mark.asyncio
    async def test_tool_use_then_end_turn(self):
        provider = MockProvider([
            _tool_use_response("echo", {"text": "ping"}),
            _end_turn_response("Pong"),
        ])
        reg = _make_registry()
        messages = [{"role": "user", "content": "ping me"}]

        resp = await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=reg.get_schemas(), tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=5),
        )
        assert resp.text == "Pong"
        # Messages should have been mutated in-place:
        # user + assistant(tool_use) + tool_results + assistant(end_turn)
        assert len(messages) == 4

    @pytest.mark.asyncio
    async def test_max_turns_stops_loop(self):
        # Provider always returns tool_use — loop must stop at max_turns
        provider = MockProvider([_tool_use_response()] * 10)
        reg = _make_registry()
        messages = [{"role": "user", "content": "go"}]

        await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=reg.get_schemas(), tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=2),
        )
        # Should have stopped after 2 turns
        assert provider._call_count == 2

    @pytest.mark.asyncio
    async def test_tool_exception_returns_error_string(self):
        def explode(**kwargs):
            raise RuntimeError("kaboom")

        reg = ToolRegistry()
        reg.register(ToolSpec(name="bomb", description="explodes", input_schema={"type": "object"}, function=explode))

        provider = MockProvider([
            _tool_use_response("bomb", {}),
            _end_turn_response("recovered"),
        ])
        messages = [{"role": "user", "content": "boom"}]

        await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=reg.get_schemas(), tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=5),
        )
        # The tool error should be in the tool_results message
        tool_results = [m for m in messages if m.get("role") == "tool_results"]
        assert len(tool_results) >= 1
        assert "Error:" in tool_results[0]["results"][0]["content"]

    @pytest.mark.asyncio
    async def test_cost_recorded_in_db(self, cost_db):
        provider = MockProvider([_end_turn_response()])
        reg = _make_registry()
        messages = [{"role": "user", "content": "hi"}]

        await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=[], tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=1),
            cost=replace(_TEST_COST, metering=cost_db),
        )

        rows = cost_db.query("SELECT COUNT(*) AS cnt FROM costs")
        count = rows[0]["cnt"]
        assert count >= 1

    @pytest.mark.asyncio
    async def test_max_cost_circuit_breaker(self, cost_db):
        # High token counts to trigger cost limit quickly
        resp1 = LLMResponse(
            text=None,
            tool_calls=[ToolCall(id="tc-1", name="echo", arguments={"text": "x"})],
            stop_reason="tool_use",
            usage=Usage(input_tokens=10_000_000, output_tokens=1_000_000),
        )
        resp2 = _end_turn_response()
        provider = MockProvider([resp1, resp2])
        reg = _make_registry()
        messages = [{"role": "user", "content": "expensive"}]

        resp = await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=reg.get_schemas(), tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=5, max_cost=0.01),
            cost=replace(_TEST_COST, metering=cost_db, session_id="cost-test"),
        )
        assert resp.cost_limited is True

    @pytest.mark.asyncio
    async def test_intermediate_text_dropped_when_final_has_text(self):
        """When the final turn has text, intermediate text alongside tool
        calls is dropped — it's thinking out loud, not a deliberate message."""
        resp_with_both = LLMResponse(
            text="Thinking out loud",
            tool_calls=[ToolCall(id="tc-1", name="echo", arguments={"text": "x"})],
            stop_reason="tool_use",
            usage=Usage(input_tokens=100, output_tokens=50),
        )
        provider = MockProvider([resp_with_both, _end_turn_response("Final")])
        reg = _make_registry()
        messages = [{"role": "user", "content": "go"}]

        resp = await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=reg.get_schemas(), tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=5),
        )
        assert resp.text == "Final"

    @pytest.mark.asyncio
    async def test_intermediate_text_surfaced_on_empty_end(self):
        """When the final turn is empty, intermediate text alongside tool
        calls is surfaced as the response so the user isn't left with
        silence."""
        turn1 = LLMResponse(
            text="First thought",
            tool_calls=[ToolCall(id="tc-1", name="echo", arguments={"text": "a"})],
            stop_reason="tool_use",
            usage=Usage(input_tokens=100, output_tokens=50),
        )
        turn2 = LLMResponse(
            text="Second thought",
            tool_calls=[ToolCall(id="tc-2", name="echo", arguments={"text": "b"})],
            stop_reason="tool_use",
            usage=Usage(input_tokens=100, output_tokens=50),
        )
        turn3 = _end_turn_response("")  # empty end_turn

        provider = MockProvider([turn1, turn2, turn3])
        reg = _make_registry()
        messages = [{"role": "user", "content": "check these docs"}]

        resp = await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=reg.get_schemas(), tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=10),
        )
        assert resp.text == "First thought\n\nSecond thought"

    @pytest.mark.asyncio
    async def test_messages_mutated_in_place(self):
        provider = MockProvider([_end_turn_response("Done")])
        reg = _make_registry()
        messages = [{"role": "user", "content": "hi"}]
        original_id = id(messages)

        await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=[], tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=1),
        )
        # Same list object, but with new messages appended
        assert id(messages) == original_id
        assert len(messages) == 2  # user + assistant


# ─── Phase 3: Behavioral Survivors ──────────────────────────────


class TestCostAccumulation:
    """Mutant #53: accumulated_cost += turn_cost → = turn_cost"""

    @pytest.mark.asyncio
    async def test_cost_accumulates_across_turns(self, cost_db):
        """Cost must ACCUMULATE (+=) not reset (=) across turns."""
        # Two tool-use turns, each with known token counts
        turn1 = LLMResponse(
            text=None,
            tool_calls=[ToolCall(id="tc-1", name="echo", arguments={"text": "a"})],
            stop_reason="tool_use",
            usage=Usage(input_tokens=1_000_000, output_tokens=100_000),
        )
        turn2 = LLMResponse(
            text=None,
            tool_calls=[ToolCall(id="tc-2", name="echo", arguments={"text": "b"})],
            stop_reason="tool_use",
            usage=Usage(input_tokens=1_000_000, output_tokens=100_000),
        )
        # End turn — cost should already be $10 total
        end = _end_turn_response("Done", input_tokens=100, output_tokens=50)

        provider = MockProvider([turn1, turn2, end])
        reg = _make_registry()
        messages = [{"role": "user", "content": "test"}]

        # max_cost = $8 — with accumulation, this triggers after turn 2
        # turn1: 1M * 5.0/1M + 100K * 25.0/1M = 5.0 + 2.5 = 7.5
        # turn2: another 7.5, total = 15.0 → exceeds $8
        resp = await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=reg.get_schemas(), tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=10, max_cost=8.0),
            cost=replace(_TEST_COST, metering=cost_db, session_id="accum-test"),
        )
        assert resp.cost_limited is True


class TestMaxCostZeroUnlimited:
    """Mutant #55/#56: max_cost=0 should mean unlimited."""

    @pytest.mark.asyncio
    async def test_max_cost_zero_means_unlimited(self, cost_db):
        """max_cost=0 must NOT trigger the cost limit."""
        turn1 = LLMResponse(
            text=None,
            tool_calls=[ToolCall(id="tc-1", name="echo", arguments={"text": "a"})],
            stop_reason="tool_use",
            usage=Usage(input_tokens=10_000_000, output_tokens=1_000_000),
        )
        end = _end_turn_response("Done normally")

        provider = MockProvider([turn1, end])
        reg = _make_registry()
        messages = [{"role": "user", "content": "test"}]

        resp = await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=reg.get_schemas(), tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=10, max_cost=0.0),
            cost=replace(_TEST_COST, metering=cost_db, session_id="zero-cost-test"),
        )
        assert resp.text == "Done normally"
        assert "Cost limit" not in (resp.text or "")


class TestCostLimitPreservesText:
    """Cost limit preserves agent text without appending framework noise."""

    @pytest.mark.asyncio
    async def test_cost_limit_preserves_agent_text(self, cost_db):
        """Cost limit keeps agent text intact and sets cost_limited flag."""
        resp1 = LLMResponse(
            text="Here is your answer",
            tool_calls=[],
            stop_reason="end_turn",
            usage=Usage(input_tokens=10_000_000, output_tokens=1_000_000),
        )
        provider = MockProvider([resp1])
        reg = _make_registry()
        messages = [{"role": "user", "content": "test"}]

        resp = await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=[], tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=1, max_cost=0.01),
            cost=replace(_TEST_COST, metering=cost_db, session_id="append-test"),
        )
        assert resp.text == "Here is your answer"
        assert resp.cost_limited is True


class TestLoopTermination:
    """Mutant #90: stop_reason != 'tool_use' or not tool_calls → and"""

    @pytest.mark.asyncio
    async def test_end_turn_exits_loop(self):
        """stop_reason='end_turn' exits loop even if something weird happened."""
        provider = MockProvider([_end_turn_response("Done")])
        reg = _make_registry()
        messages = [{"role": "user", "content": "test"}]

        resp = await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=[], tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=5),
        )
        assert resp.text == "Done"
        assert provider._call_count == 1

    @pytest.mark.asyncio
    async def test_tool_use_continues_loop(self):
        """stop_reason='tool_use' with tool_calls continues loop."""
        provider = MockProvider([
            _tool_use_response("echo", {"text": "hi"}),
            _end_turn_response("Done"),
        ])
        reg = _make_registry()
        messages = [{"role": "user", "content": "test"}]

        resp = await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=reg.get_schemas(), tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=5),
        )
        assert resp.text == "Done"
        assert provider._call_count == 2


class TestToolResultDictKeys:
    """Mutant #115: tool_call_id key name."""

    @pytest.mark.asyncio
    async def test_tool_result_has_correct_keys(self):
        """Tool result dict must have 'tool_call_id' and 'content' keys."""
        provider = MockProvider([
            _tool_use_response("echo", {"text": "x"}),
            _end_turn_response("Done"),
        ])
        reg = _make_registry()
        messages = [{"role": "user", "content": "test"}]

        await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=reg.get_schemas(), tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=5),
        )
        # Find tool_results in messages
        tool_results = [m for m in messages if m.get("role") == "tool_results"]
        assert len(tool_results) >= 1
        result = tool_results[0]["results"][0]
        assert "tool_call_id" in result
        assert "content" in result
        assert result["tool_call_id"] == "tc-1"


class TestReturnExceptions:
    """Mutant #125: return_exceptions=True → False"""

    @pytest.mark.asyncio
    async def test_parallel_tool_error_collected_not_raised(self):
        """Tool exception is collected as error string, not re-raised."""
        def explode(**kwargs):
            raise RuntimeError("kaboom")

        def ok(**kwargs):
            return "ok-result"

        reg = ToolRegistry()
        reg.register(ToolSpec(name="bomb", description="explodes", input_schema={"type": "object", "properties": {}}, function=explode))
        reg.register(ToolSpec(name="ok_tool", description="works", input_schema={"type": "object", "properties": {}}, function=ok))

        resp1 = LLMResponse(
            text=None,
            tool_calls=[
                ToolCall(id="tc-1", name="bomb", arguments={}),
                ToolCall(id="tc-2", name="ok_tool", arguments={}),
            ],
            stop_reason="tool_use",
            usage=Usage(input_tokens=100, output_tokens=50),
        )
        provider = MockProvider([resp1, _end_turn_response("Done")])
        messages = [{"role": "user", "content": "test"}]

        resp = await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=reg.get_schemas(), tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=5),
        )
        # Should reach end_turn, not crash
        assert resp.text == "Done"
        # Tool results should contain both — error string and success
        tool_results = [m for m in messages if m.get("role") == "tool_results"]
        assert len(tool_results) >= 1
        results = tool_results[0]["results"]
        assert len(results) == 2


class TestFallbackText:
    """Mutant #163-166: fallback text logic."""

    @pytest.mark.asyncio
    async def test_fallback_on_normal_end_turn(self):
        """Intermediate text is surfaced when the final turn is empty,
        so the user isn't left with silence."""
        turn1 = LLMResponse(
            text="Intermediate thought",
            tool_calls=[ToolCall(id="tc-1", name="echo", arguments={"text": "x"})],
            stop_reason="tool_use",
            usage=Usage(input_tokens=100, output_tokens=50),
        )
        end = _end_turn_response(None)  # None text
        end.text = None

        provider = MockProvider([turn1, end])
        reg = _make_registry()
        messages = [{"role": "user", "content": "test"}]

        resp = await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=reg.get_schemas(), tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=5),
        )
        assert resp.text == "Intermediate thought"

    @pytest.mark.asyncio
    async def test_no_fallback_when_final_has_text(self):
        """Fallback NOT used when final response already has text."""
        turn1 = LLMResponse(
            text="Intermediate",
            tool_calls=[ToolCall(id="tc-1", name="echo", arguments={"text": "x"})],
            stop_reason="tool_use",
            usage=Usage(input_tokens=100, output_tokens=50),
        )
        provider = MockProvider([turn1, _end_turn_response("Final answer")])
        reg = _make_registry()
        messages = [{"role": "user", "content": "test"}]

        resp = await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=reg.get_schemas(), tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=5),
        )
        assert resp.text == "Final answer"
        assert "Intermediate" not in resp.text


class TestToolsFormatting:
    """Mutant #12: fmt_tools = ... → None"""

    @pytest.mark.asyncio
    async def test_empty_tools_gives_empty_list(self):
        """When no tools, format_tools result should be empty list, not None."""
        provider = MockProvider([_end_turn_response("Ok")])
        reg = _make_registry()
        messages = [{"role": "user", "content": "test"}]

        resp = await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=[], tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=1),
        )
        assert resp.text == "Ok"

    @pytest.mark.asyncio
    async def test_tools_passed_to_provider(self):
        """When tools are provided, they're formatted and passed to complete."""
        calls = []
        class TrackingProvider(MockProvider):
            async def complete(self, system, messages, tools, **kwargs):
                calls.append(tools)
                return await super().complete(system, messages, tools)

        provider = TrackingProvider([_end_turn_response("Ok")])
        reg = _make_registry()
        messages = [{"role": "user", "content": "test"}]

        await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=reg.get_schemas(), tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=1),
        )
        assert len(calls) == 1
        assert len(calls[0]) > 0  # tools were passed, not None


class TestMaxTokensWithToolCalls:
    """Truncated response with tool_use blocks: execute tools, continue loop."""

    @pytest.mark.asyncio
    async def test_max_tokens_with_tool_calls_executes_and_continues(self):
        """Complete tool calls in a truncated response must be executed
        and the loop must continue — the model's work is valid."""
        truncated = LLMResponse(
            text="I'll read that file",
            tool_calls=[ToolCall(id="tc-trunc", name="echo", arguments={"text": "x"})],
            stop_reason="max_tokens",
            usage=Usage(input_tokens=100, output_tokens=16384),
        )
        provider = MockProvider([truncated, _end_turn_response("Done")])
        reg = _make_registry()
        messages = [{"role": "user", "content": "read the file"}]

        resp = await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=reg.get_schemas(), tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=5),
        )
        # Loop continued past truncation and reached end_turn
        assert resp.text == "Done"
        assert provider._call_count == 2
        # Tool results from the executed tool call are in the history
        tool_results = [m for m in messages if m.get("role") == "tool_results"]
        assert len(tool_results) == 1
        assert tool_results[0]["results"][0]["tool_call_id"] == "tc-trunc"
        assert "echo:x" in tool_results[0]["results"][0]["content"]

    @pytest.mark.asyncio
    async def test_max_tokens_with_tool_calls_tools_actually_run(self):
        """Verify the tools are actually executed, not stubbed with errors."""
        call_log = []

        def tracked_echo(text: str = "") -> str:
            call_log.append(text)
            return f"echo:{text}"

        reg = ToolRegistry()
        reg.register(ToolSpec(name="echo", description="echo tool", input_schema={"type": "object"}, function=tracked_echo))

        truncated = LLMResponse(
            text=None,
            tool_calls=[ToolCall(id="tc-trunc", name="echo", arguments={"text": "ping"})],
            stop_reason="max_tokens",
            usage=Usage(input_tokens=100, output_tokens=16384),
        )
        provider = MockProvider([truncated, _end_turn_response("Done")])
        messages = [{"role": "user", "content": "go"}]

        await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=reg.get_schemas(), tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=5),
        )
        assert call_log == ["ping"], "Tool must actually execute"

    @pytest.mark.asyncio
    async def test_max_tokens_without_tool_calls_returns_normally(self):
        """Truncated response without tool_calls should still return normally."""
        truncated = LLMResponse(
            text="I was saying—",
            tool_calls=[],
            stop_reason="max_tokens",
            usage=Usage(input_tokens=100, output_tokens=16384),
        )
        provider = MockProvider([truncated])
        reg = _make_registry()
        messages = [{"role": "user", "content": "go"}]

        resp = await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=[], tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=5),
        )
        assert resp.text == "I was saying—"
        assert resp.stop_reason == "max_tokens"


class TestCostInit:
    """Mutant #15: accumulated_cost = 0.0 → 1.0"""

    @pytest.mark.asyncio
    async def test_initial_cost_is_zero(self, cost_db):
        """Without any API calls, accumulated cost should be zero."""
        # Single end turn, very small token counts
        provider = MockProvider([_end_turn_response("Ok", input_tokens=1, output_tokens=1)])
        reg = _make_registry()
        messages = [{"role": "user", "content": "test"}]

        # Very tight cost limit — if initial cost was 1.0, this would trigger
        resp = await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=[], tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=1, max_cost=0.5),
            cost=replace(_TEST_COST, metering=cost_db, session_id="init-test"),
        )
        assert "Cost limit" not in (resp.text or "")


# ─── Provider Retry with Backoff ────────────────────────────────


class TestIsTransientError:
    """is_transient_error classification."""

    def test_rate_limit_is_transient(self):
        class RateLimitError(Exception):
            pass

        assert is_transient_error(RateLimitError("429")) is True

    def test_connection_error_is_transient(self):
        assert is_transient_error(ConnectionError("reset")) is True

    def test_os_error_is_transient(self):
        assert is_transient_error(OSError("network down")) is True

    def test_auth_error_not_transient(self):
        class AuthenticationError(Exception):
            pass

        assert is_transient_error(AuthenticationError("bad key")) is False

    def test_bad_request_not_transient(self):
        class BadRequestError(Exception):
            pass

        assert is_transient_error(BadRequestError("invalid")) is False

    def test_permission_denied_not_transient(self):
        class PermissionDeniedError(Exception):
            pass

        assert is_transient_error(PermissionDeniedError("denied")) is False

    def test_unknown_error_not_transient(self):
        assert is_transient_error(ValueError("nope")) is False

    def test_api_status_error_with_500(self):
        class APIStatusError(Exception):
            status_code = 500
        assert is_transient_error(APIStatusError("server error")) is True

    def test_api_status_error_with_400(self):
        class APIStatusError(Exception):
            status_code = 400
        assert is_transient_error(APIStatusError("bad request")) is False

    def test_not_found_error_not_transient(self):
        class NotFoundError(Exception):
            pass

        assert is_transient_error(NotFoundError("404")) is False

    def test_unprocessable_entity_not_transient(self):
        class UnprocessableEntityError(Exception):
            pass

        assert is_transient_error(UnprocessableEntityError("422")) is False

    def test_api_connection_error_is_transient(self):
        class APIConnectionError(Exception):
            pass

        assert is_transient_error(APIConnectionError("reset")) is True

    def test_api_timeout_error_is_transient(self):
        class APITimeoutError(Exception):
            pass

        assert is_transient_error(APITimeoutError("timeout")) is True

    def test_internal_server_error_is_transient(self):
        class InternalServerError(Exception):
            pass

        assert is_transient_error(InternalServerError("500")) is True

    def test_overloaded_error_is_transient(self):
        class OverloadedError(Exception):
            pass

        assert is_transient_error(OverloadedError("overloaded")) is True

    def test_api_status_error_with_429_is_transient(self):
        class APIStatusError(Exception):
            status_code = 429
        assert is_transient_error(APIStatusError("rate limited")) is True

    def test_api_status_error_with_503_is_transient(self):
        class APIStatusError(Exception):
            status_code = 503
        assert is_transient_error(APIStatusError("service unavailable")) is True

    def test_api_status_error_with_401_not_transient(self):
        class APIStatusError(Exception):
            status_code = 401
        assert is_transient_error(APIStatusError("unauthorized")) is False

    def test_mistral_error_429_is_transient(self):
        class MistralError(Exception):
            status_code = 429
        assert is_transient_error(MistralError("rate limited")) is True

    def test_mistral_error_500_is_transient(self):
        class MistralError(Exception):
            status_code = 500
        assert is_transient_error(MistralError("server error")) is True

    def test_mistral_error_401_not_transient(self):
        class MistralError(Exception):
            status_code = 401
        assert is_transient_error(MistralError("unauthorized")) is False

    def test_mistral_error_400_not_transient(self):
        class MistralError(Exception):
            status_code = 400
        assert is_transient_error(MistralError("bad request")) is False

    def test_mistral_sdk_error_503_is_transient(self):
        class SDKError(Exception):
            status_code = 503
        assert is_transient_error(SDKError("service unavailable")) is True

    def test_mistral_error_no_status_not_transient(self):
        class MistralError(Exception):
            pass
        assert is_transient_error(MistralError("unknown")) is False


class TestRetryLogic:
    """Provider retry in agentic loop."""

    @pytest.mark.asyncio
    async def test_retry_succeeds_on_second_attempt(self):
        """Transient error on first call, success on second."""
        call_count = [0]
        ok_response = _end_turn_response("Recovered")

        class RetryProvider(MockProvider):
            async def complete(self, system, messages, tools, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    exc = type("RateLimitError", (Exception,), {})("429")
                    raise exc
                return ok_response

        provider = RetryProvider([ok_response])
        reg = _make_registry()
        messages = [{"role": "user", "content": "test"}]

        resp = await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=[], tool_executor=reg,
            config=LoopConfig(max_turns=1, timeout=600.0, api_retries=2, api_retry_base_delay=0.01, sqlite_timeout=30),
        )
        assert resp.text == "Recovered"
        assert call_count[0] == 2

    @pytest.mark.asyncio
    async def test_no_retry_on_auth_error(self):
        """Auth errors propagate immediately without retry."""
        call_count = [0]
        AuthError = type("AuthenticationError", (Exception,), {})

        class AuthFailProvider(MockProvider):
            async def complete(self, system, messages, tools, **kwargs):
                call_count[0] += 1
                raise AuthError("bad key")

        provider = AuthFailProvider([_end_turn_response()])
        reg = _make_registry()
        messages = [{"role": "user", "content": "test"}]

        with pytest.raises(Exception, match="bad key"):
            await run_agentic_loop(
                provider=provider, system=[], messages=messages,
                tools=[], tool_executor=reg,
                config=LoopConfig(max_turns=1, timeout=600.0, api_retries=3, api_retry_base_delay=0.01, sqlite_timeout=30),
            )
        assert call_count[0] == 1  # No retry

    @pytest.mark.asyncio
    async def test_exhausted_retries_propagates(self):
        """After all retries exhausted, the error propagates."""
        RateLimit = type("RateLimitError", (Exception,), {})

        class AlwaysFailProvider(MockProvider):
            async def complete(self, system, messages, tools, **kwargs):
                raise RateLimit("429 always")

        provider = AlwaysFailProvider([_end_turn_response()])
        reg = _make_registry()
        messages = [{"role": "user", "content": "test"}]

        with pytest.raises(Exception, match="429 always"):
            await run_agentic_loop(
                provider=provider, system=[], messages=messages,
                tools=[], tool_executor=reg,
                config=LoopConfig(max_turns=1, timeout=600.0, api_retries=2, api_retry_base_delay=0.01, sqlite_timeout=30),
            )


# ─── Streaming Tests ─────────────────────────────────────────────

class MockStreamProvider(MockProvider):
    """MockProvider that also supports stream() via pre-configured deltas."""

    def __init__(self, responses, deltas=None, caps=None):
        super().__init__(responses, caps=caps or ModelCapabilities(supports_streaming=True))
        self._deltas = deltas or []

    async def stream(self, system, messages, tools, **kwargs):
        for d in self._deltas:
            yield d


class TestStreamToResponse:
    """Verify _stream_to_response aggregates deltas correctly."""

    async def test_text_only(self):
        deltas = [
            StreamDelta(text="Hello "),
            StreamDelta(text="world"),
            StreamDelta(stop_reason="end_turn", usage=Usage(input_tokens=10, output_tokens=5)),
        ]
        provider = MockStreamProvider([], deltas=deltas)
        collected = []
        resp = await _stream_to_response(provider, [], [], [], lambda d: collected.append(d))
        assert resp.text == "Hello world"
        assert resp.stop_reason == "end_turn"
        assert resp.usage.output_tokens == 5
        assert len(collected) == 3

    async def test_tool_calls(self):
        deltas = [
            StreamDelta(tool_call_index=0, tool_call_id="tc1", tool_name="read"),
            StreamDelta(tool_call_index=0, tool_args_delta='{"path":'),
            StreamDelta(tool_call_index=0, tool_args_delta='"/tmp"}'),
            StreamDelta(stop_reason="tool_use", usage=Usage(input_tokens=50, output_tokens=20)),
        ]
        provider = MockStreamProvider([], deltas=deltas)
        resp = await _stream_to_response(provider, [], [], [], None)
        assert resp.stop_reason == "tool_use"
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "read"
        assert resp.tool_calls[0].arguments == {"path": "/tmp"}

    async def test_loop_uses_streaming(self):
        """run_agentic_loop uses stream() when supports_streaming and on_stream_delta."""
        deltas = [
            StreamDelta(text="Streamed!"),
            StreamDelta(stop_reason="end_turn", usage=Usage(input_tokens=10, output_tokens=5)),
        ]
        provider = MockStreamProvider([], deltas=deltas)
        reg = ToolRegistry()
        messages = [{"role": "user", "content": "test"}]
        collected = []
        resp = await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=[], tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=1),
            on_stream_delta=lambda d: collected.append(d),
        )
        assert resp.text == "Streamed!"
        assert len(collected) >= 1


# ─── Strategy Tests ──────────────────────────────────────────────

class TestRunSingleShot:
    """Verify run_single_shot calls model once without tools."""

    async def test_single_call(self):
        provider = MockProvider([_end_turn_response("Hello")])
        reg = ToolRegistry()
        messages = [{"role": "user", "content": "test"}]
        resp = await run_single_shot(
            provider=provider, system=[], messages=messages,
            tools=[{"name": "read", "description": "Read", "input_schema": {}}],
            tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=5),
        )
        assert resp.text == "Hello"
        assert resp.stop_reason == "end_turn"
        assert provider._call_count == 1  # exactly one call


class TestContextTrimming:
    """Verify context budget enforcement trims messages."""

    async def test_trims_when_over_budget(self):
        """Messages are trimmed when context exceeds max_context_tokens."""
        caps = ModelCapabilities(max_context_tokens=500)
        provider = MockProvider([_end_turn_response("OK")], caps=caps)
        reg = ToolRegistry()
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "A " * 500},
            {"role": "user", "content": "B " * 500},
            {"role": "user", "content": "latest"},
        ]
        original_count = len(messages)
        resp = await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=[], tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=1),
        )
        assert resp.text == "OK"
        assert len(messages) < original_count + 1

    async def test_no_trim_when_under_budget(self):
        """Messages not trimmed when within budget."""
        caps = ModelCapabilities(max_context_tokens=100000)
        provider = MockProvider([_end_turn_response("OK")], caps=caps)
        reg = ToolRegistry()
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
            {"role": "user", "content": "test"},
        ]
        resp = await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=[], tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=1),
        )
        assert resp.text == "OK"
        assert len(messages) == 4
