"""Plugin runtime: typed errors + execution helper.

Plugins raise typed :class:`PluginError` subclasses on failure so the
framework (tool registry in ``tools/__init__.py``, preprocessor dispatch
in ``pipeline.py``) can emit Prometheus metrics, apply retry policy, and
expose the minimal signal the agent needs — without leaking SDK
internals, HTTP status codes, or exception class names into the agent's
context.

Plugins MUST NOT catch their own exceptions and return error-shaped
strings or dicts. Raise a :class:`PluginError` subclass. See
``docs/plugins.md`` for the full contract.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

import metrics

log = logging.getLogger(__name__)


# ─── Typed errors ──────────────────────────────────────────────────


class PluginError(Exception):
    """Base class for plugin-raised errors.

    Subclasses declare a stable ``code`` (used as a Prometheus label),
    a ``retryable`` flag (whether the framework should retry), and a
    ``user_safe`` flag (whether the message can be shown to the agent
    verbatim — otherwise the framework replaces it with an opaque
    "unavailable" string).
    """

    code: str = "plugin_error"
    retryable: bool = False
    user_safe: bool = False

    def __init__(self, message: str = "") -> None:
        super().__init__(message or type(self).__name__)


class PluginNotConfigured(PluginError):
    """Plugin is not set up (missing API key, no TOML, disabled).

    Operator must fix; the agent should see "unavailable".
    """

    code = "not_configured"


class PluginInvalidInput(PluginError):
    """Caller supplied invalid arguments to a plugin operation.

    The message IS propagated to the agent so it can correct its call
    (e.g., "voice_id 'foo' not found, no default configured").
    """

    code = "invalid_input"
    user_safe = True


class PluginTransient(PluginError):
    """Transient network/connection failure. Framework retries."""

    code = "transient"
    retryable = True


class PluginUpstream(PluginError):
    """Upstream API returned 5xx or malformed response. Framework retries."""

    code = "upstream"
    retryable = True


class PluginAuth(PluginError):
    """Authentication failed (401, invalid token). Not retryable."""

    code = "auth_failed"


class PluginQuota(PluginError):
    """Quota or rate limit exhausted (429, credits empty). Not retryable."""

    code = "quota_exceeded"


class PluginEmptyOutput(PluginError):
    """Plugin returned empty result where one was expected (e.g., empty STT)."""

    code = "empty_output"


# ─── Execution helper ─────────────────────────────────────────────


_UNAVAILABLE = "unavailable"

T = TypeVar("T")


async def run_plugin_op(
    plugin: str,
    operation: str,
    fn: Callable[..., Awaitable[T]],
    *args: object,
    retry_max: int = 2,
    retry_backoff: float = 1.0,
    **kwargs: object,
) -> T:
    """Execute a plugin operation with retry policy + Prometheus emission.

    ``fn`` must be an async callable that raises :class:`PluginError`
    subclasses on failure. Retryable errors are retried up to
    ``retry_max`` times with ``retry_backoff`` seconds between attempts.
    The final exception is re-raised after exhaustion — the caller
    decides what the agent sees.

    Emits on every call:
        - ``lucyd_plugin_calls_total{plugin, operation, status, code}``
        - ``lucyd_plugin_duration_seconds{plugin, operation}``

    Emits per retry:
        - ``lucyd_plugin_retries_total{plugin, operation, code}``
    """
    start = time.monotonic()
    attempt = 0
    while True:
        try:
            result = await fn(*args, **kwargs)
        except PluginError as e:
            if e.retryable and attempt < retry_max:
                attempt += 1
                if metrics.ENABLED:
                    metrics.PLUGIN_RETRIES_TOTAL.labels(
                        plugin=plugin, operation=operation, code=e.code,
                    ).inc()
                log.warning(
                    "Plugin %s.%s retry %d/%d after %s: %s",
                    plugin, operation, attempt, retry_max, e.code, e,
                )
                await asyncio.sleep(retry_backoff)
                continue
            if metrics.ENABLED:
                metrics.PLUGIN_CALLS_TOTAL.labels(
                    plugin=plugin, operation=operation,
                    status="error", code=e.code,
                ).inc()
                metrics.PLUGIN_DURATION.labels(
                    plugin=plugin, operation=operation,
                ).observe(time.monotonic() - start)
            raise
        else:
            if metrics.ENABLED:
                metrics.PLUGIN_CALLS_TOTAL.labels(
                    plugin=plugin, operation=operation,
                    status="success", code="",
                ).inc()
                metrics.PLUGIN_DURATION.labels(
                    plugin=plugin, operation=operation,
                ).observe(time.monotonic() - start)
            return result


def agent_safe_message(error: PluginError) -> str:
    """Translate a PluginError into text the agent is allowed to see.

    ``user_safe`` errors (invalid input) pass through verbatim so the
    agent can correct its call. All other errors collapse to a neutral
    "unavailable" token — the agent learns the operation didn't work
    but is not given internal detail.
    """
    if error.user_safe:
        return str(error)
    return _UNAVAILABLE


_plugin_state: dict[str, dict[str, str | bool]] = {}


def mark_configured(plugin: str, backend: str = "") -> None:
    """Signal that the plugin finished ``configure()`` successfully.

    Records the plugin in the in-process state registry and sets the
    :data:`metrics.PLUGIN_CONFIGURED` gauge to 1. Plugins call this at
    the end of their ``configure()`` once all credentials and SDK
    clients are wired. Dashboards and the ``/api/v1/plugins`` endpoint
    use this to show the set of active plugins per deployment.
    """
    _plugin_state[plugin] = {"configured": True, "backend": backend}
    if metrics.ENABLED:
        metrics.PLUGIN_CONFIGURED.labels(plugin=plugin, backend=backend).set(1)


def mark_unconfigured(plugin: str, backend: str = "") -> None:
    """Signal that the plugin is inactive (no config, missing creds).

    Records inactive state in the registry and sets the
    :data:`metrics.PLUGIN_CONFIGURED` gauge to 0. Called from the
    ``configure()`` early-return paths so dashboards and the health
    endpoint correctly distinguish inactive plugins from uninstalled
    ones (the latter don't appear in the registry at all).
    """
    _plugin_state[plugin] = {"configured": False, "backend": backend}
    if metrics.ENABLED:
        metrics.PLUGIN_CONFIGURED.labels(plugin=plugin, backend=backend).set(0)


def list_plugin_health() -> list[dict[str, str | bool]]:
    """Return a list of {name, configured, backend} for every known plugin.

    Order is alphabetical by plugin name. Only plugins that called
    ``mark_configured`` or ``mark_unconfigured`` appear — so unloaded
    plugins are absent rather than listed as ``configured: false``.
    """
    return [
        {"name": name, **state}
        for name, state in sorted(_plugin_state.items())
    ]


def plugin_health(name: str) -> dict[str, str | bool] | None:
    """Return health for a single plugin, or None if the plugin is unknown."""
    state = _plugin_state.get(name)
    if state is None:
        return None
    return {"name": name, **state}


def verify_plugin_declared_state(plugin: str) -> bool:
    """Return True if the plugin called :func:`mark_configured` or :func:`mark_unconfigured`.

    The loader in ``lucyd.py`` calls this after a plugin's ``configure()``
    returns. Plugins that never declare state are flagged with a warning
    so dashboards and the health endpoint show a complete picture.
    """
    return plugin in _plugin_state
