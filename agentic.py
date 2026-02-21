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
import sqlite3
import time
from pathlib import Path
from typing import Any

from providers import LLMProvider, LLMResponse
from tools import ToolRegistry

log = logging.getLogger(__name__)


def _init_cost_db(path: str) -> None:
    """Create cost tracking table if it doesn't exist."""
    if not path:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS costs (
                timestamp INTEGER,
                session_id TEXT,
                model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cache_read_tokens INTEGER,
                cache_write_tokens INTEGER,
                cost_usd REAL
            )
        """)
        conn.commit()
    finally:
        conn.close()


def _record_cost(
    path: str,
    session_id: str,
    model: str,
    usage: Any,
    cost_rates: list[float],
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

    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO costs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(time.time()),
                session_id,
                model,
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_read_tokens,
                usage.cache_write_tokens,
                cost,
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
    max_turns: int = 50,
    timeout: float = 600.0,
    cost_db: str | None = None,
    session_id: str = "",
    model_name: str = "",
    cost_rates: list[float] | None = None,
    max_cost: float = 0.0,
    on_response: Any = None,
    on_tool_results: Any = None,
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

    response = None
    for turn in range(max_turns):
        fmt_messages = provider.format_messages(messages)

        try:
            response = await asyncio.wait_for(
                provider.complete(system, fmt_messages, fmt_tools),
                timeout=timeout,
            )
        except TimeoutError:
            log.error("API call timed out after %.0fs (turn %d)", timeout, turn)
            raise

        # Track cost
        if cost_db and cost_rates:
            turn_cost = _record_cost(cost_db, session_id, model_name, response.usage, cost_rates)
            accumulated_cost += turn_cost

        # Max cost circuit breaker
        if max_cost > 0 and accumulated_cost > max_cost:
            log.warning("Cost limit reached: $%.4f > $%.2f (turn %d)",
                        accumulated_cost, max_cost, turn)
            if response.text:
                response.text += f"\n[Cost limit reached: ${accumulated_cost:.4f}]"
            else:
                response.text = f"[Cost limit reached: ${accumulated_cost:.4f}]"
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
            log.warning("Response truncated (max_tokens) on turn %d", turn)

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
            log.info("Tool call: %s(%s)", tc.name, _truncate_args(tc.arguments))
            result = await tool_executor.execute(tc.name, tc.arguments)
            return {"tool_call_id": tc.id, "content": result}

        tasks = [_execute_tool(tc) for tc in response.tool_calls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Handle any exceptions from parallel execution
        final_results = []
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                tc = response.tool_calls[i]
                log.error("Tool %s raised exception: %s", tc.name, result)
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

    log.warning("Max turns (%d) reached", max_turns)
    if response is not None and not response.text and fallback_text:
        response.text = "\n\n".join(fallback_text)
    return response  # type: ignore[return-value]


def _truncate_args(args: dict, max_len: int = 200) -> str:
    """Truncate tool arguments for logging."""
    s = str(args)
    return s[:max_len] + "..." if len(s) > max_len else s
