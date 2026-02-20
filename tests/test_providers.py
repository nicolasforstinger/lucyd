"""Tests for providers/ — LLMResponse, Anthropic formatting, OpenAI formatting.

Uses pytest.importorskip for SDK-dependent tests — skips if anthropic/openai
are not installed. Formatting tests use the provider classes directly
(no API calls).
"""

import json

import pytest

from providers import LLMResponse, ToolCall, Usage, create_provider

# ─── LLMResponse ─────────────────────────────────────────────────

class TestLLMResponse:
    def test_to_internal_text_only(self):
        resp = LLMResponse(
            text="Hello",
            tool_calls=[],
            stop_reason="end_turn",
            usage=Usage(input_tokens=10, output_tokens=5),
        )
        msg = resp.to_internal_message()
        assert msg["role"] == "assistant"
        assert msg["text"] == "Hello"
        assert "tool_calls" not in msg

    def test_to_internal_with_tool_calls(self):
        resp = LLMResponse(
            text=None,
            tool_calls=[ToolCall(id="tc-1", name="read", arguments={"path": "/tmp"})],
            stop_reason="tool_use",
            usage=Usage(input_tokens=10, output_tokens=5),
        )
        msg = resp.to_internal_message()
        assert len(msg["tool_calls"]) == 1
        assert msg["tool_calls"][0]["name"] == "read"

    def test_to_internal_with_thinking(self):
        resp = LLMResponse(
            text="Answer",
            tool_calls=[],
            stop_reason="end_turn",
            usage=Usage(),
            thinking="Let me think...",
        )
        msg = resp.to_internal_message()
        assert msg["thinking"] == "Let me think..."

    def test_to_internal_with_thinking_block(self):
        block = {"type": "thinking", "thinking": "deep thoughts", "signature": "abc123"}
        resp = LLMResponse(
            text="Result",
            tool_calls=[],
            stop_reason="end_turn",
            usage=Usage(),
            _thinking_block=block,
        )
        msg = resp.to_internal_message()
        assert msg["thinking_block"] == block


# ─── Anthropic Provider ──────────────────────────────────────────

class TestAnthropicFormatTools:
    @pytest.fixture(autouse=True)
    def _skip_if_no_sdk(self):
        pytest.importorskip("anthropic")

    def _make_provider(self, **kwargs):
        from providers.anthropic_compat import AnthropicCompatProvider
        defaults = dict(api_key="test-key", model="test-model")
        defaults.update(kwargs)
        return AnthropicCompatProvider(**defaults)

    def test_passthrough_name_desc_schema(self):
        p = self._make_provider()
        tools = [{"name": "t1", "description": "desc1", "input_schema": {"type": "object"}}]
        result = p.format_tools(tools)
        assert result[0]["name"] == "t1"
        assert result[0]["description"] == "desc1"
        assert result[0]["input_schema"] == {"type": "object"}

    def test_empty_list(self):
        p = self._make_provider()
        assert p.format_tools([]) == []


class TestAnthropicFormatSystem:
    @pytest.fixture(autouse=True)
    def _skip_if_no_sdk(self):
        pytest.importorskip("anthropic")

    def _make_provider(self, **kwargs):
        from providers.anthropic_compat import AnthropicCompatProvider
        defaults = dict(api_key="test-key", model="test-model")
        defaults.update(kwargs)
        return AnthropicCompatProvider(**defaults)

    def test_cache_control_on_stable_block(self):
        p = self._make_provider(cache_control=True)
        blocks = [{"text": "personality", "tier": "stable"}]
        result = p.format_system(blocks)
        assert result[0]["cache_control"] == {"type": "ephemeral"}

    def test_no_cache_control_on_dynamic(self):
        p = self._make_provider(cache_control=True)
        blocks = [{"text": "current time", "tier": "dynamic"}]
        result = p.format_system(blocks)
        assert "cache_control" not in result[0]

    def test_cache_disabled(self):
        p = self._make_provider(cache_control=False)
        blocks = [{"text": "personality", "tier": "stable"}]
        result = p.format_system(blocks)
        assert "cache_control" not in result[0]


