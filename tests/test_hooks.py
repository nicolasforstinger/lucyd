"""Tests for the lifecycle-hooks module."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from hooks import AgentHooks, CompositeHooks, MetricsHooks, NullHooks
from providers import Usage


# ── Recording hook for test assertions ────────────────────────────


@dataclass
class _RecordingHooks:
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def on_llm_start(self, model: str, provider: str, session_id: str) -> None:
        self.events.append(("llm_start", {"model": model, "provider": provider, "session_id": session_id}))

    async def on_llm_end(
        self, model: str, provider: str, session_id: str,
        usage: Usage, latency_ms: int, success: bool,
    ) -> None:
        self.events.append(("llm_end", {
            "model": model, "provider": provider, "session_id": session_id,
            "latency_ms": latency_ms, "success": success,
        }))

    async def on_tool_start(self, tool_name: str, session_id: str) -> None:
        self.events.append(("tool_start", {"tool_name": tool_name, "session_id": session_id}))

    async def on_tool_end(
        self, tool_name: str, session_id: str,
        duration_seconds: float, status: str,
    ) -> None:
        self.events.append(("tool_end", {
            "tool_name": tool_name, "session_id": session_id,
            "duration_seconds": duration_seconds, "status": status,
        }))

    async def on_agent_end(
        self, session_id: str, outcome: str, turns: int, cost_eur: float,
    ) -> None:
        self.events.append(("agent_end", {
            "session_id": session_id, "outcome": outcome, "turns": turns, "cost_eur": cost_eur,
        }))


class TestProtocolConformance:
    def test_nullhooks_is_agenthooks(self) -> None:
        h: AgentHooks = NullHooks()
        assert h is not None

    def test_metricshooks_is_agenthooks(self) -> None:
        h: AgentHooks = MetricsHooks()
        assert h is not None

    def test_recording_is_agenthooks(self) -> None:
        h: AgentHooks = _RecordingHooks()
        assert h is not None


class TestNullHooks:
    @pytest.mark.asyncio
    async def test_all_methods_are_noops(self) -> None:
        h = NullHooks()
        usage = Usage(input_tokens=10, output_tokens=5)
        # Just verify none raise
        await h.on_llm_start("m", "p", "s")
        await h.on_llm_end("m", "p", "s", usage, 100, True)
        await h.on_tool_start("t", "s")
        await h.on_tool_end("t", "s", 0.5, "success")
        await h.on_agent_end("s", "ok", 3, 0.01)


class TestCompositeHooks:
    @pytest.mark.asyncio
    async def test_fans_out_to_all_hooks(self) -> None:
        a = _RecordingHooks()
        b = _RecordingHooks()
        composite = CompositeHooks(hooks=[a, b])
        await composite.on_tool_end("test_tool", "sess", 0.5, "success")
        assert len(a.events) == 1
        assert len(b.events) == 1
        assert a.events[0][0] == "tool_end"
        assert b.events[0][0] == "tool_end"

    @pytest.mark.asyncio
    async def test_hook_failure_doesnt_halt_others(self) -> None:
        class Broken:
            async def on_llm_start(self, *a: Any, **kw: Any) -> None:
                raise RuntimeError("broken")
            async def on_llm_end(self, *a: Any, **kw: Any) -> None: ...
            async def on_tool_start(self, *a: Any, **kw: Any) -> None: ...
            async def on_tool_end(self, *a: Any, **kw: Any) -> None: ...
            async def on_agent_end(self, *a: Any, **kw: Any) -> None: ...

        rec = _RecordingHooks()
        composite = CompositeHooks(hooks=[Broken(), rec])
        await composite.on_llm_start("m", "p", "s")  # must not raise
        # The good hook still fired
        assert len(rec.events) == 1

    @pytest.mark.asyncio
    async def test_empty_composite_is_noop(self) -> None:
        composite = CompositeHooks(hooks=[])
        await composite.on_agent_end("s", "ok", 1, 0.0)


class TestMetricsHooks:
    @pytest.mark.asyncio
    async def test_methods_dont_raise(self) -> None:
        """MetricsHooks must never raise — even if metrics subsystem has
        a bug, the agent run continues."""
        h = MetricsHooks()
        usage = Usage(input_tokens=10, output_tokens=5)
        await h.on_llm_start("m", "p", "s")
        await h.on_llm_end("m", "p", "s", usage, 100, True)
        await h.on_llm_end("m", "p", "s", usage, 100, False)  # failure path
        await h.on_tool_start("t", "s")
        await h.on_tool_end("t", "s", 0.5, "success")
        await h.on_tool_end("t", "s", 0.5, "error")
        await h.on_agent_end("s", "ok", 3, 0.01)
