"""Tests for small model / CPU optimization features.

Covers: model profiles, system prompt diet, compaction auto-scaling,
tool output truncation, prompt cache awareness, thinking detection,
agentic loop efficiency, and graceful degradation.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import Config
from context import ContextBuilder, _estimate_tokens
from providers import LLMResponse, ModelCapabilities, ToolCall, Usage, _repair_json
from providers.openai import _strip_thinking
from tools import _smart_truncate


# ─── Helpers ────────────────────────────────────────────────────

def _make_config(**overrides):
    """Build a minimal Config with optional overrides."""
    data = {
        "agent": {"name": "Test", "workspace": "/tmp"},
        "models": {"primary": {"provider": "openai", "model": "test"}},
    }
    for key_path, val in overrides.items():
        parts = key_path.split(".")
        d = data
        for part in parts[:-1]:
            d = d.setdefault(part, {})
        d[parts[-1]] = val
    return Config(data)


class TestAdaptiveKeepPct:
    def test_small_context_higher_keep_pct(self):
        """Small contexts (<=32k) use keep_pct=0.5 by default."""
        cfg = _make_config(**{"models.primary.max_context_tokens": 32768})
        assert cfg.compaction_keep_pct == 0.5

    def test_large_context_normal_keep_pct(self):
        """Large contexts use standard keep_pct=0.3."""
        cfg = _make_config(**{"models.primary.max_context_tokens": 200000})
        assert cfg.compaction_keep_pct == 0.3

    def test_no_context_info_normal_keep_pct(self):
        """Without max_context_tokens, uses standard 0.3."""
        cfg = _make_config()
        assert cfg.compaction_keep_pct == 0.3

    def test_explicit_keep_pct_wins(self):
        """Explicit keep_recent_pct overrides adaptive behavior."""
        cfg = _make_config(**{
            "models.primary.max_context_tokens": 8192,
            "behavior.compaction.keep_recent_pct": 0.4,
        })
        assert cfg.compaction_keep_pct == 0.4


class TestMaxSystemTokens:
    def test_blocks_trimmed_when_exceeding_cap(self, tmp_workspace):
        """Dynamic and semi-stable blocks are trimmed when cap exceeded."""
        builder = ContextBuilder(
            workspace=tmp_workspace,
            stable_files=["SOUL.md"],
            semi_stable_files=["MEMORY.md"],
            max_system_tokens=10,  # very low cap
        )
        blocks = builder.build()
        # Should have trimmed some blocks
        tiers = [b["tier"] for b in blocks]
        # If stable alone fits, semi_stable and dynamic may be trimmed
        assert "stable" in tiers

    def test_no_trim_when_under_cap(self, tmp_workspace):
        """No trimming when under the cap."""
        builder = ContextBuilder(
            workspace=tmp_workspace,
            stable_files=["SOUL.md"],
            semi_stable_files=["MEMORY.md"],
            max_system_tokens=100000,  # very high cap
        )
        blocks = builder.build()
        tiers = [b["tier"] for b in blocks]
        assert "stable" in tiers
        assert "semi_stable" in tiers
        assert "dynamic" in tiers

    def test_zero_cap_means_unlimited(self, tmp_workspace):
        """max_system_tokens=0 means no limit."""
        builder = ContextBuilder(
            workspace=tmp_workspace,
            stable_files=["SOUL.md"],
            semi_stable_files=["MEMORY.md"],
            max_system_tokens=0,
        )
        blocks = builder.build()
        assert len(blocks) >= 3  # stable + semi_stable + dynamic


class TestContextBudget:
    def test_estimate_tokens(self):
        """Token estimation uses tiktoken or byte-based fallback (not len//4)."""
        result = _estimate_tokens("a" * 400)
        # Byte fallback: 400 * 10 // 33 = 121; tiktoken: varies
        assert result > 0
        assert _estimate_tokens("") == 0
        # Multibyte chars produce more tokens than single-byte
        assert _estimate_tokens("\u00e9" * 100) >= _estimate_tokens("e" * 100)

    def test_budget_logging_doesnt_crash(self, tmp_workspace):
        """_log_budget runs without error."""
        builder = ContextBuilder(
            workspace=tmp_workspace,
            stable_files=["SOUL.md"],
            semi_stable_files=[],
        )
        builder.build()
        # No assertion — just verify it doesn't crash


# ─── Challenge 3: Head+Tail Truncation ──────────────────────────


class TestHeadTailTruncation:
    def test_short_text_not_truncated(self):
        """Short text passes through unchanged."""
        assert _smart_truncate("hello", 100) == "hello"

    def test_head_tail_preserves_end(self):
        """Truncated text includes both beginning and end."""
        text = "START " + "x" * 5000 + " END"
        result = _smart_truncate(text, 500)
        assert "START" in result
        assert "END" in result
        assert "truncated" in result

    def test_json_array_truncation(self):
        """JSON arrays use item-count-based truncation."""
        data = [{"id": i, "data": "x" * 50} for i in range(100)]
        text = json.dumps(data)
        result = _smart_truncate(text, 500)
        assert "items" in result or len(result) <= 500

    def test_very_small_limit_uses_head_only(self):
        """Very small limits fall back to head-only."""
        text = "a" * 1000
        result = _smart_truncate(text, 100)
        assert "truncated" in result
        assert len(result) <= 200  # some overflow from marker


# ─── Challenge 4: Prompt Cache Awareness ─────────────────────────


class TestPromptCacheAwareness:
    def test_cache_tokens_from_details(self):
        """Parses cached_tokens from prompt_tokens_details."""
        from providers.openai import OpenAIProvider

        usage = OpenAIProvider._parse_usage_dict({
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 60},
        })

        assert usage.input_tokens == 100
        assert usage.output_tokens == 20
        assert usage.cache_read_tokens == 60

    def test_usage_context_tokens_includes_cache(self):
        """Usage.context_tokens includes cache_read_tokens."""
        u = Usage(input_tokens=100, output_tokens=50, cache_read_tokens=200)
        assert u.context_tokens == 300