class TestAnthropicFormatMessages:
    @pytest.fixture(autouse=True)
    def _skip_if_no_sdk(self):
        pytest.importorskip("anthropic")

    def _make_provider(self, **kwargs):
        from providers.anthropic_compat import AnthropicCompatProvider
        defaults = dict(api_key="test-key", model="test-model")
        defaults.update(kwargs)
        return AnthropicCompatProvider(**defaults)

    def test_user_message(self):
        p = self._make_provider()
        msgs = [{"role": "user", "content": "Hello"}]
        result = p.format_messages(msgs)
        assert result[0]["role"] == "user"
        assert result[0]["content"] == "Hello"

    def test_assistant_with_thinking(self):
        p = self._make_provider()
        msgs = [{"role": "assistant", "text": "Reply", "thinking": "hmm"}]
        result = p.format_messages(msgs)
        content = result[0]["content"]
        types = [b["type"] for b in content]
        assert "thinking" in types
        assert "text" in types

    def test_assistant_with_tool_calls(self):
        p = self._make_provider()
        msgs = [{"role": "assistant", "tool_calls": [
            {"id": "tc1", "name": "read", "arguments": {"path": "/tmp"}}
        ]}]
        result = p.format_messages(msgs)
        content = result[0]["content"]
        assert content[0]["type"] == "tool_use"
        assert content[0]["name"] == "read"

    def test_tool_results_become_user_role(self):
        p = self._make_provider()
        msgs = [{"role": "tool_results", "results": [
            {"tool_call_id": "tc1", "content": "file contents"}
        ]}]
        result = p.format_messages(msgs)
        assert result[0]["role"] == "user"
        assert result[0]["content"][0]["type"] == "tool_result"


class TestAnthropicThinkingParam:
    @pytest.fixture(autouse=True)
    def _skip_if_no_sdk(self):
        pytest.importorskip("anthropic")

    def _make_provider(self, **kwargs):
        from providers.anthropic_compat import AnthropicCompatProvider
        defaults = dict(api_key="test-key", model="test-model")
        defaults.update(kwargs)
        return AnthropicCompatProvider(**defaults)

    def test_adaptive_mode(self):
        p = self._make_provider(thinking_mode="adaptive")
        param = p._build_thinking_param()
        assert param["type"] == "adaptive"

    def test_budgeted_mode(self):
        p = self._make_provider(thinking_mode="budgeted", thinking_budget=5000)
        param = p._build_thinking_param()
        assert param["type"] == "enabled"
        assert param["budget_tokens"] == 5000

    def test_disabled_returns_none(self):
        p = self._make_provider(thinking_mode="disabled")
        assert p._build_thinking_param() is None


# ─── OpenAI-Compat Provider ─────────────────────────────────────

class TestOpenAIFormatTools:
    @pytest.fixture(autouse=True)
    def _skip_if_no_sdk(self):
        pytest.importorskip("openai")

    def _make_provider(self, **kwargs):
        from providers.openai_compat import OpenAICompatProvider
        defaults = dict(api_key="test-key", model="test-model")
        defaults.update(kwargs)
        return OpenAICompatProvider(**defaults)

    def test_wraps_in_type_function(self):
        p = self._make_provider()
        tools = [{"name": "t1", "description": "d", "input_schema": {"type": "object"}}]
        result = p.format_tools(tools)
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "t1"

    def test_input_schema_becomes_parameters(self):
        p = self._make_provider()
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        tools = [{"name": "t1", "description": "d", "input_schema": schema}]
        result = p.format_tools(tools)
        assert result[0]["function"]["parameters"] == schema


