"""Tests for synthesis.py — memory recall synthesis layer."""

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from synthesis import PROMPTS, VALID_STYLES, SynthesisResult, synthesize_recall


@dataclass
class FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 50
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


def _make_provider(response_text: str):
    """Create a mock provider returning the given text."""

    @dataclass
    class FakeResponse:
        text: str
        usage: FakeUsage = None

        def __post_init__(self):
            if self.usage is None:
                self.usage = FakeUsage()

    provider = AsyncMock()
    provider.format_system.return_value = "system"
    provider.format_messages.return_value = "messages"
    provider.complete.return_value = FakeResponse(text=response_text)
    return provider


SAMPLE_RECALL = (
    "[Known facts]\n"
    "  nicolas — lives in: Austria\n"
    "  lucy — role: companion\n\n"
    "[Recent conversations]\n"
    "  [2026-02-23] Discussed memory architecture (tone: productive)\n\n"
    "[Open commitments]\n"
    "  #1 - nicolas: review PR (by 2026-02-25)\n\n"
    "[Memory loaded: Known facts, Recent conversations, Open commitments | 342/1500 tokens used]"
)


# ─── Passthrough ────────────────────────────────────────────────


class TestStructuredPassthrough:
    @pytest.mark.asyncio
    async def test_structured_returns_input_unchanged(self):
        provider = _make_provider("should not be called")
        result = await synthesize_recall(SAMPLE_RECALL, "structured", provider)
        assert isinstance(result, SynthesisResult)
        assert result.text == SAMPLE_RECALL
        assert result.usage is None
        provider.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_input_returns_empty(self):
        provider = _make_provider("should not be called")
        result = await synthesize_recall("", "narrative", provider)
        assert result.text == ""
        assert result.usage is None
        provider.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_whitespace_only_returns_unchanged(self):
        provider = _make_provider("should not be called")
        result = await synthesize_recall("   \n  ", "narrative", provider)
        assert result.text == "   \n  "
        assert result.usage is None
        provider.complete.assert_not_called()


# ─── Fallback ───────────────────────────────────────────────────


class TestFallback:
    @pytest.mark.asyncio
    async def test_provider_failure_returns_raw(self):
        provider = _make_provider("")
        provider.complete.side_effect = Exception("API down")
        result = await synthesize_recall(SAMPLE_RECALL, "narrative", provider)
        assert result.text == SAMPLE_RECALL
        assert result.usage is None

    @pytest.mark.asyncio
    async def test_empty_response_returns_raw(self):
        provider = _make_provider("")
        result = await synthesize_recall(SAMPLE_RECALL, "narrative", provider)
        assert result.text == SAMPLE_RECALL
        # Usage is still returned even on empty response fallback
        assert result.usage is not None

    @pytest.mark.asyncio
    async def test_whitespace_response_returns_raw(self):
        provider = _make_provider("   \n  ")
        result = await synthesize_recall(SAMPLE_RECALL, "narrative", provider)
        assert result.text == SAMPLE_RECALL

    @pytest.mark.asyncio
    async def test_none_response_returns_raw(self):
        @dataclass
        class NoneResponse:
            text: str = None
            usage: FakeUsage = None

            def __post_init__(self):
                if self.usage is None:
                    self.usage = FakeUsage()

        provider = AsyncMock()
        provider.format_system.return_value = "system"
        provider.format_messages.return_value = "messages"
        provider.complete.return_value = NoneResponse()
        result = await synthesize_recall(SAMPLE_RECALL, "narrative", provider)
        assert result.text == SAMPLE_RECALL

    @pytest.mark.asyncio
    async def test_unknown_style_returns_raw(self):
        provider = _make_provider("should not be called")
        result = await synthesize_recall(SAMPLE_RECALL, "banana", provider)
        assert result.text == SAMPLE_RECALL
        assert result.usage is None
        provider.complete.assert_not_called()


# ─── Synthesis ──────────────────────────────────────────────────


