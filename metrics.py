"""Prometheus metrics for Lucyd daemon.

All metric objects are module-level singletons. Import and use directly.
Gracefully degrades to no-ops if prometheus_client is not installed.
"""

from __future__ import annotations

try:
    from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST

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

    UPTIME = Gauge(
        "lucyd_uptime_seconds",
        "Daemon uptime in seconds",
    )

    ERRORS_TOTAL = Counter(
        "lucyd_errors_total",
        "Processing errors",
        ["error_type"],
    )

    ENABLED = True

except ImportError:
    ENABLED = False
    generate_latest = None  # type: ignore[assignment]  # conditional export — function when prometheus_client installed, None otherwise
    CONTENT_TYPE_LATEST = "text/plain"
