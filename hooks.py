"""Lifecycle hooks for the agentic loop + message pipeline.

Concentrates the "fire an observability side effect at this well-known
point" wiring in one place. Instead of scattering ``metrics.X.inc()``
and ``log.info(...)`` calls through the hot path, the agentic loop and
pipeline call hook methods; a concrete implementation
(:class:`MetricsHooks`) owns the emission details.

Reasons to prefer this over direct ``metrics.*`` calls:

- One audit target when we want to confirm "every LLM call emits cost
  and latency." Today those calls are in ``agentic.py``, ``memory.py``,
  ``consolidation.py``, ``metering.py``, and plugins — and missing one
  is a silent gap.
- Dev/test overrides: tests swap in a recording hook to assert on
  emitted events without mocking the metric library.
- Multiple sinks: if we ever want to also forward to OpenTelemetry or
  Loki, that's a second implementation of the same Protocol, not a
  second set of edit sites.

The Protocol is deliberately narrow — only the events every agent call
passes through. Per-tool and per-plugin metrics keep their own
call sites; those are plugin-local and not part of the loop contract.

Integration will happen incrementally — emission sites migrate from
``metrics.X.inc()`` to ``hooks.on_llm_end(...)`` one call site at a
time, with a ``MetricsHooks.forward`` no-op during the transition.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import metrics

if TYPE_CHECKING:
    from providers import Usage

log = logging.getLogger(__name__)


class AgentHooks(Protocol):
    """Lifecycle hook surface for the agentic loop + pipeline.

    All hooks are async so concrete implementations may do I/O without
    blocking the event loop. Implementations must NOT raise — a hook
    failure has to degrade to a log line, never halt the agent.
    """

    async def on_llm_start(
        self, model: str, provider: str, session_id: str,
    ) -> None: ...

    async def on_llm_end(
        self,
        model: str,
        provider: str,
        session_id: str,
        usage: Usage,
        latency_ms: int,
        success: bool,
    ) -> None: ...

    async def on_tool_start(
        self, tool_name: str, session_id: str,
    ) -> None: ...

    async def on_tool_end(
        self,
        tool_name: str,
        session_id: str,
        duration_seconds: float,
        status: str,   # "success" | "error" | "timeout"
    ) -> None: ...

    async def on_agent_end(
        self,
        session_id: str,
        outcome: str,
        turns: int,
        cost_eur: float,
    ) -> None: ...


# ─── MetricsHooks: forward to Prometheus ───────────────────────────


@dataclass
class MetricsHooks:
    """Concrete :class:`AgentHooks` that forwards to Prometheus via ``metrics``.

    Thin wrapper — the per-event metrics already exist and fire from
    legacy call sites today. Adopting the hook just means those legacy
    call sites get replaced with a single ``await hooks.on_xxx(...)``
    that lands here. Until every site is migrated both paths emit
    harmlessly (Counter.inc is idempotent in aggregation).
    """

    _llm_start_ts: dict[str, float] = field(default_factory=dict)

    async def on_llm_start(self, model: str, provider: str, session_id: str) -> None:
        self._llm_start_ts[session_id] = time.monotonic()

    async def on_llm_end(
        self,
        model: str,
        provider: str,
        session_id: str,
        usage: Usage,
        latency_ms: int,
        success: bool,
    ) -> None:
        try:
            metrics.record_api_call(model, provider, usage, latency_ms=latency_ms)
            if not success and metrics.ENABLED:
                metrics.API_CALLS_TOTAL.labels(
                    model=model, provider=provider, status="error",
                ).inc()
        except Exception:  # noqa: BLE001 — hook must not raise
            log.exception("MetricsHooks.on_llm_end failed")

    async def on_tool_start(self, tool_name: str, session_id: str) -> None:
        pass  # tool duration measured by on_tool_end's `duration_seconds`

    async def on_tool_end(
        self,
        tool_name: str,
        session_id: str,
        duration_seconds: float,
        status: str,
    ) -> None:
        if not metrics.ENABLED:
            return
        try:
            metrics.TOOL_CALLS_TOTAL.labels(tool_name=tool_name, status=status).inc()
            metrics.TOOL_DURATION.labels(tool_name=tool_name).observe(duration_seconds)
        except Exception:  # noqa: BLE001 — hook must not raise
            log.exception("MetricsHooks.on_tool_end failed")

    async def on_agent_end(
        self,
        session_id: str,
        outcome: str,
        turns: int,
        cost_eur: float,
    ) -> None:
        if not metrics.ENABLED:
            return
        try:
            metrics.MESSAGE_OUTCOME_TOTAL.labels(outcome=outcome).inc()
            metrics.AGENTIC_TURNS.observe(turns)
        except Exception:  # noqa: BLE001 — hook must not raise
            log.exception("MetricsHooks.on_agent_end failed")


# ─── NullHooks + CompositeHooks ───────────────────────────────────


@dataclass
class NullHooks:
    """No-op :class:`AgentHooks` implementation. Safe default for tests."""

    async def on_llm_start(self, model: str, provider: str, session_id: str) -> None:
        pass

    async def on_llm_end(
        self,
        model: str,
        provider: str,
        session_id: str,
        usage: Usage,
        latency_ms: int,
        success: bool,
    ) -> None:
        pass

    async def on_tool_start(self, tool_name: str, session_id: str) -> None:
        pass

    async def on_tool_end(
        self,
        tool_name: str,
        session_id: str,
        duration_seconds: float,
        status: str,
    ) -> None:
        pass

    async def on_agent_end(
        self,
        session_id: str,
        outcome: str,
        turns: int,
        cost_eur: float,
    ) -> None:
        pass


@dataclass
class CompositeHooks:
    """Fan-out :class:`AgentHooks` that calls each wrapped hook in order.

    Use when the daemon wants both a metrics sink and a debug-log sink,
    or to stack a recording hook on top of the real sink in tests.
    A failure in any wrapped hook is swallowed (logged at ERROR) so a
    buggy sink never halts the run.
    """

    hooks: list[AgentHooks]

    async def _fanout(self, fn_name: str, *args: Any, **kwargs: Any) -> None:
        for h in self.hooks:
            fn: Callable[..., Awaitable[None]] = getattr(h, fn_name)
            try:
                await fn(*args, **kwargs)
            except Exception:  # noqa: BLE001 — composite isolation
                log.exception("CompositeHooks: %s.%s failed", type(h).__name__, fn_name)

    async def on_llm_start(self, model: str, provider: str, session_id: str) -> None:
        await self._fanout("on_llm_start", model, provider, session_id)

    async def on_llm_end(
        self,
        model: str,
        provider: str,
        session_id: str,
        usage: Usage,
        latency_ms: int,
        success: bool,
    ) -> None:
        await self._fanout(
            "on_llm_end", model, provider, session_id, usage, latency_ms, success,
        )

    async def on_tool_start(self, tool_name: str, session_id: str) -> None:
        await self._fanout("on_tool_start", tool_name, session_id)

    async def on_tool_end(
        self,
        tool_name: str,
        session_id: str,
        duration_seconds: float,
        status: str,
    ) -> None:
        await self._fanout(
            "on_tool_end", tool_name, session_id, duration_seconds, status,
        )

    async def on_agent_end(
        self,
        session_id: str,
        outcome: str,
        turns: int,
        cost_eur: float,
    ) -> None:
        await self._fanout(
            "on_agent_end", session_id, outcome, turns, cost_eur,
        )