class TestSynthesis:
    @pytest.mark.asyncio
    async def test_narrative_calls_provider(self):
        provider = _make_provider("Nicolas went from zero to near-launch in under a month.")
        result = await synthesize_recall(SAMPLE_RECALL, "narrative", provider)
        assert result.text.startswith("Nicolas went from zero")
        assert result.usage is not None
        provider.complete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_factual_calls_provider(self):
        provider = _make_provider("Nicolas lives in Austria. Lucy serves as his companion.")
        result = await synthesize_recall(SAMPLE_RECALL, "factual", provider)
        assert "Austria" in result.text
        assert result.usage is not None
        provider.complete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_provider_receives_correct_format(self):
        provider = _make_provider("synthesized output")
        await synthesize_recall(SAMPLE_RECALL, "narrative", provider)

        provider.format_system.assert_called_once_with([])
        provider.format_messages.assert_called_once()
        # Check that the prompt contains the recall text
        call_args = provider.format_messages.call_args[0][0]
        assert len(call_args) == 1
        assert call_args[0]["role"] == "user"
        assert "nicolas — lives in: Austria" in call_args[0]["content"]

    @pytest.mark.asyncio
    async def test_prompt_contains_recall_text(self):
        provider = _make_provider("synthesized output")
        await synthesize_recall(SAMPLE_RECALL, "narrative", provider)

        call_args = provider.format_messages.call_args[0][0]
        prompt = call_args[0]["content"]
        # Prompt should contain the template + the raw recall text
        assert "MEMORY BLOCKS:" in prompt
        assert "nicolas — lives in: Austria" in prompt
        assert "OUTPUT:" in prompt


# ─── Footer Preservation ────────────────────────────────────────


class TestFooterPreservation:
    @pytest.mark.asyncio
    async def test_memory_loaded_footer_preserved(self):
        provider = _make_provider("Synthesized paragraph here.")
        result = await synthesize_recall(SAMPLE_RECALL, "narrative", provider)
        assert "[Memory loaded:" in result.text
        assert "342/1500 tokens used" in result.text

    @pytest.mark.asyncio
    async def test_dropped_footer_preserved(self):
        text_with_dropped = (
            "[Known facts]\n  fact1\n\n"
            "[Memory loaded: Known facts | 100/1500 tokens used]\n"
            "[Dropped (over budget): Memory search — use memory_search to access]"
        )
        provider = _make_provider("Synthesized paragraph.")
        result = await synthesize_recall(text_with_dropped, "factual", provider)
        assert "[Memory loaded:" in result.text
        assert "[Dropped (over budget):" in result.text

    @pytest.mark.asyncio
    async def test_no_footer_when_absent(self):
        text_no_footer = "[Known facts]\n  nicolas — lives in: Austria"
        provider = _make_provider("Nicolas lives in Austria.")
        result = await synthesize_recall(text_no_footer, "factual", provider)
        assert result.text == "Nicolas lives in Austria."
        assert "[Memory loaded:" not in result.text


# ─── Prompt Registry ────────────────────────────────────────────


class TestPromptRegistry:
    def test_narrative_prompt_exists(self):
        assert "narrative" in PROMPTS

    def test_factual_prompt_exists(self):
        assert "factual" in PROMPTS

    def test_structured_not_in_prompts(self):
        assert "structured" not in PROMPTS

    def test_all_prompts_have_recall_placeholder(self):
        for style, prompt in PROMPTS.items():
            assert "{recall_text}" in prompt, f"{style} prompt missing placeholder"

    def test_valid_styles_complete(self):
        assert VALID_STYLES == {"structured", "narrative", "factual"}


# ─── Tool Path Integration ──────────────────────────────────────