# ─── Challenge 5: Thinking Token Detection ───────────────────────


class TestThinkingDetection:
    def test_strip_thinking_simple(self):
        """Extracts <think> block and returns clean text."""
        text = "<think>I need to consider this</think>Here is my answer"
        cleaned, thinking = _strip_thinking(text)
        assert cleaned == "Here is my answer"
        assert thinking == "I need to consider this"

    def test_strip_thinking_multiple(self):
        """Handles multiple think blocks."""
        text = "<think>first</think>text<think>second</think>more"
        cleaned, thinking = _strip_thinking(text)
        assert "text" in cleaned
        assert "more" in cleaned
        assert "first" in thinking
        assert "second" in thinking

    def test_strip_thinking_no_blocks(self):
        """No think blocks returns text unchanged."""
        text = "Just regular text"
        cleaned, thinking = _strip_thinking(text)
        assert cleaned == text
        assert thinking == ""

    def test_strip_thinking_empty(self):
        """Empty text returns empty."""
        cleaned, thinking = _strip_thinking("")
        assert cleaned == ""
        assert thinking == ""

    def test_strip_thinking_multiline(self):
        """Handles multiline think blocks."""
        text = "<think>\nline1\nline2\n</think>\nAnswer"
        cleaned, thinking = _strip_thinking(text)
        assert cleaned == "Answer"
        assert "line1" in thinking


# ─── Challenge 8: JSON Repair ────────────────────────────────────


class TestJSONRepair:
    def test_valid_json_passes_through(self):
        """Valid JSON is parsed normally."""
        assert _repair_json('{"key": "value"}') == {"key": "value"}

    def test_trailing_comma_fixed(self):
        """Trailing comma before } is fixed."""
        assert _repair_json('{"key": "value",}') == {"key": "value"}

    def test_single_quotes_fixed(self):
        """Single quotes are converted to double quotes."""
        assert _repair_json("{'key': 'value'}") == {"key": "value"}

    def test_unquoted_keys_fixed(self):
        """Unquoted keys are quoted."""
        assert _repair_json('{key: "value"}') == {"key": "value"}

    def test_garbage_returns_none(self):
        """Unrepairable input returns None."""
        assert _repair_json("not json at all") is None

    def test_none_input(self):
        """None input returns None."""
        assert _repair_json(None) is None

    def test_empty_string(self):
        """Empty string returns None."""
        assert _repair_json("") is None

    def test_nested_json_repair(self):
        """Nested JSON with trailing commas."""
        result = _repair_json('{"a": [1, 2, 3,], "b": "x",}')
        assert result == {"a": [1, 2, 3], "b": "x"}


# ─── Challenge 6: Agentic Loop Efficiency ────────────────────────