class TestOpenAIFormatMessages:
    @pytest.fixture(autouse=True)
    def _skip_if_no_sdk(self):
        pytest.importorskip("openai")

    def _make_provider(self, **kwargs):
        from providers.openai_compat import OpenAICompatProvider
        defaults = dict(api_key="test-key", model="test-model")
        defaults.update(kwargs)
        return OpenAICompatProvider(**defaults)

    def test_user_message(self):
        p = self._make_provider()
        msgs = [{"role": "user", "content": "Hi"}]
        result = p.format_messages(msgs)
        assert result[0]["role"] == "user"
        assert result[0]["content"] == "Hi"

    def test_assistant_with_tool_calls(self):
        p = self._make_provider()
        msgs = [{"role": "assistant", "text": "", "tool_calls": [
            {"id": "tc1", "name": "exec", "arguments": {"cmd": "ls"}}
        ]}]
        result = p.format_messages(msgs)
        tc = result[0]["tool_calls"][0]
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "exec"
        # Arguments should be JSON string
        assert json.loads(tc["function"]["arguments"]) == {"cmd": "ls"}

    def test_tool_results_expanded(self):
        p = self._make_provider()
        msgs = [{"role": "tool_results", "results": [
            {"tool_call_id": "tc1", "content": "output1"},
            {"tool_call_id": "tc2", "content": "output2"},
        ]}]
        result = p.format_messages(msgs)
        # Each tool result becomes a separate message
        assert len(result) == 2
        assert result[0]["role"] == "tool"
        assert result[1]["role"] == "tool"

    def test_user_message_with_neutral_image_blocks(self):
        """Neutral image blocks are converted to OpenAI image_url format."""
        p = self._make_provider()
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "describe this"},
            {"type": "image", "media_type": "image/jpeg", "data": "base64data"},
        ]}]
        result = p.format_messages(msgs)
        content = result[0]["content"]
        assert len(content) == 2
        # Text block passes through
        assert content[0] == {"type": "text", "text": "describe this"}
        # Image block converted to OpenAI data URI format
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"] == "data:image/jpeg;base64,base64data"

    def test_user_message_string_unchanged(self):
        """Plain string content is not affected by image conversion."""
        p = self._make_provider()
        msgs = [{"role": "user", "content": "just text"}]
        result = p.format_messages(msgs)
        assert result[0]["content"] == "just text"

    def test_multiple_images_converted(self):
        """Multiple image blocks all converted to data URIs."""
        p = self._make_provider()
        msgs = [{"role": "user", "content": [
            {"type": "image", "media_type": "image/png", "data": "img1"},
            {"type": "text", "text": "compare"},
            {"type": "image", "media_type": "image/jpeg", "data": "img2"},
        ]}]
        result = p.format_messages(msgs)
        content = result[0]["content"]
        images = [b for b in content if b.get("type") == "image_url"]
        assert len(images) == 2
        assert "image/png" in images[0]["image_url"]["url"]
        assert "image/jpeg" in images[1]["image_url"]["url"]


# ─── TEST-10: Additional provider dataclass and factory tests ────


class TestUsageDefaults:
    """Verify Usage dataclass defaults to zero for all fields."""

    def test_all_defaults_zero(self):
        u = Usage()
        assert u.input_tokens == 0
        assert u.output_tokens == 0
        assert u.cache_read_tokens == 0
        assert u.cache_write_tokens == 0

    def test_partial_override(self):
        u = Usage(input_tokens=100, output_tokens=50)
        assert u.input_tokens == 100
        assert u.output_tokens == 50
        assert u.cache_read_tokens == 0
        assert u.cache_write_tokens == 0


class TestToolCallFields:
    """Verify ToolCall dataclass stores fields correctly."""

    def test_fields_stored(self):
        tc = ToolCall(id="tc-42", name="read_file", arguments={"path": "/tmp/x"})
        assert tc.id == "tc-42"
        assert tc.name == "read_file"
        assert tc.arguments == {"path": "/tmp/x"}

    def test_arguments_dict_type(self):
        tc = ToolCall(id="tc-1", name="exec", arguments={})
        assert isinstance(tc.arguments, dict)


class TestCreateProviderFactory:
    """Verify create_provider raises for unknown types."""

    def test_raises_for_unknown_provider(self):
        with pytest.raises(ValueError, match="Unknown provider type"):
            create_provider({"provider": "nonexistent", "model": "x"}, api_key="k")

    def test_raises_for_empty_provider(self):
        with pytest.raises(ValueError, match="Unknown provider type"):
            create_provider({"model": "x"}, api_key="k")

    def test_anthropic_compat_creates_provider(self):
        """create_provider returns AnthropicCompatProvider for 'anthropic-compat'."""
        pytest.importorskip("anthropic")
        from providers.anthropic_compat import AnthropicCompatProvider
        p = create_provider(
            {"provider": "anthropic-compat", "model": "claude-test", "max_tokens": 1024},
            api_key="test-key",
        )
        assert isinstance(p, AnthropicCompatProvider)

    def test_openai_compat_creates_provider(self):
        """create_provider returns OpenAICompatProvider for 'openai-compat'."""
        pytest.importorskip("openai")
        from providers.openai_compat import OpenAICompatProvider
        p = create_provider(
            {"provider": "openai-compat", "model": "gpt-test", "max_tokens": 1024},
            api_key="test-key",
        )
        assert isinstance(p, OpenAICompatProvider)