class TestToolPathSynthesis:
    """Test that memory_search tool applies synthesis when configured."""

    @pytest.fixture(autouse=True)
    def _reset_globals(self):
        """Reset memory_tools module globals before/after each test."""
        from tools import memory_tools
        old_memory = memory_tools._memory
        old_conn = memory_tools._conn
        old_config = memory_tools._config
        old_synth = memory_tools._synth_provider
        yield
        memory_tools._memory = old_memory
        memory_tools._conn = old_conn
        memory_tools._config = old_config
        memory_tools._synth_provider = old_synth

    @pytest.mark.asyncio
    async def test_synthesis_applied_when_configured(self):
        from tools.memory_tools import (
            set_memory,
            set_structured_memory,
            set_synthesis_provider,
            tool_memory_search,
        )

        # Mock memory interface (vector search — not used when structured works)
        memory_iface = AsyncMock()

        # Mock config with narrative style
        config = type("Config", (), {
            "recall_max_dynamic_tokens": 1500,
            "recall_synthesis_style": "narrative",
            "recall_max_facts": 20,
            "recall_decay_rate": 0.03,
            "recall_fact_format": "natural",
            "recall_show_emotional_tone": True,
            "recall_priority_facts": 15,
            "recall_priority_episodes": 25,
            "recall_priority_vector": 35,
            "recall_priority_commitments": 40,
            "recall_episode_section_header": "Recent conversations",
        })()

        # Set up structured memory with a real DB
        import sqlite3
        from memory_schema import ensure_schema
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)
        conn.execute(
            "INSERT INTO facts (entity, attribute, value, confidence) "
            "VALUES ('test', 'attr', 'val', 1.0)"
        )
        conn.commit()

        set_memory(memory_iface)
        set_structured_memory(conn, config)

        synth_provider = _make_provider("Synthesized tool output.")
        set_synthesis_provider(synth_provider)

        result = await tool_memory_search("test")
        assert "Synthesized tool output." in result
        synth_provider.complete.assert_awaited_once()
        conn.close()

    @pytest.mark.asyncio
    async def test_no_synthesis_when_structured(self):
        from tools.memory_tools import (
            set_memory,
            set_structured_memory,
            set_synthesis_provider,
            tool_memory_search,
        )

        memory_iface = AsyncMock()
        config = type("Config", (), {
            "recall_max_dynamic_tokens": 1500,
            "recall_synthesis_style": "structured",
            "recall_max_facts": 20,
            "recall_decay_rate": 0.03,
            "recall_fact_format": "natural",
            "recall_show_emotional_tone": True,
            "recall_priority_facts": 15,
            "recall_priority_episodes": 25,
            "recall_priority_vector": 35,
            "recall_priority_commitments": 40,
            "recall_episode_section_header": "Recent conversations",
        })()

        import sqlite3
        from memory_schema import ensure_schema
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)
        conn.execute(
            "INSERT INTO facts (entity, attribute, value, confidence) "
            "VALUES ('test', 'attr', 'val', 1.0)"
        )
        conn.commit()

        set_memory(memory_iface)
        set_structured_memory(conn, config)

        synth_provider = _make_provider("Should not appear.")
        set_synthesis_provider(synth_provider)

        result = await tool_memory_search("test")
        # Raw recall, not synthesized
        assert "test" in result
        assert "Should not appear" not in result
        synth_provider.complete.assert_not_called()
        conn.close()

    @pytest.mark.asyncio
    async def test_no_synthesis_without_provider(self):
        from tools.memory_tools import (
            set_memory,
            set_structured_memory,
            tool_memory_search,
        )

        memory_iface = AsyncMock()
        config = type("Config", (), {
            "recall_max_dynamic_tokens": 1500,
            "recall_synthesis_style": "narrative",
            "recall_max_facts": 20,
            "recall_decay_rate": 0.03,
            "recall_fact_format": "natural",
            "recall_show_emotional_tone": True,
            "recall_priority_facts": 15,
            "recall_priority_episodes": 25,
            "recall_priority_vector": 35,
            "recall_priority_commitments": 40,
            "recall_episode_section_header": "Recent conversations",
        })()

        import sqlite3
        from memory_schema import ensure_schema
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)
        conn.execute(
            "INSERT INTO facts (entity, attribute, value, confidence) "
            "VALUES ('test', 'attr', 'val', 1.0)"
        )
        conn.commit()

        set_memory(memory_iface)
        set_structured_memory(conn, config)
        # No set_synthesis_provider call — provider stays None

        result = await tool_memory_search("test")
        # Should still return raw recall without crashing
        assert "test" in result
        conn.close()
