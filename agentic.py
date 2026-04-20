"""Provider-agnostic agentic tool-use loop.

The core of the agent. Takes a provider, messages, tools, and loops
until done (end_turn, max_tokens, or max_turns).

run_single_shot: one model call, no tools (for constrained models).
run_agentic_loop: multi-turn tool-use loop (think, act, observe, repeat).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import metrics
from messages import Message, ToolResultsMessage

from providers import CostContext, LLMProvider, LLMResponse, StreamDelta, SystemPrompt, ToolCall, Usage
from tools import ToolRegistry

log = logging.getLogger(__name__)


# ─── Loop Configuration ──────────────────────────────────────────

@dataclass(frozen=True)
class LoopConfig:
    """Configuration bundle for the agentic loop.

    Groups max_turns, timeout, retry, cost, and behavior tuning params
    that always travel together through the call chain.
    """
    max_turns: int = 25
    timeout: float = 120.0
    api_retries: int = 3
    api_retry_base_delay: float = 1.0
    max_cost: float = 0.0
    max_context_for_tools: int = 0
    tool_call_retry: bool = False
    tool_success_warn_threshold: float = 0.5
    thinking_concise_hint: bool = False
    trace_id: str = ""


async def _call_provider_with_retry(
    provider: LLMProvider,
    system: SystemPrompt,
    fmt_messages: list[dict[str, Any]],  # Any justified: provider-formatted message dicts
    fmt_tools: list[dict[str, Any]],  # Any justified: provider-formatted tool schemas
    *,
    cfg: LoopConfig,
    trace_id: str,
    on_stream_delta: Callable[[StreamDelta], Any] | None = None,  # Any justified: may be sync or async
) -> LLMResponse:
    """Call the provider with retry and streaming.

    Shared by both run_single_shot and run_agentic_loop. Handles:
    - Streaming vs non-streaming dispatch
    - Retry with exponential backoff + jitter for transient errors
    - Timeout enforcement
    """
    use_streaming = (
        on_stream_delta is not None
        and provider.capabilities.supports_streaming
    )

    last_exc: BaseException | None = None
    _model = getattr(provider, "model", "")
    _prov = getattr(provider, "provider_name", "")

    for attempt in range(1 + cfg.api_retries):
        _api_start = time.time()
        try:
            if use_streaming:
                response = await asyncio.wait_for(
                    _stream_to_response(provider, system, fmt_messages, fmt_tools, on_stream_delta),
                    timeout=cfg.timeout,
                )
            else:
                response = await asyncio.wait_for(
                    provider.complete(system, fmt_messages, fmt_tools),
                    timeout=cfg.timeout,
                )
            response._api_latency_ms = int((time.time() - _api_start) * 1000)
            metrics.record_api_call(
                _model, _prov, response.usage, latency_ms=response._api_latency_ms,
            )
            return response
        except TimeoutError:
            log.error("[%s] API call timed out after %.0fs", trace_id[:8], cfg.timeout)
            if metrics.ENABLED:
                metrics.API_CALLS_TOTAL.labels(model=_model, provider=_prov, status="timeout").inc()
            raise
        except Exception as exc:
            log.warning("[%s] API error: %s: %s", trace_id[:8], type(exc).__name__, str(exc)[:200])
            if metrics.ENABLED:
                metrics.API_CALLS_TOTAL.labels(model=_model, provider=_prov, status="error").inc()
            if not is_transient_error(exc) or attempt >= cfg.api_retries:
                raise
            delay = cfg.api_retry_base_delay * (2 ** attempt) * (0.5 + random.random())  # noqa: S311 — jitter for backoff timing
            log.warning("[%s] Transient API error (attempt %d/%d): %s — retrying in %.1fs",
                        trace_id[:8], attempt + 1, cfg.api_retries + 1, exc, delay)
            last_exc = exc
            if metrics.ENABLED:
                metrics.API_RETRIES_TOTAL.labels(model=_model, provider=_prov).inc()
            await asyncio.sleep(delay)

    raise last_exc  # type: ignore[misc]  # loop always runs ≥1 iteration; last_exc is set on retry path


async def run_single_shot(
    provider: LLMProvider,
    system: SystemPrompt,
    messages: list[Message],
    tools: list[dict[str, Any]],  # ignored; Any justified: tool schema dicts
    tool_executor: ToolRegistry,  # ignored
    config: LoopConfig | None = None,
    cost: CostContext | None = None,
    on_response: Callable[[LLMResponse], Any] | None = None,  # Any justified: may be sync or async
    on_tool_results: Callable[[ToolResultsMessage], Any] | None = None,  # Any justified: may be sync or async
    on_stream_delta: Callable[[StreamDelta], Any] | None = None,  # Any justified: may be sync or async
) -> LLMResponse:
    """Single model call, no tools. For constrained models or simple queries."""
    cfg = config or LoopConfig()
    cc = cost if cost else CostContext.none()
    trace_id = cfg.trace_id or str(uuid.uuid4())

    fmt_messages = provider.format_messages(messages)

    response = await _call_provider_with_retry(
        provider, system, fmt_messages, [],
        cfg=cfg, trace_id=trace_id, on_stream_delta=on_stream_delta,
    )

    messages.append(response.to_internal_message())

    if cc.metering and cc.cost_rates:
        latency = getattr(response, "_api_latency_ms", None)
        response.total_cost = await cc.metering.record(
            session_id=cc.session_id,
            model=cc.model_name, provider=cc.provider_name,
            usage=response.usage, cost_rates=cc.cost_rates,
            trace_id=trace_id, latency_ms=latency,
            converter=cc.converter, currency=cc.currency,
        )

    if on_response:
        await on_response(response) if inspect.iscoroutinefunction(on_response) \
            else on_response(response)

    return response


async def _stream_to_response(
    provider: LLMProvider, system: SystemPrompt, messages: list[dict[str, Any]],
    tools: list[dict[str, Any]], on_delta: Callable[[StreamDelta], Any] | None,
) -> LLMResponse:
    """Consume provider.stream(), call on_delta for each chunk, return aggregated LLMResponse."""
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls_building: dict[int, dict[str, Any]] = {}  # index → {id, name, args_json}
    stop_reason = "end_turn"
    usage = Usage()
    ttft: float | None = None
    stream_start = time.time()

    async for delta in provider.stream(system, messages, tools):
        # Track time-to-first-token
        if ttft is None and (delta.text or delta.thinking):
            ttft = time.time() - stream_start
            log.info("TTFT: %.3fs", ttft)
            if metrics.ENABLED:
                metrics.TTFT.labels(
                    model=getattr(provider, "model", ""),
                    provider=getattr(provider, "provider_name", ""),
                ).observe(ttft)

        # Forward to consumer
        if on_delta:
            if inspect.iscoroutinefunction(on_delta):
                await on_delta(delta)
            else:
                on_delta(delta)

        if delta.text:
            text_parts.append(delta.text)
        if delta.thinking:
            thinking_parts.append(delta.thinking)
        if delta.tool_call_id and delta.tool_call_index >= 0:
            tool_calls_building[delta.tool_call_index] = {
                "id": delta.tool_call_id,
                "name": delta.tool_name,
                "args_json": "",
            }
        if delta.tool_args_delta and delta.tool_call_index >= 0:
            tc = tool_calls_building.get(delta.tool_call_index)
            if tc:
                tc["args_json"] += delta.tool_args_delta
        if delta.stop_reason:
            stop_reason = delta.stop_reason
        if delta.usage:
            usage = delta.usage

    # Build tool calls from accumulated fragments
    tool_calls = []
    for _idx in sorted(tool_calls_building):
        tc = tool_calls_building[_idx]
        args_json = tc["args_json"]
        try:
            args = json.loads(args_json) if args_json else {}
        except (json.JSONDecodeError, ValueError):
            args = {"raw": args_json}
        tool_calls.append(ToolCall(id=tc["id"], name=tc["name"], arguments=args))

    text = "".join(text_parts) or None
    thinking = "".join(thinking_parts) or None

    return LLMResponse(
        text=text,
        tool_calls=tool_calls,
        stop_reason=stop_reason,
        usage=usage,
        thinking=thinking,
    )


def _turn_group_end(messages: list[Message], start: int) -> int:
    """Return the exclusive end index of the turn group starting at `start`.

    A turn group is:
    - A user message (standalone)
    - An assistant message without tool_calls (standalone)
    - An assistant message with tool_calls + all messages up to and
      including the next tool_results (intermediate system user hints
      are included in the group).  Stops at the next assistant turn
      to avoid consuming a later valid turn group.
    """
    if messages[start]["role"] == "agent" and messages[start].get("tool_calls"):
        for j in range(start + 1, len(messages)):
            if messages[j]["role"] == "tool_result":
                return j + 1
            if messages[j]["role"] == "agent":
                break  # next assistant turn — don't consume it
    return start + 1


async def run_agentic_loop(
    provider: LLMProvider,
    system: SystemPrompt,
    messages: list[Message],
    tools: list[dict[str, Any]],  # Any justified: tool schema dicts
    tool_executor: ToolRegistry,
    config: LoopConfig | None = None,
    cost: CostContext | None = None,
    on_response: Callable[[LLMResponse], Any] | None = None,  # Any justified: may be sync or async
    on_tool_results: Callable[[ToolResultsMessage], Any] | None = None,  # Any justified: may be sync or async
    on_stream_delta: Callable[[StreamDelta], Any] | None = None,  # Any justified: may be sync or async
) -> LLMResponse:
    """Run the provider-agnostic agentic loop.

    Args:
        provider: LLM provider instance.
        system: Formatted system prompt (provider-specific format).
        messages: Conversation messages in internal format.
        tools: Tool schemas in generic format.
        tool_executor: ToolRegistry for executing tool calls.
        config: LoopConfig bundle (turns, timeout, retries, cost limits, behavior flags).
        cost: CostContext bundle (metering, session_id, model_name, cost_rates).
        on_response: Callback(response) after each LLM response.
        on_tool_results: Callback(results_msg) after each tool execution.
        on_stream_delta: Callback for streaming deltas.

    Returns:
        Final LLMResponse from the loop. response.text is the final
        turn's text, or intermediate text from earlier turns if the
        final turn produced none.
    """
    cfg = config or LoopConfig()
    cc = cost if cost else CostContext.none()
    trace_id = cfg.trace_id or str(uuid.uuid4())
    max_turns = max(1, cfg.max_turns)
    max_cost = cfg.max_cost
    max_context_for_tools = cfg.max_context_for_tools
    tool_call_retry = cfg.tool_call_retry
    thinking_concise_hint = cfg.thinking_concise_hint
    fmt_tools = provider.format_tools(tools) if tools else []
    accumulated_cost = 0.0
    fallback_text: list[str] = []

    tool_calls_total = 0
    tool_calls_failed = 0
    all_attachments: list[str] = []

    response = None
    for turn in range(max_turns):
        turn_start = time.time()

        fmt_messages = provider.format_messages(messages)

        response = await _call_provider_with_retry(
            provider, system, fmt_messages, fmt_tools,
            cfg=cfg, trace_id=trace_id, on_stream_delta=on_stream_delta,
        )

        turn_elapsed = time.time() - turn_start
        u = response.usage
        log.info(
            "[%s] turn %d: prompt=%dk gen=%d tokens time=%.1fs%s",
            trace_id[:8], turn,
            (u.input_tokens + u.cache_read_tokens) // 1000,
            u.output_tokens, turn_elapsed,
            f" thinking={response.thinking[:200]}..." if response.thinking else "",
        )

        # Track cost (Prometheus emission happens inside metering.record)
        if cc.metering and cc.cost_rates:
            latency = getattr(response, "_api_latency_ms", None)
            turn_cost = await cc.metering.record(
                session_id=cc.session_id,
                model=cc.model_name, provider=cc.provider_name,
                usage=response.usage, cost_rates=cc.cost_rates,
                trace_id=trace_id, latency_ms=latency,
                converter=cc.converter, currency=cc.currency,
            )
            accumulated_cost += turn_cost

        # Max cost circuit breaker
        if max_cost > 0 and accumulated_cost > max_cost:
            log.warning("[%s] Cost limit reached: $%.4f > $%.2f (turn %d)",
                        trace_id[:8], accumulated_cost, max_cost, turn)
            response.cost_limited = True
            if not response.text and fallback_text:
                response.text = "\n\n".join(fallback_text)
            response.attachments = all_attachments
            response.turns = turn + 1
            response.total_cost = accumulated_cost
            return response

        # Add to messages (skip empty post-attachment turns — no useful content)
        internal_msg = response.to_internal_message()
        _has_content = internal_msg.get("text") or internal_msg.get("tool_calls")
        if _has_content:
            messages.append(internal_msg)

        if on_response:
            await on_response(response) if inspect.iscoroutinefunction(on_response) \
                else on_response(response)

        # Collect text generated alongside tool calls.  If the final
        # turn produces its own text, that wins.  Otherwise this text
        # is surfaced as the response so the user isn't left with silence.
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
        #
        # end_turn takes precedence: when the model explicitly stops, any
        # tool_calls alongside text are treated as the final response.
        # This prevents re-entering the loop when the model intended to stop.
        if not response.tool_calls or response.stop_reason == "end_turn":
            if not response.text and fallback_text:
                response.text = "\n\n".join(fallback_text)
            response.attachments = all_attachments
            response.turns = turn + 1
            response.total_cost = accumulated_cost
            return response

        # Inject wrap-up hint when context is too large for useful tool use
        if max_context_for_tools > 0:
            ctx_tokens = response.usage.context_tokens
            if ctx_tokens > max_context_for_tools:
                messages.append({
                    "role": "user",
                    "content": (
                        f"[system: Context at {ctx_tokens:,} tokens "
                        f"(limit: {max_context_for_tools:,}). Quality degrades "
                        f"at this size. Summarize intermediate results and "
                        f"provide your final answer instead of making more "
                        f"tool calls.]"
                    ),
                })

        # Concise thinking hint for tool-result processing turns
        concise_hint_injected = False
        if thinking_concise_hint and response.tool_calls:
            concise_hint_injected = True

        # Notify consumer about tool execution status
        if on_stream_delta:
            tool_names = [tc.name for tc in response.tool_calls]
            status_delta = StreamDelta(status=f"Running tools: {', '.join(tool_names)}...")
            if inspect.iscoroutinefunction(on_stream_delta):
                await on_stream_delta(status_delta)
            else:
                on_stream_delta(status_delta)

        # Execute tool calls in parallel
        async def _execute_tool(tc: ToolCall) -> dict[str, Any]:
            log.info("[%s] Tool call: %s(%s)",
                     trace_id[:8], tc.name, _truncate_args(tc.arguments))
            tool_result = await tool_executor.execute(tc.name, tc.arguments)
            # tool_result is {"text": str, "attachments": list[str]}
            return {
                "tool_call_id": tc.id,
                "tool_name": tc.name,
                "content": tool_result["text"],
                "_attachments": tool_result.get("attachments", []),
            }

        tasks = [_execute_tool(tc) for tc in response.tool_calls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Handle any exceptions from parallel execution
        final_results = []
        for i, result in enumerate(results):
            tc = response.tool_calls[i]
            tool_calls_total += 1
            if isinstance(result, BaseException):
                tool_calls_failed += 1
                log.error("[%s] Tool %s raised exception: %s",
                          trace_id[:8], tc.name, result)
                final_results.append({
                    "tool_call_id": tc.id,
                    "tool_name": tc.name,
                    "content": f"Error: {type(result).__name__}: {result}",
                })
            else:
                # Collect file attachments produced by tools (deduplicate)
                for _att in result.pop("_attachments", []):
                    if _att not in all_attachments:
                        all_attachments.append(_att)
                # Check if tool returned an error (argument errors, etc.)
                content = result.get("content", "")
                if isinstance(content, str) and content.startswith("Error:"):
                    tool_calls_failed += 1
                    # Tool call retry: give the model one chance to fix bad args
                    if tool_call_retry and "Invalid arguments" in content:
                        result["content"] = (
                            f"{content}\n\n"
                            f"Your tool call had invalid arguments. "
                            f"Here is what you sent: {_truncate_args(tc.arguments)}. "
                            f"Please try again with valid JSON arguments."
                        )
                final_results.append(result)

        results_msg: ToolResultsMessage = {"role": "tool_result", "results": final_results}
        messages.append(results_msg)

        # Inject concise thinking hint after tool results
        if concise_hint_injected:
            messages.append({
                "role": "user",
                "content": "[system: Respond concisely. Choose next action quickly.]",
            })

        if on_tool_results:
            await on_tool_results(results_msg) if inspect.iscoroutinefunction(on_tool_results) \
                else on_tool_results(results_msg)

        if tool_calls_total >= 4:
            success_rate = 1.0 - (tool_calls_failed / tool_calls_total)
            if success_rate < cfg.tool_success_warn_threshold:
                log.warning(
                    "[%s] Tool success rate %.0f%% (%d/%d) — model may be "
                    "struggling with the configured toolset",
                    trace_id[:8], success_rate * 100,
                    tool_calls_total - tool_calls_failed, tool_calls_total,
                )

        # Warn agent when approaching turn limit (2 turns remaining)
        remaining = max_turns - (turn + 1)
        if remaining == 2:
            warning_text = (
                f"[system: You have 2 tool-use turns remaining out of {max_turns}. "
                f"Wrap up your work and provide a final answer.]"
            )
            messages.append({"role": "user", "content": warning_text})

    log.warning("[%s] Max turns (%d) reached for session %s",
                trace_id[:8], max_turns, cc.session_id)
    if response is not None:
        stop_msg = f"\n[Stopped: maximum tool-use turns ({max_turns}) reached]"
        if response.text:
            response.text += stop_msg
        elif fallback_text:
            response.text = "\n\n".join(fallback_text) + stop_msg
        else:
            response.text = stop_msg
        response.attachments = all_attachments
    response.turns = max_turns  # type: ignore[union-attr]  # response is always set when loop exits normally
    response.total_cost = accumulated_cost  # type: ignore[union-attr]
    return response  # type: ignore[return-value]  # same — mypy can't prove the loop body always executes


def is_transient_error(exc: BaseException) -> bool:
    """Check if an exception is transient and worth retrying.

    Uses class name matching to work with both Anthropic and OpenAI SDKs
    without importing them. Never retries auth (401), bad request (400),
    or permission (403) errors.

    Also handles httpx exceptions from the SDK-free OpenAI-compatible
    provider fallback path.
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

    # httpx exceptions from SDK-free provider fallback (class-name based
    # to avoid hard-importing httpx in the agentic module).
    if cls_name == "HTTPStatusError":
        # httpx.HTTPStatusError stores status on response.status_code
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None) if resp else None
        return status is not None and status >= 429
    # All httpx transport/network/timeout errors that represent temporary
    # failures.  Covers the full httpx exception tree under NetworkError,
    # TimeoutException, and ProtocolError.
    _httpx_transient = {
        "TimeoutException", "ConnectTimeout", "ReadTimeout",
        "WriteTimeout", "PoolTimeout",
        "ConnectError", "ReadError", "WriteError",
        "CloseError", "ProxyError",
        "NetworkError", "TransportError",
        "RemoteProtocolError", "LocalProtocolError", "ProtocolError",
    }
    if cls_name in _httpx_transient:
        return True

    # Mistral SDK exceptions — status_code attribute, no named subclasses
    if cls_name in ("MistralError", "SDKError", "HTTPValidationError"):
        status = getattr(exc, "status_code", None)
        if status is not None:
            if status in (400, 401, 403, 404, 422):
                return False
            return bool(status >= 429)
        return False

    # Connection-level errors
    return isinstance(exc, (ConnectionError, OSError))


def _truncate_args(args: dict[str, Any], max_len: int = 200) -> str:
    """Truncate tool arguments for logging."""
    s = str(args)
    return s[:max_len] + "..." if len(s) > max_len else s
