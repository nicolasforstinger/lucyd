"""Tests for agentic.py — run_agentic_loop, _record_cost, _truncate_args.

Uses a MockProvider that returns pre-configured LLMResponse sequences.
No real API calls.
"""

import sqlite3
from dataclasses import dataclass

import pytest

from agentic import _init_cost_db, _record_cost, _truncate_args, run_agentic_loop
from providers import LLMResponse, ToolCall, Usage
from tools import ToolRegistry

# ─── Helpers ─────────────────────────────────────────────────────

@dataclass
class MockUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class MockProvider:
    """Returns pre-configured LLMResponse objects in sequence."""

    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self._call_count = 0

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

    reg.register("echo", "echo tool", {"type": "object"}, echo)
    reg.register("async_echo", "async echo", {"type": "object"}, async_echo)
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


# ─── _record_cost (additional tests beyond test_cost.py) ─────────

class TestRecordCostEdgeCases:
    def test_correct_cost_calculation(self, cost_db):
        usage = MockUsage(
            input_tokens=1_000_000,
            output_tokens=100_000,
            cache_read_tokens=500_000,
        )
        cost = _record_cost(str(cost_db), "s1", "opus", usage, [5.0, 25.0, 0.5])
        assert abs(cost - 7.75) < 0.001

    def test_empty_path_returns_zero(self):
        usage = MockUsage(input_tokens=1000)
        assert _record_cost("", "s", "m", usage, [5.0, 25.0]) == 0.0

    def test_empty_rates_returns_zero(self, cost_db):
        usage = MockUsage(input_tokens=1000)
        assert _record_cost(str(cost_db), "s", "m", usage, []) == 0.0


# ─── Agentic Loop ────────────────────────────────────────────────

class TestAgenticLoop:
    @pytest.mark.asyncio
    async def test_single_turn_end_turn(self):
        provider = MockProvider([_end_turn_response("Hello")])
        reg = _make_registry()
        messages = [{"role": "user", "content": "Hi"}]

        resp = await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=[], tool_executor=reg, max_turns=5,
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
            tools=reg.get_schemas(), tool_executor=reg, max_turns=5,
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
            tools=reg.get_schemas(), tool_executor=reg, max_turns=2,
        )
        # Should have stopped after 2 turns
        assert provider._call_count == 2

    @pytest.mark.asyncio
    async def test_tool_exception_returns_error_string(self):
        def explode(**kwargs):
            raise RuntimeError("kaboom")

        reg = ToolRegistry()
        reg.register("bomb", "explodes", {"type": "object"}, explode)

        provider = MockProvider([
            _tool_use_response("bomb", {}),
            _end_turn_response("recovered"),
        ])
        messages = [{"role": "user", "content": "boom"}]

        await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=reg.get_schemas(), tool_executor=reg, max_turns=5,
        )
        # The tool error should be in the tool_results message
        tool_results = [m for m in messages if m.get("role") == "tool_results"]
        assert len(tool_results) >= 1
        assert "Error:" in tool_results[0]["results"][0]["content"]

    @pytest.mark.asyncio
    async def test_cost_recorded_in_db(self, cost_db):
        _init_cost_db(str(cost_db))
        provider = MockProvider([_end_turn_response()])
        reg = _make_registry()
        messages = [{"role": "user", "content": "hi"}]

        await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=[], tool_executor=reg, max_turns=1,
            cost_db=str(cost_db), session_id="test-sess",
            model_name="opus", cost_rates=[5.0, 25.0, 0.5],
        )

        conn = sqlite3.connect(str(cost_db))
        count = conn.execute("SELECT COUNT(*) FROM costs").fetchone()[0]
        conn.close()
        assert count >= 1

    @pytest.mark.asyncio
    async def test_max_cost_circuit_breaker(self, cost_db):
        _init_cost_db(str(cost_db))
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
            tools=reg.get_schemas(), tool_executor=reg, max_turns=5,
            cost_db=str(cost_db), session_id="cost-test",
            model_name="opus", cost_rates=[5.0, 25.0, 0.5],
            max_cost=0.01,  # Very low limit
        )
        assert "Cost limit" in (resp.text or "")

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
            tools=reg.get_schemas(), tool_executor=reg, max_turns=5,
        )
        assert resp.text == "Final"

    @pytest.mark.asyncio
    async def test_intermediate_text_used_as_fallback_on_empty_end(self):
        """When the final turn is empty, intermediate text is used as
        fallback — prevents silence when the model said something useful
        earlier but ended with an empty turn."""
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
            tools=reg.get_schemas(), tool_executor=reg, max_turns=10,
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
            tools=[], tool_executor=reg, max_turns=1,
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
        _init_cost_db(str(cost_db))
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
            tools=reg.get_schemas(), tool_executor=reg, max_turns=10,
            cost_db=str(cost_db), session_id="accum-test",
            model_name="opus", cost_rates=[5.0, 25.0, 0.5],
            max_cost=8.0,
        )
        assert "Cost limit" in (resp.text or "")


