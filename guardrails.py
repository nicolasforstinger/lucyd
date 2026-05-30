"""Tripwire predicates for agent input + output.

A guardrail is an async predicate that inspects either the incoming
user message or the outgoing agent reply and returns ``(tripped,
reason)``. If a predicate returns ``tripped=True`` the framework
raises :class:`GuardrailTripped`; :mod:`pipeline` catches it, aborts
the agentic loop, emits a metric, and returns a safe message.

Design goals:
  * No new dependency; predicates are plain ``async`` callables.
  * Input + output sides are independent — register on one, both, or
    neither. Empty registry = no-op.
  * Safe default: an exception inside a predicate counts as NOT
    tripped (log + continue). We never want a bug in a guardrail to
    freeze the agent.
  * Concrete types throughout. No ``Any`` leakage.

Minimal plugin surface — ergonomic wrapper around a Python function:

    from guardrails import Guardrails, GuardrailTripped

    g = Guardrails()

    @g.input("no_api_keys")
    async def _block_api_key_leakage(text: str) -> tuple[bool, str]:
        if "sk-ant-api03-" in text or "sk-proj-" in text:
            return True, "message appears to contain an API key"
        return False, ""

See ``docs/guardrails.md`` for the integration contract when we wire
this into :class:`pipeline.MessagePipeline`.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

log = logging.getLogger(__name__)


Predicate = Callable[[str], Awaitable[tuple[bool, str]]]
Scope = Literal["input", "output"]


class GuardrailTripped(Exception):
    """A guardrail predicate flagged a message. Halts the current run.

    Surfaced to the operator via metrics + logs; the agent sees a
    safe neutral message ("request blocked") rather than the raw
    reason (which may contain the offending content).
    """

    code: str = "guardrail_tripped"

    def __init__(self, name: str, scope: Scope, reason: str) -> None:
        super().__init__(f"{scope} guardrail '{name}' tripped: {reason}")
        self.name = name
        self.scope = scope
        self.reason = reason


@dataclass(frozen=True)
class _Rule:
    name: str
    predicate: Predicate
    scope: Scope


@dataclass
class Guardrails:
    """Registry of input + output tripwire predicates."""

    _rules: list[_Rule] = field(default_factory=list)

    def input(self, name: str) -> Callable[[Predicate], Predicate]:
        """Decorator: register a predicate as an input guardrail."""
        return self._register("input", name)

    def output(self, name: str) -> Callable[[Predicate], Predicate]:
        """Decorator: register a predicate as an output guardrail."""
        return self._register("output", name)

    def _register(self, scope: Scope, name: str) -> Callable[[Predicate], Predicate]:
        def wrap(fn: Predicate) -> Predicate:
            self._rules.append(_Rule(name=name, predicate=fn, scope=scope))
            return fn
        return wrap

    async def check_input(self, text: str) -> None:
        """Run every input guardrail. Raises :class:`GuardrailTripped` on trip."""
        await self._run("input", text)

    async def check_output(self, text: str) -> None:
        """Run every output guardrail. Raises :class:`GuardrailTripped` on trip."""
        await self._run("output", text)

    async def _run(self, scope: Scope, text: str) -> None:
        for rule in self._rules:
            if rule.scope != scope:
                continue
            try:
                tripped, reason = await rule.predicate(text)
            except Exception:  # noqa: BLE001 — predicate bug must not freeze the agent
                log.exception("guardrail %s.%s raised — treating as not-tripped", scope, rule.name)
                continue
            if tripped:
                raise GuardrailTripped(rule.name, scope, reason)

    def names(self, scope: Scope | None = None) -> list[str]:
        """Return registered rule names, optionally filtered by scope."""
        return [r.name for r in self._rules if scope is None or r.scope == scope]
