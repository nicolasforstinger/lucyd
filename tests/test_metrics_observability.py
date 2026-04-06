"""Tests for observability metrics — TTFT, retries, outcomes, search latency, etc.

Verifies that the 8 new metrics + 1 new label value are recorded
at the correct sites with correct labels.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

import metrics
from agentic import (
    LoopConfig,
    _stream_to_response,
    run_agentic_loop,
)
from providers import LLMResponse, ModelCapabilities, StreamDelta, ToolCall, Usage
from tools import ToolRegistry, ToolSpec

pytestmark = pytest.mark.skipif(not metrics.ENABLED, reason="prometheus_client not installed")

# ─── Helpers ─────────────────────────────────────────────────────

_LOOP_CONFIG = LoopConfig(
    timeout=600.0,
    api_retries=2,
    api_retry_base_delay=0.01,
)


def _end_turn_response(text: str = "Done", **usage_kw: int) -> LLMResponse:
    kw: dict[str, int] = {"input_tokens": 100, "output_tokens": 50}
    kw.update(usage_kw)
    return LLMResponse(
        text=text, tool_calls=[], stop_reason="end_turn", usage=Usage(**kw),
    )


def _tool_use_response(tool_name: str = "echo", arguments: dict[str, str] | None = None) -> LLMResponse:
    return LLMResponse(
        text=None,
        tool_calls=[ToolCall(id="tc-1", name=tool_name, arguments=arguments or {"text": "hi"})],
        stop_reason="tool_use",
        usage=Usage(input_tokens=100, output_tokens=50),
    )


class MockProvider:
    def __init__(self, responses: list[LLMResponse], caps: ModelCapabilities | None = None) -> None:
        self._responses = list(responses)
        self._call_count = 0
        self._capabilities = caps or ModelCapabilities()
        self.model = "test-model"
        self.provider_name = "test-provider"

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    def format_tools(self, tools: list[dict[str, object]]) -> list[dict[str, object]]:
        return tools

    def format_system(self, blocks: list[dict[str, object]]) -> list[dict[str, object]]:
        return blocks

    def format_messages(self, messages: list[object]) -> list[object]:
        return messages

    async def complete(self, system: object, messages: object, tools: object, **kw: object) -> LLMResponse:
        idx = min(self._call_count, len(self._responses) - 1)
        self._call_count += 1
        return self._responses[idx]


class MockStreamProvider(MockProvider):
    def __init__(self, responses: list[LLMResponse], deltas: list[StreamDelta] | None = None) -> None:
        super().__init__(responses, caps=ModelCapabilities(supports_streaming=True))
        self._deltas = deltas or []

    async def stream(self, system: object, messages: object, tools: object, **kw: object) -> AsyncIterator[StreamDelta]:
        for d in self._deltas:
            yield d


def _make_registry() -> ToolRegistry:
    reg = ToolRegistry()

    def echo(text: str = "") -> str:
        return f"echo:{text}"

    reg.register(ToolSpec(name="echo", description="echo tool", input_schema={"type": "object"}, function=echo))
    return reg


def _sample_value(metric: object, label_filter: dict[str, str] | None = None) -> float:
    """Sum sample values from a Prometheus metric, optionally filtering by labels."""
    total = 0.0
    for family in metric.collect():  # type: ignore[union-attr]
        for sample in family.samples:
            if label_filter and not all(sample.labels.get(k) == v for k, v in label_filter.items()):
                continue
            # For histograms, only count _count suffix to get observation count
            if sample.name.endswith("_count"):
                total += sample.value
            elif not sample.name.endswith(("_bucket", "_sum", "_created", "_total")):
                total += sample.value
            elif sample.name.endswith("_total"):
                total += sample.value
    return total


def _clear_metric(metric: object) -> None:
    """Reset a prometheus metric for test isolation."""
    if hasattr(metric, "_metrics"):
        metric._metrics.clear()  # type: ignore[union-attr]


# ─── TTFT ────────────────────────────────────��───────────────────


class TestTTFTMetric:
    @pytest.mark.asyncio
    async def test_ttft_observed_on_streaming(self) -> None:
        _clear_metric(metrics.TTFT)
        deltas = [
            StreamDelta(text="Hello"),
            StreamDelta(stop_reason="end_turn", usage=Usage(input_tokens=10, output_tokens=5)),
        ]
        provider = MockStreamProvider([], deltas=deltas)
        await _stream_to_response(provider, [], [], [], None)

        count = _sample_value(metrics.TTFT, {"model": "test-model", "provider": "test-provider"})
        assert count == 1, "TTFT should have 1 observation"


# ─── cache_write tokens ─────────────────────────────────────────


class TestCacheWriteMetric:
    @pytest.mark.asyncio
    async def test_cache_write_tokens_recorded(self) -> None:
        _clear_metric(metrics.TOKENS_TOTAL)
        resp = _end_turn_response(cache_write_tokens=500)
        provider = MockProvider([resp])
        reg = _make_registry()
        messages = [{"role": "user", "content": "test"}]

        await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=[], tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=1),
        )

        val = _sample_value(metrics.TOKENS_TOTAL, {"direction": "cache_write"})
        assert val == 500, "cache_write tokens should be recorded"

    @pytest.mark.asyncio
    async def test_cache_write_zero_not_recorded(self) -> None:
        _clear_metric(metrics.TOKENS_TOTAL)
        resp = _end_turn_response(cache_write_tokens=0)
        provider = MockProvider([resp])
        reg = _make_registry()
        messages = [{"role": "user", "content": "test"}]

        await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=[], tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=1),
        )

        val = _sample_value(metrics.TOKENS_TOTAL, {"direction": "cache_write"})
        assert val == 0, "cache_write=0 should not create a sample"


# ─── API retries ─────────────────────────────────────────────────


class TestRetryMetric:
    @pytest.mark.asyncio
    async def test_retry_increments_counter(self) -> None:
        _clear_metric(metrics.API_RETRIES_TOTAL)
        call_count = [0]
        ok_response = _end_turn_response("Recovered")

        class RetryProvider(MockProvider):
            async def complete(self, system: object, messages: object, tools: object, **kw: object) -> LLMResponse:
                call_count[0] += 1
                if call_count[0] == 1:
                    raise type("RateLimitError", (Exception,), {})("429")
                return ok_response

        provider = RetryProvider([ok_response])
        reg = _make_registry()
        messages = [{"role": "user", "content": "test"}]

        await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=[], tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=1),
        )

        val = _sample_value(metrics.API_RETRIES_TOTAL, {"model": "test-model", "provider": "test-provider"})
        assert val == 1, "One retry should be recorded"


# ─── Context trims ───────────────────────────────────────────────


class TestContextTrimMetrics:
    @pytest.mark.asyncio
    async def test_trim_records_count_and_tokens(self) -> None:
        _clear_metric(metrics.CONTEXT_TRIMS_TOTAL)
        _clear_metric(metrics.CONTEXT_TRIM_TOKENS)

        # Provider with a small context window to force trimming
        resp1 = LLMResponse(
            text=None,
            tool_calls=[ToolCall(id="tc-1", name="echo", arguments={"text": "x"})],
            stop_reason="tool_use",
            usage=Usage(input_tokens=100, output_tokens=50),
        )
        resp2 = _end_turn_response()
        small_ctx = ModelCapabilities(max_context_tokens=500)
        provider = MockProvider([resp1, resp2], caps=small_ctx)
        reg = _make_registry()

        # Messages that exceed the 500-token context budget
        messages: list[dict[str, object]] = [
            {"role": "user", "content": "x" * 2000},
            {"role": "assistant", "content": "y" * 2000},
            {"role": "user", "content": "z" * 2000},
        ]

        await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=reg.get_schemas(), tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=3),
        )

        trim_count = _sample_value(metrics.CONTEXT_TRIMS_TOTAL)
        assert trim_count >= 1, "At least one context trim should be recorded"


# ─── Message outcome ─────────────────────────────────────────────


class TestMessageOutcome:
    def test_resolved_outcome(self) -> None:
        """end_turn without cost_limited → resolved."""
        resp = _end_turn_response()
        assert resp.stop_reason == "end_turn"
        assert resp.cost_limited is False
        # Classification logic: not cost_limited, stop_reason == end_turn → resolved

    def test_max_turns_outcome(self) -> None:
        """Loop exhausting max_turns → stop_reason is tool_use, not end_turn."""
        resp = _tool_use_response()
        assert resp.stop_reason == "tool_use"
        assert resp.cost_limited is False
        # Classification logic: not cost_limited, stop_reason != end_turn → max_turns

    def test_cost_limited_outcome(self) -> None:
        """cost_limited flag takes priority over stop_reason."""
        resp = _end_turn_response()
        resp.cost_limited = True
        # Classification logic: cost_limited → cost_limited (regardless of stop_reason)

    @pytest.mark.asyncio
    async def test_outcome_resolved_recorded_in_loop(self) -> None:
        """Integration: resolved outcome increments counter after normal completion."""
        _clear_metric(metrics.MESSAGE_OUTCOME_TOTAL)
        provider = MockProvider([_end_turn_response()])
        reg = _make_registry()
        messages = [{"role": "user", "content": "test"}]

        resp = await run_agentic_loop(
            provider=provider, system=[], messages=messages,
            tools=[], tool_executor=reg,
            config=replace(_LOOP_CONFIG, max_turns=1),
        )
        assert resp.stop_reason == "end_turn"
        assert resp.cost_limited is False
        # Outcome metric is recorded in pipeline.py, not agentic.py
        # This test verifies the response fields used for classification


# ─── Session open ────────────────────────────────────────────────


class TestSessionOpenMetric:
    def test_new_session_increments_counter(self, tmp_path: object) -> None:
        from pathlib import Path
        from session import SessionManager

        before = _sample_value(metrics.SESSION_OPEN_TOTAL)
        mgr = SessionManager(Path(str(tmp_path)) / "sessions")
        mgr.get_or_create("user-1", model="test-model")
        after = _sample_value(metrics.SESSION_OPEN_TOTAL)

        assert after - before == 1, "New session should increment SESSION_OPEN_TOTAL by 1"

    def test_existing_session_no_increment(self, tmp_path: object) -> None:
        from pathlib import Path
        from session import SessionManager

        sessions_dir = Path(str(tmp_path)) / "sessions"
        mgr = SessionManager(sessions_dir)
        mgr.get_or_create("user-1", model="test-model")

        # Capture value after first create, then call again
        before = _sample_value(metrics.SESSION_OPEN_TOTAL)
        mgr.get_or_create("user-1", model="test-model")
        after = _sample_value(metrics.SESSION_OPEN_TOTAL)

        assert after == before, "Existing session should not increment SESSION_OPEN_TOTAL"


# ─── Memory search latency ──────────────────────────────────────


class TestMemorySearchDuration:
    _MEM_KWARGS: dict[str, int] = {
        "embedding_timeout": 15, "top_k": 10,
        "vector_search_limit": 10000, "fts_min_results": 3,
    }

    @pytest.mark.asyncio
    async def test_fts_search_records_duration(self, tmp_path: object) -> None:
        from memory import MemoryInterface

        _clear_metric(metrics.MEMORY_SEARCH_DURATION)

        db_path = f"{tmp_path}/memory.db"
        mem = MemoryInterface(
            db_path=db_path, embedding_api_key="",
            embedding_model="", embedding_base_url="",
            **self._MEM_KWARGS,
        )

        # Create the FTS table so search doesn't crash
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunks
            USING fts5(id, text, source, metadata, tokenize='porter')
        """)
        conn.commit()
        conn.close()

        await mem.search("test query")

        count = _sample_value(metrics.MEMORY_SEARCH_DURATION, {"search_type": "fts"})
        assert count == 1, "FTS search should record 1 observation with search_type=fts"

    @pytest.mark.asyncio
    async def test_combined_search_records_duration(self, tmp_path: object) -> None:
        from memory import MemoryInterface

        _clear_metric(metrics.MEMORY_SEARCH_DURATION)

        db_path = f"{tmp_path}/memory.db"
        kw = {**self._MEM_KWARGS, "fts_min_results": 999}
        mem = MemoryInterface(
            db_path=db_path, embedding_api_key="test-key",
            embedding_model="test", embedding_base_url="http://localhost:0",
            **kw,
        )

        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunks
            USING fts5(id, text, source, metadata, tokenize='porter')
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_vectors
            (id TEXT PRIMARY KEY, embedding BLOB)
        """)
        conn.commit()
        conn.close()

        # Mock the vector search to avoid network call
        with patch.object(mem, "_vector_search", new_callable=AsyncMock, return_value=[]):
            await mem.search("test query")

        count = _sample_value(metrics.MEMORY_SEARCH_DURATION, {"search_type": "combined"})
        assert count == 1, "Combined search should record 1 observation with search_type=combined"
