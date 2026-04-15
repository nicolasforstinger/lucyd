"""Prometheus metrics for Lucyd daemon.

All metric objects are module-level singletons. Import and use directly.
Gracefully degrades to no-ops if prometheus_client is not installed.
"""

from __future__ import annotations

try:
    from prometheus_client import Counter, Gauge, Histogram, generate_latest

    # ── Per-message ──────────────────────────────────────────────────

    # Per-message label set: drill from aggregate down to individual conversation
    _MSG_LABELS = ["channel_id", "task_type", "session_id", "sender"]

    MESSAGES_TOTAL = Counter(
        "lucyd_messages_total",
        "Total messages processed",
        _MSG_LABELS,
    )

    MESSAGE_DURATION = Histogram(
        "lucyd_message_duration_seconds",
        "End-to-end message processing duration (received → response returned)",
        _MSG_LABELS,
        buckets=(1, 2, 5, 10, 20, 30, 60, 120, 300, 600),
    )

    MESSAGE_COST = Histogram(
        "lucyd_message_cost_eur",
        "Total cost per message in EUR",
        _MSG_LABELS,
        buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 5),
    )

    AGENTIC_TURNS = Histogram(
        "lucyd_agentic_turns",
        "Number of agentic loop turns per message",
        _MSG_LABELS,
        buckets=(1, 2, 3, 5, 8, 10, 15, 20, 30, 50),
    )

    CONTEXT_UTILIZATION = Histogram(
        "lucyd_context_utilization_ratio",
        "Context window utilization (tokens used / max tokens)",
        _MSG_LABELS,
        buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.0),
    )

    # ── Per-provider/model ───────────────────────────────────────────

    API_CALLS_TOTAL = Counter(
        "lucyd_api_calls_total",
        "LLM API calls",
        ["model", "provider", "status"],
    )

    API_LATENCY = Histogram(
        "lucyd_api_latency_seconds",
        "LLM API call latency",
        ["model", "provider"],
        buckets=(0.5, 1, 2, 3, 5, 8, 10, 15, 20, 30, 60),
    )

    TOKENS_TOTAL = Counter(
        "lucyd_tokens_total",
        "Tokens consumed",
        ["direction", "model", "provider"],
    )

    API_COST = Counter(
        "lucyd_api_cost_eur_total",
        "Cumulative LLM API cost in EUR",
        ["model", "provider"],
    )

    # ── Per-tool ─────────────────────────────────────────────────────

    TOOL_CALLS_TOTAL = Counter(
        "lucyd_tool_calls_total",
        "Tool invocations",
        ["tool_name", "status"],
    )

    TOOL_DURATION = Histogram(
        "lucyd_tool_duration_seconds",
        "Tool execution duration",
        ["tool_name"],
        buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
    )

    # ── Per-preprocessor ─────────────────────────────────────────────

    PREPROCESSOR_TOTAL = Counter(
        "lucyd_preprocessor_total",
        "Preprocessor invocations",
        ["name", "status"],
    )

    PREPROCESSOR_DURATION = Histogram(
        "lucyd_preprocessor_duration_seconds",
        "Preprocessor execution duration",
        ["name"],
        buckets=(0.1, 0.5, 1, 2, 5, 10, 30),
    )

    # ── Memory operations ──────────────────────────────────────────

    MEMORY_OPS_TOTAL = Counter(
        "lucyd_memory_ops_total",
        "Memory operations",
        ["operation"],
    )

    # ── Per-session ──────────────────────────────────────────────────

    ACTIVE_SESSIONS = Gauge(
        "lucyd_active_sessions",
        "Currently active sessions",
    )

    COMPACTION_TOTAL = Counter(
        "lucyd_compaction_total",
        "Session compaction events",
    )

    COMPACTION_TOKENS_RECLAIMED = Histogram(
        "lucyd_compaction_tokens_reclaimed",
        "Tokens reclaimed per compaction",
        buckets=(1000, 5000, 10000, 25000, 50000, 100000, 200000),
    )

    SESSION_CLOSE_TOTAL = Counter(
        "lucyd_session_close_total",
        "Session close events",
        ["reason"],
    )

    # ── System ───────────────────────────────────────────────────────

    QUEUE_DEPTH = Gauge(
        "lucyd_queue_depth",
        "Message queue depth",
    )

    QUEUE_WAIT_SECONDS = Histogram(
        "lucyd_queue_wait_seconds",
        "Time between queue entry and processing start",
        ["priority"],
        buckets=(0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60),
    )

    UPTIME = Gauge(
        "lucyd_uptime_seconds",
        "Daemon uptime in seconds",
    )

    ERRORS_TOTAL = Counter(
        "lucyd_errors_total",
        "Processing errors",
        ["error_type"],
    )

    # ── Observability blindspot metrics ──────────────────────────────

    TTFT = Histogram(
        "lucyd_ttft_seconds",
        "Time to first token",
        ["model", "provider"],
        buckets=(0.1, 0.2, 0.5, 0.75, 1, 1.5, 2, 3, 5, 8, 10),
    )

    API_RETRIES_TOTAL = Counter(
        "lucyd_api_retries_total",
        "LLM API retry attempts",
        ["model", "provider"],
    )

    SESSION_OPEN_TOTAL = Counter(
        "lucyd_session_open_total",
        "Sessions created",
    )

    CONSOLIDATION_DURATION = Histogram(
        "lucyd_consolidation_duration_seconds",
        "Consolidation wall-clock duration including LLM extraction",
        buckets=(0.5, 1, 2, 5, 10, 20, 30, 60),
    )

    MEMORY_SEARCH_DURATION = Histogram(
        "lucyd_memory_search_duration_seconds",
        "Memory search latency",
        ["search_type"],
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
    )

    MESSAGE_OUTCOME_TOTAL = Counter(
        "lucyd_message_outcome_total",
        "Message processing outcomes",
        ["outcome"],
    )

    # ── Currency conversion ──────────────────────────────────────────

    FX_FETCH_ERRORS_TOTAL = Counter(
        "lucyd_fx_fetch_errors_total",
        "FX rate API fetch failures",
    )

    ENABLED = True

    def record_api_call(
        model: str, provider: str, usage: object, latency_ms: int | None = None,
    ) -> None:
        """Emit call-level Prometheus metrics for an API call.

        Called from _call_provider_with_retry (LLM completions) and from
        embedding paths (memory.py, indexer.py) so that all API calls —
        regardless of call type — appear in token/call/latency dashboards.
        """
        if not provider:
            # Cost still goes to DB via metering.record(); only Prometheus
            # is skipped here to avoid orphaned label series on dashboards.
            import logging as _log
            _log.getLogger("lucyd.metrics").error(
                "Skipping call metrics — empty provider (model=%s)", model)
            return
        API_CALLS_TOTAL.labels(model=model, provider=provider, status="success").inc()
        in_tok = getattr(usage, "input_tokens", 0)
        out_tok = getattr(usage, "output_tokens", 0)
        cache_r = getattr(usage, "cache_read_tokens", 0)
        cache_w = getattr(usage, "cache_write_tokens", 0)
        TOKENS_TOTAL.labels(direction="input", model=model, provider=provider).inc(in_tok)
        TOKENS_TOTAL.labels(direction="output", model=model, provider=provider).inc(out_tok)
        if cache_r:
            TOKENS_TOTAL.labels(direction="cache_read", model=model, provider=provider).inc(cache_r)
        if cache_w:
            TOKENS_TOTAL.labels(direction="cache_write", model=model, provider=provider).inc(cache_w)
        if latency_ms is not None:
            API_LATENCY.labels(model=model, provider=provider).observe(latency_ms / 1000)

except ImportError:
    ENABLED = False
    generate_latest = None  # type: ignore[assignment,unused-ignore]

    def record_api_call(  # type: ignore[misc,unused-ignore]  # no-op fallback; unused when prometheus_client installed
        model: str, provider: str, usage: object, latency_ms: int | None = None,
    ) -> None:
        """No-op fallback when prometheus_client is not installed."""
