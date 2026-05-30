"""Tests for the guardrails tripwire module."""

from __future__ import annotations

import logging

import pytest

from guardrails import GuardrailTripped, Guardrails


# ── Registration ─────────────────────────────────────────────────


class TestRegistration:
    def test_input_rule_registered(self) -> None:
        g = Guardrails()

        @g.input("no_secrets")
        async def _check(text: str) -> tuple[bool, str]:
            return False, ""

        assert g.names() == ["no_secrets"]
        assert g.names("input") == ["no_secrets"]
        assert g.names("output") == []

    def test_output_rule_registered(self) -> None:
        g = Guardrails()

        @g.output("no_pii")
        async def _check(text: str) -> tuple[bool, str]:
            return False, ""

        assert g.names("output") == ["no_pii"]
        assert g.names("input") == []

    def test_both_scopes(self) -> None:
        g = Guardrails()

        @g.input("in_rule")
        async def _in(text: str) -> tuple[bool, str]:
            return False, ""

        @g.output("out_rule")
        async def _out(text: str) -> tuple[bool, str]:
            return False, ""

        assert set(g.names()) == {"in_rule", "out_rule"}


# ── Behaviour ────────────────────────────────────────────────────


class TestBehaviour:
    @pytest.mark.asyncio
    async def test_empty_registry_is_noop(self) -> None:
        g = Guardrails()
        await g.check_input("anything")
        await g.check_output("anything")

    @pytest.mark.asyncio
    async def test_untripped_allows_through(self) -> None:
        g = Guardrails()

        @g.input("always_ok")
        async def _ok(text: str) -> tuple[bool, str]:
            return False, ""

        await g.check_input("hello")

    @pytest.mark.asyncio
    async def test_tripped_raises(self) -> None:
        g = Guardrails()

        @g.input("blocks_foo")
        async def _block(text: str) -> tuple[bool, str]:
            if "foo" in text:
                return True, "contains foo"
            return False, ""

        with pytest.raises(GuardrailTripped) as exc_info:
            await g.check_input("hello foo world")
        assert exc_info.value.name == "blocks_foo"
        assert exc_info.value.scope == "input"
        assert "contains foo" in exc_info.value.reason

    @pytest.mark.asyncio
    async def test_first_tripped_wins(self) -> None:
        g = Guardrails()

        @g.input("first")
        async def _first(text: str) -> tuple[bool, str]:
            return True, "first reason"

        @g.input("second")
        async def _second(text: str) -> tuple[bool, str]:
            return True, "second reason"

        with pytest.raises(GuardrailTripped) as exc_info:
            await g.check_input("x")
        assert exc_info.value.name == "first"

    @pytest.mark.asyncio
    async def test_input_doesnt_run_output(self) -> None:
        g = Guardrails()
        called = []

        @g.input("in")
        async def _in(text: str) -> tuple[bool, str]:
            called.append("input")
            return False, ""

        @g.output("out")
        async def _out(text: str) -> tuple[bool, str]:
            called.append("output")
            return False, ""

        await g.check_input("x")
        assert called == ["input"]

    @pytest.mark.asyncio
    async def test_predicate_exception_treated_as_untripped(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A buggy predicate must not freeze the agent — log + continue."""
        g = Guardrails()

        @g.input("buggy")
        async def _bug(text: str) -> tuple[bool, str]:
            raise RuntimeError("oops")

        with caplog.at_level(logging.ERROR, logger="guardrails"):
            await g.check_input("x")  # must NOT raise

        assert any("buggy" in rec.message for rec in caplog.records)


# ── Concrete predicate examples ──────────────────────────────────


class TestExamplePredicates:
    @pytest.mark.asyncio
    async def test_api_key_leakage_predicate(self) -> None:
        """Example input guardrail — block messages that look like API keys."""
        g = Guardrails()

        @g.input("no_api_keys")
        async def _block(text: str) -> tuple[bool, str]:
            for prefix in ("sk-ant-api03-", "sk-proj-", "sk-"):
                if prefix in text:
                    return True, "message appears to contain an API key"
            return False, ""

        await g.check_input("this is a safe message")
        with pytest.raises(GuardrailTripped):
            await g.check_input("my key is sk-ant-api03-abcdef")
