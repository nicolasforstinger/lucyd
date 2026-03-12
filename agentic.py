"""Provider-agnostic agentic tool-use loop.

The core of the agent. Takes a provider, messages, tools, and loops
until done (end_turn, max_tokens, or max_turns).

Text generated alongside tool calls (intermediate "thinking out loud")
is persisted to the session but not surfaced to callers. Only the
final turn's text becomes response.text. Deliberate outbound messages
go through the message tool.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from providers import LLMProvider, LLMResponse
from tools import ToolRegistry

log = logging.getLogger(__name__)


def cost_db_query(path: str, sql: str, params: tuple = (), *,
                  sqlite_timeout: int) -> list:
    """Run a read query against the cost DB, returning all rows.

    Handles connect/close lifecycle.  Returns [] on any error or
    if the DB file does not exist.
    """
    if not path or not Path(path).exists():
        return []
    conn = sqlite3.connect(path, timeout=sqlite_timeout)
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()
    except Exception:  # noqa: S110 — cost DB query; graceful degradation
        return []
    finally:
        conn.close()


def _init_cost_db(path: str, *, sqlite_timeout: int) -> None:
    """Create cost tracking table if it doesn't exist."""
    if not path:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=sqlite_timeout)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS costs (
                timestamp INTEGER,
                session_id TEXT,
                model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cache_read_tokens INTEGER,
                cache_write_tokens INTEGER,
                cost_usd REAL,
                call_type TEXT DEFAULT 'agentic',
                trace_id TEXT
            )
        """)
        # Migrate existing DBs: add columns if they don't exist
        cols = {r[1] for r in conn.execute("PRAGMA table_info(costs)").fetchall()}
        if "call_type" not in cols:
            conn.execute("ALTER TABLE costs ADD COLUMN call_type TEXT DEFAULT 'agentic'")
        if "trace_id" not in cols:
            conn.execute("ALTER TABLE costs ADD COLUMN trace_id TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_costs_timestamp ON costs(timestamp)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_costs_session ON costs(session_id)"
        )
        conn.commit()
    finally:
        conn.close()


def _record_cost(
    path: str,
    session_id: str,
    model: str,
    usage: Any,
    cost_rates: list[float],
    call_type: str = "agentic",
    trace_id: str = "",
    *,
    sqlite_timeout: int,
) -> float:
    """Record API cost and return USD amount."""
    if not path or not cost_rates:
        return 0.0

    # cost_rates: [input_per_mtok, output_per_mtok, cache_read_per_mtok]
    input_rate = cost_rates[0] if len(cost_rates) > 0 else 0.0
    output_rate = cost_rates[1] if len(cost_rates) > 1 else 0.0
    cache_rate = cost_rates[2] if len(cost_rates) > 2 else 0.0

    cost = (
        usage.input_tokens * input_rate / 1_000_000
        + usage.output_tokens * output_rate / 1_000_000
        + usage.cache_read_tokens * cache_rate / 1_000_000
    )

    conn = sqlite3.connect(path, timeout=sqlite_timeout)
    try:
        conn.execute(
            "INSERT INTO costs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(time.time()),
                session_id,
                model,
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_read_tokens,
                usage.cache_write_tokens,
                cost,
                call_type,
                trace_id,
            ),
        )
        conn.commit()
    except Exception as e:
        log.warning("Failed to record cost: %s", e)
    finally:
        conn.close()

    return cost


async def run_agentic_loop(
    provider: LLMProvider,
    system: Any,
    messages: list[dict],
    tools: list[dict],
    tool_executor: ToolRegistry,
    max_turns: int,
    timeout: float,
    api_retries: int,
    api_retry_base_delay: float,
    sqlite_timeout: int,
    cost_db: str | None = None,
    session_id: str = "",
    model_name: str = "",
    cost_rates: list[float] | None = None,
    max_cost: float = 0.0,
    on_response: Any = None,
    on_tool_results: Any = None,
    trace_id: str = "",
) -> LLMResponse:
    """Run the provider-agnostic agentic loop.

    Args:
        provider: LLM provider instance.
        system: Formatted system prompt (provider-specific format).
        messages: Conversation messages in internal format.
        tools: Tool schemas in generic format.
        tool_executor: ToolRegistry for executing tool calls.
        max_turns: Max tool-use loop iterations.
        timeout: Timeout per API call in seconds.
        cost_db: Path to cost tracking SQLite DB.
        session_id: Session ID for cost tracking.
        model_name: Model name for cost tracking.
        cost_rates: [input, output, cache_read] per million tokens.
        max_cost: Max USD cost per message (0.0 = disabled).
        on_response: Callback(response) after each LLM response.
        on_tool_results: Callback(results_msg) after each tool execution.

    Returns:
        Final LLMResponse from the loop. response.text is the final
        turn's text only. Intermediate text generated alongside tool
        calls is persisted to the session but not surfaced here —
        deliberate outbound messages go through the message tool.
    """
    max_turns = max(1, max_turns)
    fmt_tools = provider.format_tools(tools) if tools else []
    accumulated_cost = 0.0
    fallback_text: list[str] = []
    if not trace_id:
        trace_id = str(uuid.uuid4())

    response = None
    for turn in range(max_turns):
        fmt_messages = provider.format_messages(messages)

        last_exc: BaseException | None = None
        for attempt in range(1 + api_retries):
            try:
                response = await asyncio.wait_for(
                    provider.complete(system, fmt_messages, fmt_tools),
                    timeout=timeout,
                )
                break
            except TimeoutError:
                log.error("[%s] API call timed out after %.0fs (turn %d)",
                          trace_id[:8], timeout, turn)
                raise
            except Exception as exc:
                if not is_transient_error(exc) or attempt >= api_retries:
                    raise
                delay = api_retry_base_delay * (2 ** attempt) * (0.5 + random.random())  # noqa: S311 — jitter for backoff timing
                log.warning("[%s] Transient API error (attempt %d/%d): %s — retrying in %.1fs",
                            trace_id[:8], attempt + 1, api_retries + 1, exc, delay)
                last_exc = exc
                await asyncio.sleep(delay)
        else:
            raise last_exc  # type: ignore[misc]

        # Track cost
        if cost_db and cost_rates:
            turn_cost = _record_cost(
                cost_db, session_id, model_name, response.usage, cost_rates,
                trace_id=trace_id, sqlite_timeout=sqlite_timeout,
            )
            accumulated_cost += turn_cost

        # Max cost circuit breaker
        if max_cost > 0 and accumulated_cost > max_cost:
            log.warning("[%s] Cost limit reached: $%.4f > $%.2f (turn %d)",
                        trace_id[:8], accumulated_cost, max_cost, turn)
            response.cost_limited = True
            # Preserve any agent text; fall back to intermediate text
            if not response.text and fallback_text:
                response.text = "\n\n".join(fallback_text)
            return response

        # Add to messages
        internal_msg = response.to_internal_message()
        messages.append(internal_msg)

        if on_response:
            await on_response(response) if inspect.iscoroutinefunction(on_response) \
                else on_response(response)

        # Collect intermediate text as fallback in case the final turn is empty
        if response.text and response.tool_calls:
            fallback_text.append(response.text)

        if response.stop_reason == "max_tokens":
            log.warning("[%s] Response truncated (max_tokens) on turn %d",
                        trace_id[:8], turn)
            # Warn agent so it can wrap up
            if response.tool_calls:
                messages.append({
                    "role": "user",
                    "content": (
                        "[system: Your response was truncated (max output tokens). "
                        "Some tool calls may be missing. Wrap up quickly.]"
                    ),
                })

        # If there are complete tool calls, execute them — even on max_tokens.
        # A truncated response may contain valid tool_use blocks generated
        # before the cutoff; discarding them corrupts the session (dangling
        # tool_use with no tool_result) and wastes the model's work.
        if not response.tool_calls or response.stop_reason == "end_turn":
            if not response.text and fallback_text:
                response.text = "\n\n".join(fallback_text)
            return response

        # Execute tool calls in parallel
        async def _execute_tool(tc):
            log.info("[%s] Tool call: %s(%s)",
                     trace_id[:8], tc.name, _truncate_args(tc.arguments))
            result = await tool_executor.execute(tc.name, tc.arguments)
            return {"tool_call_id": tc.id, "content": result}

        tasks = [_execute_tool(tc) for tc in response.tool_calls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Handle any exceptions from parallel execution
        final_results = []
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                tc = response.tool_calls[i]
                log.error("[%s] Tool %s raised exception: %s",
                          trace_id[:8], tc.name, result)
                final_results.append({
                    "tool_call_id": tc.id,
                    "content": f"Error: {type(result).__name__}: {result}",
                })
            else:
                final_results.append(result)

        results_msg = {"role": "tool_results", "results": final_results}
        messages.append(results_msg)

        if on_tool_results:
            await on_tool_results(results_msg) if inspect.iscoroutinefunction(on_tool_results) \
                else on_tool_results(results_msg)

        # Warn agent when approaching turn limit (2 turns remaining)
        remaining = max_turns - (turn + 1)
        if remaining == 2:
            warning_text = (
                f"[system: You have 2 tool-use turns remaining out of {max_turns}. "
                f"Wrap up your work and provide a final answer.]"
            )
            messages.append({"role": "user", "content": warning_text})

    log.warning("[%s] Max turns (%d) reached", trace_id[:8], max_turns)
    if response is not None:
        stop_msg = f"\n[Stopped: maximum tool-use turns ({max_turns}) reached]"
        if response.text:
            response.text += stop_msg
        elif fallback_text:
            response.text = "\n\n".join(fallback_text) + stop_msg
        else:
            response.text = stop_msg
    return response  # type: ignore[return-value]


def is_transient_error(exc: BaseException) -> bool:
    """Check if an exception is transient and worth retrying.

    Uses class name matching to work with both Anthropic and OpenAI SDKs
    without importing them. Never retries auth (401), bad request (400),
    or permission (403) errors.
    """
    cls_name = type(exc).__name__

    # Non-retryable: auth, permission, bad request
    non_retryable = {
        "AuthenticationError", "PermissionDeniedError",
        "BadRequestError", "NotFoundError",
        "UnprocessableEntityError",
    }
    if cls_name in non_retryable:
        return False

    # Retryable: rate limits, server errors, connection problems
    retryable = {
        "RateLimitError", "APIStatusError",
        "InternalServerError", "APIConnectionError",
        "APITimeoutError", "OverloadedError",
    }
    if cls_name in retryable:
        # APIStatusError: only retry 429 and 5xx
        status = getattr(exc, "status_code", None)
        return status is None or status >= 429

    # Connection-level errors
    return isinstance(exc, (ConnectionError, OSError))


def _truncate_args(args: dict, max_len: int = 200) -> str:
    """Truncate tool arguments for logging."""
    s = str(args)
    return s[:max_len] + "..." if len(s) > max_len else s