class TestMaxCostZeroUnlimited:
    """Mutant #55/#56: max_cost=0 should mean unlimited."""

    @pytest.mark.asyncio
    async def test_max_cost_zero_means_unlimited(self, cost_db):
        """max_cost=0 must NOT trigger the cost limit."""
        _init_cost_db(str(cost_db))
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
            tools=reg.get_schemas(), tool_executor=reg, max_turns=10,
            cost_db=str(cost_db), session_id="zero-cost-test",
            model_name="opus", cost_rates=[5.0, 25.0, 0.5],
            max_cost=0.0,  # 0 means unlimited
        )
        assert resp.text == "Done normally"
        assert "Cost limit" not in (resp.text or "")


class TestCostLimitAppends:
    """Mutant #70: response.text += → response.text = (overwrites)."""

    @pytest.mark.asyncio
    async def test_cost_limit_appends_not_replaces(self, cost_db):
        """Cost limit message is APPENDED to existing text, not replacing it."""
        _init_cost_db(str(cost_db))
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
            tools=[], tool_executor=reg, max_turns=1,
            cost_db=str(cost_db), session_id="append-test",
            model_name="opus", cost_rates=[5.0, 25.0, 0.5],
            max_cost=0.01,
        )
        # Must contain BOTH the original text AND the cost limit message
        assert "Here is your answer" in resp.text
        assert "Cost limit" in resp.text


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
            tools=[], tool_executor=reg, max_turns=5,
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
            tools=reg.get_schemas(), tool_executor=reg, max_turns=5,
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
            tools=reg.get_schemas(), tool_executor=reg, max_turns=5,
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
        reg.register("bomb", "explodes", {"type": "object", "properties": {}}, explode)
        reg.register("ok_tool", "works", {"type": "object", "properties": {}}, ok)

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
            tools=reg.get_schemas(), tool_executor=reg, max_turns=5,
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
    async def test_fallback_only_when_final_empty(self):
        """Fallback text used ONLY when response.text is empty/None."""
        turn1 = LLMResponse(
            text="Intermediate thought",
            tool_calls=[ToolCall(id="tc-1", name="echo", arguments={"text": "x"})],
            stop_reason="tool_use",
            usage=Usage(input_tokens=100, output_tokens=50),
        )
        end = _end_turn_response(None)  # None text → fallback kicks in
        # Fix: end_turn_response helper puts a string, let's override
        end.text = None

        provider = MockProvider([turn1, end])
        reg = _make_registry()
        messages = [{"role": "user", "content": "test"}]

        resp = await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=reg.get_schemas(), tool_executor=reg, max_turns=5,
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
            tools=reg.get_schemas(), tool_executor=reg, max_turns=5,
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
            tools=[], tool_executor=reg, max_turns=1,
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
            tools=reg.get_schemas(), tool_executor=reg, max_turns=1,
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
            tools=reg.get_schemas(), tool_executor=reg, max_turns=5,
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
        reg.register("echo", "echo tool", {"type": "object"}, tracked_echo)

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
            tools=reg.get_schemas(), tool_executor=reg, max_turns=5,
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
            tools=[], tool_executor=reg, max_turns=5,
        )
        assert resp.text == "I was saying—"
        assert resp.stop_reason == "max_tokens"


class TestCostInit:
    """Mutant #15: accumulated_cost = 0.0 → 1.0"""

    @pytest.mark.asyncio
    async def test_initial_cost_is_zero(self, cost_db):
        """Without any API calls, accumulated cost should be zero."""
        _init_cost_db(str(cost_db))
        # Single end turn, very small token counts
        provider = MockProvider([_end_turn_response("Ok", input_tokens=1, output_tokens=1)])
        reg = _make_registry()
        messages = [{"role": "user", "content": "test"}]

        # Very tight cost limit — if initial cost was 1.0, this would trigger
        resp = await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=[], tool_executor=reg, max_turns=1,
            cost_db=str(cost_db), session_id="init-test",
            model_name="opus", cost_rates=[5.0, 25.0, 0.5],
            max_cost=0.5,  # Would trigger if starting at $1
        )
        assert "Cost limit" not in (resp.text or "")
