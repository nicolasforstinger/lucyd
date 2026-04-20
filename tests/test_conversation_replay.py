"""Conversation replay tests — feed recorded fixtures through the agentic loop.

Each fixture in tests/fixtures/ defines a conversation scenario with mock
provider responses and expected outcomes.
"""

import json
from pathlib import Path

import pytest

from agentic import LoopConfig, run_agentic_loop
from providers import LLMResponse, ModelCapabilities, ToolCall, Usage
from tools import ToolRegistry, ToolSpec

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_fixtures():
    """Load all .json fixtures from the fixtures directory."""
    fixtures = []
    for f in sorted(FIXTURES_DIR.glob("*.json")):
        with f.open() as fh:
            data = json.load(fh)
            data["_path"] = f.name
            fixtures.append(data)
    return fixtures


class _ReplayProvider:
    """Mock provider that returns pre-configured responses from fixture data."""

    def __init__(self, responses: list[dict], caps=None):
        self._responses = responses
        self._call_count = 0
        self._capabilities = caps or ModelCapabilities()

    @property
    def capabilities(self):
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
        r = self._responses[idx]
        return LLMResponse(
            text=r.get("text"),
            tool_calls=[ToolCall(**tc) for tc in r.get("tool_calls", [])],
            stop_reason=r.get("stop_reason", "end_turn"),
            usage=Usage(input_tokens=100, output_tokens=50),
        )


class TestConversationReplay:
    """Replay recorded conversations and verify expected behavior."""

    @pytest.mark.parametrize(
        "fixture",
        _load_fixtures(),
        ids=lambda f: f["_path"],
    )
    async def test_replay(self, fixture):
        expected = fixture["expected"]
        messages = list(fixture["messages"])

        # Build mock responses
        mock_responses = fixture.get("mock_responses")
        if mock_responses:
            provider = _ReplayProvider(mock_responses)
        else:
            # Single-turn: generate a simple response
            provider = _ReplayProvider([{
                "text": "Hello! How can I help?",
                "tool_calls": [],
                "stop_reason": "end_turn",
            }])

        # Build tool registry with mock implementations
        reg = ToolRegistry()
        mock_results = fixture.get("mock_tool_results", {})
        for tool_def in fixture.get("tools", []):
            name = tool_def["name"]
            result = mock_results.get(name, f"{name} result")
            reg.register(ToolSpec(
                name=name,
                description=tool_def["description"],
                input_schema=tool_def.get("input_schema", {}),
                function=lambda _result=result, **kw: _result,
            ))

        resp = await run_agentic_loop(
            provider=provider,
            system=fixture.get("system", []),
            messages=messages,
            tools=reg.get_schemas(),
            tool_executor=reg,
            config=LoopConfig(
                max_turns=5,
                timeout=60.0,
                api_retries=0,
                api_retry_base_delay=0,
            ),
        )

        # Verify expected outcomes
        if "stop_reason" in expected:
            assert resp.stop_reason == expected["stop_reason"], \
                f"[{fixture['_path']}] stop_reason: {resp.stop_reason} != {expected['stop_reason']}"

        if expected.get("has_text"):
            assert resp.text, f"[{fixture['_path']}] expected text but got None"

        if expected.get("has_tool_calls") is False:
            assert not resp.tool_calls, f"[{fixture['_path']}] expected no tool calls"

        if "final_text_contains" in expected:
            assert expected["final_text_contains"] in (resp.text or ""), \
                f"[{fixture['_path']}] text {resp.text!r} does not contain {expected['final_text_contains']!r}"
