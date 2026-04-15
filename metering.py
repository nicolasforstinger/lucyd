"""Cost metering with billing period support.

Records API call costs (in EUR) to PostgreSQL with billing period
segmentation.  Agent and client identity are set once at init.  Consumers
(CLI, Grafana, psql) handle aggregation — the daemon only emits raw records.
"""

from __future__ import annotations

import datetime
import logging
import time
from typing import TYPE_CHECKING, Any

import asyncpg

import metrics

if TYPE_CHECKING:
    from conversion import CurrencyConverter
    from providers import Usage

log = logging.getLogger(__name__)


def _current_billing_period() -> str:
    return time.strftime("%Y-%m")


def _serialize(v: object) -> str | float | object:
    import datetime as _dt
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, __import__("decimal").Decimal):
        return float(v)
    return v


class MeteringDB:
    """Cost tracking backed by PostgreSQL.

    Records API call costs with billing periods and provider attribution.
    Client and agent identity are set once at construction and used for all
    records.  Requires an asyncpg connection pool.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        client_id: str = "",
        agent_id: str = "",
    ) -> None:
        self._pool = pool
        self._client_id = client_id
        self._agent_id = agent_id

    # ── Recording ─────────────────────────────────────────────────

    async def record(
        self,
        session_id: str,
        model: str,
        provider: str,
        usage: Usage,
        cost_rates: list[float],
        call_type: str = "agentic",
        trace_id: str = "",
        latency_ms: int | None = None,
        success: bool = True,
        error_type: str | None = None,
        currency: str = "EUR",
        converter: CurrencyConverter | None = None,
        cost_override: float | None = None,
    ) -> float:
        """Record an API call and return calculated cost in EUR.

        When *cost_override* is set the token-rate math is skipped and the
        caller-provided cost is used directly (e.g. TTS per-character billing).
        """
        if cost_override is not None:
            cost_val = cost_override
        elif not cost_rates:
            return 0.0
        else:
            input_rate = cost_rates[0] if len(cost_rates) > 0 else 0.0
            output_rate = cost_rates[1] if len(cost_rates) > 1 else 0.0
            cache_read_rate = cost_rates[2] if len(cost_rates) > 2 else 0.0
            cache_write_rate = cost_rates[3] if len(cost_rates) > 3 else 0.0

            cost_val = (
                usage.input_tokens * input_rate / 1_000_000
                + usage.output_tokens * output_rate / 1_000_000
                + usage.cache_read_tokens * cache_read_rate / 1_000_000
                + usage.cache_write_tokens * cache_write_rate / 1_000_000
            )

        # Convert to EUR if the provider bills in a different currency.
        fx_rate: float | None = None
        if converter is not None and currency != "EUR":
            cost_val, fx_rate = converter.convert(cost_val, currency)

        now = int(time.time())
        billing_period = _current_billing_period()

        try:
            await self._pool.execute(
                """INSERT INTO metering.costs (
                    client_id, agent_id, timestamp,
                    session_id, model, provider,
                    input_tokens, output_tokens,
                    cache_read_tokens, cache_write_tokens,
                    cost_eur, fx_rate, call_type, trace_id,
                    billing_period, latency_ms, success, error_type
                ) VALUES (
                    $1, $2, to_timestamp($3),
                    $4, $5, $6,
                    $7, $8, $9, $10,
                    $11, $12, $13, $14,
                    $15, $16, $17, $18
                )""",
                self._client_id, self._agent_id, now,
                session_id, model, provider,
                usage.input_tokens, usage.output_tokens,
                usage.cache_read_tokens, usage.cache_write_tokens,
                cost_val, fx_rate, call_type, trace_id,
                billing_period, latency_ms, success, error_type,
            )
        except Exception as e:
            log.warning("Failed to record cost: %s", e, exc_info=True)

        # Emit cost to Prometheus — single point for all cost recording.
        # Call-level metrics (tokens, calls, latency) are emitted at the
        # actual call site via metrics.record_api_call().
        # Note: cost is ALWAYS persisted to the database above, regardless
        # of whether the Prometheus metric is emitted.  The guard below only
        # prevents dashboard pollution from empty labels — it never drops cost.
        if metrics.ENABLED and provider:
            metrics.API_COST.labels(model=model, provider=provider).inc(cost_val)
        elif metrics.ENABLED:
            log.error(
                "Cost recorded to DB but Prometheus metric skipped — "
                "empty provider label (model=%s, cost=%.6f EUR). "
                "Fix the provider config.", model, cost_val,
            )

        return float(cost_val)

    # ── Queries ───────────────────────────────────────────────────

    async def query(self, sql: str, *args: str) -> list[asyncpg.Record]:
        """Read-only query."""
        rows: list[asyncpg.Record] = await self._pool.fetch(sql, *args)
        return rows

    async def month_total(self, billing_period: str = "") -> float:
        """Total cost for current agent in a billing period.  Default: current month."""
        if not billing_period:
            billing_period = _current_billing_period()
        val = await self._pool.fetchval(
            "SELECT COALESCE(SUM(cost_eur), 0.0) FROM metering.costs "
            "WHERE client_id = $1 AND agent_id = $2 "
            "AND billing_period = $3 AND success = TRUE",
            self._client_id, self._agent_id, billing_period,
        )
        return float(val) if val is not None else 0.0

    # ── Records ─────────────────────────────────────────────────────

    async def get_records(self, billing_period: str = "") -> dict[str, Any]:  # Any justified: JSON-serializable dict for HTTP response
        """Return raw cost records for a billing period.

        No aggregation — consumers (psql, Grafana, scripts) do that.
        Default period: current month.
        """
        if not billing_period:
            billing_period = _current_billing_period()
        rows = await self.query(
            """SELECT timestamp, model, provider, call_type,
                      input_tokens, output_tokens,
                      cache_read_tokens, cache_write_tokens,
                      cost_eur, fx_rate, session_id, trace_id,
                      latency_ms, success, error_type
               FROM metering.costs
               WHERE client_id = $1 AND agent_id = $2
               AND billing_period = $3
               ORDER BY timestamp""",
            self._client_id, self._agent_id, billing_period,
        )
        return {
            "client_id": self._client_id,
            "agent_id": self._agent_id,
            "billing_period": billing_period,
            "currency": "EUR",
            "records": [
                {k: _serialize(v) for k, v in dict(r).items()}
                for r in rows
            ],
        }

    # ── Maintenance ───────────────────────────────────────────────

    async def enforce_retention(self, max_months: int = 84) -> int:
        """Delete records older than max_months.  Returns count deleted."""
        today = datetime.date.today()
        month = today.month - max_months
        year = today.year
        while month <= 0:
            month += 12
            year -= 1
        cutoff_ts = int(time.mktime(datetime.date(year, month, 1).timetuple()))

        result: str = await self._pool.execute(
            "DELETE FROM metering.costs WHERE timestamp < to_timestamp($1)",
            cutoff_ts,
        )
        # asyncpg returns "DELETE N" where N is the count.
        deleted = int(result.split()[-1]) if result else 0
        if deleted > 0:
            log.info(
                "Metering retention: deleted %d records older than %d months",
                deleted, max_months,
            )
        return deleted
