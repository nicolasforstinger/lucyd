"""Tests for the plugin runtime contract (plugins.py).

Covers:
- Typed error hierarchy and its classification attributes
- run_plugin_op: success, non-retryable, retryable exhaustion, retry success
- Prometheus emission (calls_total, duration_seconds, retries_total)
- agent_safe_message translation rules
- ToolRegistry integration with PluginError subclasses
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import metrics
import plugins as plugins_mod
from plugins import (
    PluginAuth,
    PluginEmptyOutput,
    PluginError,
    PluginInvalidInput,
    PluginNotConfigured,
    PluginQuota,
    PluginTransient,
    PluginUpstream,
    agent_safe_message,
    list_plugin_health,
    mark_configured,
    mark_unconfigured,
    plugin_health,
    run_plugin_op,
    verify_plugin_declared_state,
)
from tools import ToolRegistry, ToolSpec


# ─── Error hierarchy classification ──────────────────────────────


class TestPluginErrorClassification:
    """Each subclass declares the right code, retryable, and user_safe flags."""

    def test_base_defaults(self) -> None:
        err = PluginError("something")
        assert err.code == "plugin_error"
        assert err.retryable is False
        assert err.user_safe is False
        assert str(err) == "something"

    def test_empty_message_falls_back_to_class_name(self) -> None:
        err = PluginAuth()
        assert str(err) == "PluginAuth"

    def test_invalid_input_is_user_safe(self) -> None:
        err = PluginInvalidInput("voice_id 'foo' not found")
        assert err.code == "invalid_input"
        assert err.user_safe is True
        assert err.retryable is False

    def test_transient_is_retryable(self) -> None:
        assert PluginTransient().retryable is True
        assert PluginTransient().code == "transient"

    def test_upstream_is_retryable(self) -> None:
        assert PluginUpstream().retryable is True
        assert PluginUpstream().code == "upstream"

    def test_auth_quota_empty_are_not_retryable(self) -> None:
        assert PluginAuth().retryable is False
        assert PluginQuota().retryable is False
        assert PluginEmptyOutput().retryable is False

    def test_not_configured(self) -> None:
        assert PluginNotConfigured().code == "not_configured"
        assert PluginNotConfigured().retryable is False


# ─── agent_safe_message ──────────────────────────────────────────


class TestAgentSafeMessage:
    """user_safe passes through; everything else becomes 'unavailable'."""

    def test_user_safe_error_passes_message_through(self) -> None:
        err = PluginInvalidInput("voice_id 'foo' not found")
        assert agent_safe_message(err) == "voice_id 'foo' not found"

    def test_opaque_error_returns_unavailable(self) -> None:
        assert agent_safe_message(PluginAuth("token expired")) == "unavailable"
        assert agent_safe_message(PluginQuota("429")) == "unavailable"
        assert agent_safe_message(PluginTransient("timeout")) == "unavailable"
        assert agent_safe_message(PluginUpstream("500")) == "unavailable"
        assert agent_safe_message(PluginEmptyOutput()) == "unavailable"
        assert agent_safe_message(PluginNotConfigured()) == "unavailable"


# ─── run_plugin_op ───────────────────────────────────────────────


class TestRunPluginOp:
    """Retry policy, metric emission, exception propagation."""

    @pytest.mark.asyncio
    async def test_success_path_returns_result(self) -> None:
        async def fn(x: int) -> int:
            return x * 2

        result = await run_plugin_op("test", "op", fn, 21)
        assert result == 42

    @pytest.mark.asyncio
    async def test_non_retryable_error_raises_immediately(self) -> None:
        calls = 0

        async def fn() -> None:
            nonlocal calls
            calls += 1
            raise PluginAuth("bad token")

        with pytest.raises(PluginAuth):
            await run_plugin_op("test", "op", fn, retry_max=3, retry_backoff=0)
        assert calls == 1

    @pytest.mark.asyncio
    async def test_retryable_error_retries_up_to_retry_max(self) -> None:
        calls = 0

        async def fn() -> None:
            nonlocal calls
            calls += 1
            raise PluginTransient("timeout")

        with pytest.raises(PluginTransient):
            await run_plugin_op("test", "op", fn, retry_max=2, retry_backoff=0)
        assert calls == 3  # initial + 2 retries

    @pytest.mark.asyncio
    async def test_retryable_recovers_on_later_attempt(self) -> None:
        calls = 0

        async def fn() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise PluginTransient("still timing out")
            return "finally"

        result = await run_plugin_op(
            "test", "op", fn, retry_max=3, retry_backoff=0,
        )
        assert result == "finally"
        assert calls == 3

    @pytest.mark.asyncio
    async def test_user_safe_invalid_input_is_not_retried(self) -> None:
        calls = 0

        async def fn() -> None:
            nonlocal calls
            calls += 1
            raise PluginInvalidInput("bad voice")

        with pytest.raises(PluginInvalidInput):
            await run_plugin_op("test", "op", fn, retry_max=5, retry_backoff=0)
        assert calls == 1

    @pytest.mark.asyncio
    async def test_emits_success_metric(self) -> None:
        if not metrics.ENABLED:
            pytest.skip("prometheus_client not installed")

        async def fn() -> str:
            return "ok"

        before = metrics.PLUGIN_CALLS_TOTAL.labels(
            plugin="metric_test_s", operation="op", status="success", code="",
        )._value.get()
        await run_plugin_op("metric_test_s", "op", fn, retry_backoff=0)
        after = metrics.PLUGIN_CALLS_TOTAL.labels(
            plugin="metric_test_s", operation="op", status="success", code="",
        )._value.get()
        assert after == before + 1

    @pytest.mark.asyncio
    async def test_emits_error_metric_with_code(self) -> None:
        if not metrics.ENABLED:
            pytest.skip("prometheus_client not installed")

        async def fn() -> None:
            raise PluginAuth("bad token")

        before = metrics.PLUGIN_CALLS_TOTAL.labels(
            plugin="metric_test_e", operation="op", status="error", code="auth_failed",
        )._value.get()
        with pytest.raises(PluginAuth):
            await run_plugin_op(
                "metric_test_e", "op", fn, retry_max=0, retry_backoff=0,
            )
        after = metrics.PLUGIN_CALLS_TOTAL.labels(
            plugin="metric_test_e", operation="op", status="error", code="auth_failed",
        )._value.get()
        assert after == before + 1

    @pytest.mark.asyncio
    async def test_emits_retry_metric_per_retry(self) -> None:
        if not metrics.ENABLED:
            pytest.skip("prometheus_client not installed")

        async def fn() -> None:
            raise PluginTransient("timeout")

        before = metrics.PLUGIN_RETRIES_TOTAL.labels(
            plugin="metric_test_r", operation="op", code="transient",
        )._value.get()
        with pytest.raises(PluginTransient):
            await run_plugin_op(
                "metric_test_r", "op", fn, retry_max=3, retry_backoff=0,
            )
        after = metrics.PLUGIN_RETRIES_TOTAL.labels(
            plugin="metric_test_r", operation="op", code="transient",
        )._value.get()
        assert after == before + 3  # 3 retries


# ─── Configure gauge ─────────────────────────────────────────────


class TestConfiguredGauge:
    """mark_configured / mark_unconfigured set the gauge correctly."""

    def test_mark_configured_sets_one(self) -> None:
        if not metrics.ENABLED:
            pytest.skip("prometheus_client not installed")
        mark_configured("test_plugin_c", backend="testing")
        val = metrics.PLUGIN_CONFIGURED.labels(
            plugin="test_plugin_c", backend="testing",
        )._value.get()
        assert val == 1

    def test_mark_unconfigured_sets_zero(self) -> None:
        if not metrics.ENABLED:
            pytest.skip("prometheus_client not installed")
        mark_configured("test_plugin_u", backend="testing")
        mark_unconfigured("test_plugin_u", backend="testing")
        val = metrics.PLUGIN_CONFIGURED.labels(
            plugin="test_plugin_u", backend="testing",
        )._value.get()
        assert val == 0


# ─── Plugin state registry / health endpoints ───────────────────


class TestPluginHealth:
    """list_plugin_health / plugin_health reflect mark_configured state."""

    def setup_method(self) -> None:
        plugins_mod._plugin_state.clear()

    def test_unknown_plugin_returns_none(self) -> None:
        assert plugin_health("nonexistent") is None

    def test_mark_configured_reflected_in_health(self) -> None:
        mark_configured("test_hc", backend="openai")
        health = plugin_health("test_hc")
        assert health == {"name": "test_hc", "configured": True, "backend": "openai"}

    def test_mark_unconfigured_reflected_in_health(self) -> None:
        mark_unconfigured("test_hu", backend="")
        health = plugin_health("test_hu")
        assert health == {"name": "test_hu", "configured": False, "backend": ""}

    def test_unconfigured_after_configured_overwrites(self) -> None:
        mark_configured("test_flip", backend="x")
        mark_unconfigured("test_flip", backend="x")
        assert plugin_health("test_flip") == {
            "name": "test_flip", "configured": False, "backend": "x",
        }

    def test_list_is_alphabetical(self) -> None:
        mark_configured("zeta")
        mark_configured("alpha")
        mark_configured("mu")
        names = [p["name"] for p in list_plugin_health()]
        assert names == ["alpha", "mu", "zeta"]


class TestVerifyDeclaredState:
    """Loader contract check: plugins must call mark_configured / mark_unconfigured."""

    def setup_method(self) -> None:
        plugins_mod._plugin_state.clear()

    def test_unknown_plugin_is_not_declared(self) -> None:
        assert verify_plugin_declared_state("never_loaded") is False

    def test_mark_configured_counts_as_declared(self) -> None:
        mark_configured("declared_active", backend="x")
        assert verify_plugin_declared_state("declared_active") is True

    def test_mark_unconfigured_counts_as_declared(self) -> None:
        mark_unconfigured("declared_inactive")
        assert verify_plugin_declared_state("declared_inactive") is True


# ─── Tool registry integration ───────────────────────────────────


class TestToolRegistryPluginErrors:
    """ToolRegistry.execute translates PluginError subclasses for the agent."""

    @pytest.mark.asyncio
    async def test_invalid_input_message_is_visible_to_agent(self) -> None:
        reg = ToolRegistry()

        async def bad_arg_tool() -> str:
            raise PluginInvalidInput("voice_id 'foo' not found")

        reg.register(ToolSpec(
            name="t", description="", input_schema={"type": "object", "properties": {}},
            function=bad_arg_tool,
        ))
        result = await reg.execute("t", {})
        assert "voice_id 'foo' not found" in result["text"]

    @pytest.mark.asyncio
    async def test_auth_error_returns_unavailable(self) -> None:
        reg = ToolRegistry()

        async def auth_fail() -> str:
            raise PluginAuth("invalid api key xyz123")

        reg.register(ToolSpec(
            name="t2", description="", input_schema={"type": "object", "properties": {}},
            function=auth_fail,
        ))
        result = await reg.execute("t2", {})
        assert "unavailable" in result["text"]
        assert "invalid api key" not in result["text"]  # secret must not leak
        assert "xyz123" not in result["text"]

    @pytest.mark.asyncio
    async def test_quota_error_returns_unavailable(self) -> None:
        reg = ToolRegistry()

        async def quota_fail() -> str:
            raise PluginQuota("429 limit exceeded")

        reg.register(ToolSpec(
            name="t3", description="", input_schema={"type": "object", "properties": {}},
            function=quota_fail,
        ))
        result = await reg.execute("t3", {})
        assert "unavailable" in result["text"]
        assert "429" not in result["text"]

    @pytest.mark.asyncio
    async def test_tool_call_total_increments_error_on_plugin_error(self) -> None:
        if not metrics.ENABLED:
            pytest.skip("prometheus_client not installed")
        reg = ToolRegistry()

        async def fail() -> str:
            raise PluginUpstream("500")

        reg.register(ToolSpec(
            name="t4", description="", input_schema={"type": "object", "properties": {}},
            function=fail,
        ))
        before = metrics.TOOL_CALLS_TOTAL.labels(tool_name="t4", status="error")._value.get()
        await reg.execute("t4", {})
        after = metrics.TOOL_CALLS_TOTAL.labels(tool_name="t4", status="error")._value.get()
        assert after == before + 1

    @pytest.mark.asyncio
    async def test_secret_does_not_leak_via_exception_str(self) -> None:
        """PluginError subclasses used for secrets must collapse to 'unavailable'."""
        reg = ToolRegistry()

        async def leaky() -> str:
            raise PluginAuth("Bearer sk-live-9f8e7d6c5b4a3210")

        reg.register(ToolSpec(
            name="t5", description="", input_schema={"type": "object", "properties": {}},
            function=leaky,
        ))
        with patch("tools.log") as mock_log:
            result = await reg.execute("t5", {})
        assert "sk-live" not in result["text"]
        # The log records the detail; the agent does not see it.
        assert mock_log.warning.called