class TestLLMResponseEdgeCases:
    """Additional LLMResponse.to_internal_message edge cases."""

    def test_no_text_no_tools_minimal_message(self):
        """Response with no text and no tool calls still has role and usage."""
        resp = LLMResponse(
            text=None,
            tool_calls=[],
            stop_reason="end_turn",
            usage=Usage(input_tokens=5, output_tokens=2),
        )
        msg = resp.to_internal_message()
        assert msg["role"] == "assistant"
        assert "text" not in msg
        assert "tool_calls" not in msg
        assert msg["usage"]["input_tokens"] == 5

    def test_usage_cache_fields_included(self):
        """Cache token fields are included in the internal message usage."""
        resp = LLMResponse(
            text="cached",
            tool_calls=[],
            stop_reason="end_turn",
            usage=Usage(input_tokens=10, output_tokens=5, cache_read_tokens=8, cache_write_tokens=3),
        )
        msg = resp.to_internal_message()
        assert msg["usage"]["cache_read_tokens"] == 8
        assert msg["usage"]["cache_write_tokens"] == 3

    def test_multiple_tool_calls(self):
        """Response with multiple tool calls serializes all of them."""
        resp = LLMResponse(
            text=None,
            tool_calls=[
                ToolCall(id="tc-1", name="read", arguments={"path": "/a"}),
                ToolCall(id="tc-2", name="write", arguments={"path": "/b", "content": "x"}),
                ToolCall(id="tc-3", name="exec", arguments={"cmd": "ls"}),
            ],
            stop_reason="tool_use",
            usage=Usage(input_tokens=100, output_tokens=50),
        )
        msg = resp.to_internal_message()
        assert len(msg["tool_calls"]) == 3
        names = [tc["name"] for tc in msg["tool_calls"]]
        assert names == ["read", "write", "exec"]



class TestAnthropicThinkingParamExtended:
    """Extended _build_thinking_param tests."""

    @pytest.fixture(autouse=True)
    def _skip_if_no_sdk(self):
        pytest.importorskip("anthropic")

    def _make_provider(self, **kwargs):
        from providers.anthropic_compat import AnthropicCompatProvider
        defaults = dict(api_key="test-key", model="test-model")
        defaults.update(kwargs)
        return AnthropicCompatProvider(**defaults)

    def test_adaptive_with_effort(self):
        """Adaptive mode includes effort when specified."""
        p = self._make_provider(thinking_mode="adaptive", thinking_effort="high")
        param = p._build_thinking_param()
        assert param["type"] == "adaptive"
        assert param["effort"] == "high"

    def test_adaptive_without_effort(self):
        """Adaptive mode omits effort key when not specified."""
        p = self._make_provider(thinking_mode="adaptive", thinking_effort="")
        param = p._build_thinking_param()
        assert param["type"] == "adaptive"
        assert "effort" not in param

    def test_fallback_enabled_flag(self):
        """When thinking_mode is empty, falls back to thinking_enabled flag."""
        p = self._make_provider(thinking_mode="", thinking_enabled=True, thinking_budget=8000)
        param = p._build_thinking_param()
        assert param["type"] == "enabled"
        assert param["budget_tokens"] == 8000

    def test_fallback_disabled_flag(self):
        """When both thinking_mode and thinking_enabled are off, returns None."""
        p = self._make_provider(thinking_mode="", thinking_enabled=False)
        param = p._build_thinking_param()
        assert param is None