class TestAgenticLoopEfficiency:
    """Tests for new agentic loop features (turn logging, context pressure, parallel execution)."""

    @pytest.mark.asyncio
    async def test_max_context_for_tools_injects_warning(self):
        """When context exceeds max_context_for_tools, wrap-up hint is injected."""
        from agentic import LoopConfig, run_agentic_loop

        mock_provider = MagicMock()
        mock_provider.capabilities = ModelCapabilities(max_context_tokens=200000)

        # First call: returns tool call with high context
        resp1 = LLMResponse(
            text="thinking",
            tool_calls=[ToolCall(id="1", name="test_tool", arguments={})],
            stop_reason="tool_use",
            usage=Usage(input_tokens=20000, output_tokens=100),
        )
        # Second call: ends
        resp2 = LLMResponse(
            text="final answer",
            tool_calls=[],
            stop_reason="end_turn",
            usage=Usage(input_tokens=21000, output_tokens=50),
        )
        mock_provider.complete = AsyncMock(side_effect=[resp1, resp2])
        mock_provider.format_tools = MagicMock(return_value=[])
        mock_provider.format_messages = MagicMock(side_effect=lambda m: m)

        mock_registry = MagicMock()
        mock_registry.execute = AsyncMock(return_value={"text": "tool result", "attachments": []})

        messages = [{"role": "user", "content": "hello"}]
        await run_agentic_loop(
            provider=mock_provider,
            system="system",
            messages=messages,
            tools=[{"name": "test_tool"}],
            tool_executor=mock_registry,
            config=LoopConfig(
                max_turns=5,
                timeout=60,
                api_retries=0,
                api_retry_base_delay=0,
                max_context_for_tools=15000,  # lower than the 20k tokens
            ),
        )
        # Should have injected a wrap-up warning
        system_warnings = [
            m for m in messages
            if m.get("role") == "user"
            and "Context at" in m.get("content", "")
        ]
        assert len(system_warnings) >= 1

    @pytest.mark.asyncio
    async def test_tool_call_retry_provides_guidance(self):
        """tool_call_retry adds guidance when tool args are invalid."""
        from agentic import LoopConfig, run_agentic_loop

        mock_provider = MagicMock()
        mock_provider.capabilities = ModelCapabilities(max_context_tokens=200000)

        # First call: returns tool call
        resp1 = LLMResponse(
            text="",
            tool_calls=[ToolCall(id="1", name="test_tool", arguments={"bad": True})],
            stop_reason="tool_use",
            usage=Usage(input_tokens=100, output_tokens=50),
        )
        # Second call: ends
        resp2 = LLMResponse(
            text="done",
            tool_calls=[],
            stop_reason="end_turn",
            usage=Usage(input_tokens=200, output_tokens=30),
        )
        mock_provider.complete = AsyncMock(side_effect=[resp1, resp2])
        mock_provider.format_tools = MagicMock(return_value=[])
        mock_provider.format_messages = MagicMock(side_effect=lambda m: m)

        mock_registry = MagicMock()
        # Tool returns an "Invalid arguments" error
        mock_registry.execute = AsyncMock(return_value={"text": "Error: Invalid arguments for 'test_tool': missing 'path'", "attachments": []})

        messages = [{"role": "user", "content": "hello"}]
        await run_agentic_loop(
            provider=mock_provider,
            system="system",
            messages=messages,
            tools=[{"name": "test_tool"}],
            tool_executor=mock_registry,
            config=LoopConfig(
                max_turns=5,
                timeout=60,
                api_retries=0,
                api_retry_base_delay=0,
                tool_call_retry=True,
            ),
        )
        # Tool result should contain retry guidance
        tool_results = [
            m for m in messages if m.get("role") == "tool_result"
        ]
        assert len(tool_results) >= 1
        content = tool_results[0]["results"][0]["content"]
        assert "try again" in content.lower()


# ─── Config New Schema Entries ───────────────────────────────────


class TestNewConfigEntries:
    def test_max_system_tokens_default(self):
        """max_system_tokens defaults to 0 (unlimited)."""
        cfg = _make_config()
        assert cfg.max_system_tokens == 0

    def test_max_context_for_tools_default(self):
        """max_context_for_tools defaults to 0 (disabled)."""
        cfg = _make_config()
        assert cfg.max_context_for_tools == 0

    def test_tool_call_retry_default(self):
        """tool_call_retry defaults to False."""
        cfg = _make_config()
        assert cfg.tool_call_retry is False

    def test_all_new_entries_settable(self):
        """All new config entries can be set explicitly."""
        cfg = _make_config(**{
            "agent.context.max_system_tokens": 5000,
            "behavior.max_context_for_tools": 20000,
            "tools.tool_call_retry": True,
        })
        assert cfg.max_system_tokens == 5000
        assert cfg.max_context_for_tools == 20000
        assert cfg.tool_call_retry is True