class TestAnthropicFormatSystemExtended:
    """Extended format_system tests for cache_control behavior."""

    @pytest.fixture(autouse=True)
    def _skip_if_no_sdk(self):
        pytest.importorskip("anthropic")

    def _make_provider(self, **kwargs):
        from providers.anthropic_compat import AnthropicCompatProvider
        defaults = dict(api_key="test-key", model="test-model")
        defaults.update(kwargs)
        return AnthropicCompatProvider(**defaults)

    def test_semi_stable_gets_cache_control(self):
        """Semi-stable blocks also get cache_control when enabled."""
        p = self._make_provider(cache_control=True)
        blocks = [{"text": "memory content", "tier": "semi_stable"}]
        result = p.format_system(blocks)
        assert result[0]["cache_control"] == {"type": "ephemeral"}

    def test_multiple_blocks_mixed_tiers(self):
        """Only stable and semi_stable blocks get cache_control."""
        p = self._make_provider(cache_control=True)
        blocks = [
            {"text": "personality", "tier": "stable"},
            {"text": "memory", "tier": "semi_stable"},
            {"text": "runtime info", "tier": "dynamic"},
        ]
        result = p.format_system(blocks)
        assert "cache_control" in result[0]
        assert "cache_control" in result[1]
        assert "cache_control" not in result[2]

    def test_text_preserved_verbatim(self):
        """Block text is passed through without modification."""
        p = self._make_provider(cache_control=False)
        blocks = [{"text": "exact content here", "tier": "stable"}]
        result = p.format_system(blocks)
        assert result[0]["text"] == "exact content here"
        assert result[0]["type"] == "text"


class TestAnthropicFormatMessagesExtended:
    """Extended format_messages tests."""

    @pytest.fixture(autouse=True)
    def _skip_if_no_sdk(self):
        pytest.importorskip("anthropic")

    def _make_provider(self, **kwargs):
        from providers.anthropic_compat import AnthropicCompatProvider
        defaults = dict(api_key="test-key", model="test-model")
        defaults.update(kwargs)
        return AnthropicCompatProvider(**defaults)

    def test_assistant_with_thinking_block_signature(self):
        """Thinking block with signature is preserved in formatted output."""
        p = self._make_provider()
        msgs = [{
            "role": "assistant",
            "text": "Answer",
            "thinking_block": {
                "type": "thinking",
                "thinking": "deep thought",
                "signature": "sig123",
            },
        }]
        result = p.format_messages(msgs)
        content = result[0]["content"]
        thinking_blocks = [b for b in content if b.get("type") == "thinking"]
        assert len(thinking_blocks) == 1
        assert thinking_blocks[0]["signature"] == "sig123"

    def test_empty_message_list(self):
        """Empty message list returns empty result."""
        p = self._make_provider()
        assert p.format_messages([]) == []

    def test_mixed_conversation(self):
        """Full conversation with user, assistant, tool_results formats correctly."""
        p = self._make_provider()
        msgs = [
            {"role": "user", "content": "Read /tmp/x"},
            {"role": "assistant", "text": "", "tool_calls": [
                {"id": "tc1", "name": "read", "arguments": {"path": "/tmp/x"}}
            ]},
            {"role": "tool_results", "results": [
                {"tool_call_id": "tc1", "content": "file contents"}
            ]},
            {"role": "assistant", "text": "Here is the file content."},
        ]
        result = p.format_messages(msgs)
        assert len(result) == 4
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"
        assert result[2]["role"] == "user"  # tool_results -> user role
        assert result[3]["role"] == "assistant"

    def test_user_message_with_neutral_image_blocks(self):
        """Neutral image blocks are converted to Anthropic source format."""
        p = self._make_provider()
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "what is this"},
            {"type": "image", "media_type": "image/jpeg", "data": "base64data"},
        ]}]
        result = p.format_messages(msgs)
        content = result[0]["content"]
        assert len(content) == 2
        # Text block passes through
        assert content[0] == {"type": "text", "text": "what is this"}
        # Image block converted to Anthropic format
        assert content[1]["type"] == "image"
        assert content[1]["source"]["type"] == "base64"
        assert content[1]["source"]["media_type"] == "image/jpeg"
        assert content[1]["source"]["data"] == "base64data"

    def test_user_message_string_unchanged(self):
        """Plain string content is not affected by image conversion."""
        p = self._make_provider()
        msgs = [{"role": "user", "content": "just text"}]
        result = p.format_messages(msgs)
        assert result[0]["content"] == "just text"

    def test_multiple_images_converted(self):
        """Multiple image blocks all converted."""
        p = self._make_provider()
        msgs = [{"role": "user", "content": [
            {"type": "image", "media_type": "image/png", "data": "img1"},
            {"type": "text", "text": "compare these"},
            {"type": "image", "media_type": "image/jpeg", "data": "img2"},
        ]}]
        result = p.format_messages(msgs)
        content = result[0]["content"]
        images = [b for b in content if b.get("type") == "image"]
        assert len(images) == 2
        assert images[0]["source"]["media_type"] == "image/png"
        assert images[1]["source"]["media_type"] == "image/jpeg"
