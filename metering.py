"""Cost metering with billing period support.

Records API call costs with billing period segmentation and EUR currency.
Agent identity is set once at init.  Consumers (CLI, Grafana, jq) handle
aggregation — the daemon only emits raw records.
"""

from __future__ import annotations

import datetime
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _current_billing_period() -> str:
    """Return current billing period as 'YYYY-MM'."""
    return time.strftime("%Y-%m")


class MeteringDB:
    """Cost tracking database.

    Records API call costs with billing periods and provider attribution.
    Agent identity is set once at construction and used for all records.
    """

    def __init__(self, db_path: str, *, agent_id: str = "", sqlite_timeout: float = 5.0):
        if not db_path:
            raise ValueError("MeteringDB requires a db_path")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._path = db_path
        self._agent_id = agent_id
        self._timeout = sqlite_timeout
        self._ensure_schema()

    @property
    def path(self) -> str:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=self._timeout)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA wal_autocheckpoint=1000")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS costs (
                    timestamp       INTEGER NOT NULL,
                    agent_id     TEXT    NOT NULL,
                    session_id      TEXT    NOT NULL,
                    model           TEXT    NOT NULL,
                    provider        TEXT    NOT NULL DEFAULT '',
                    input_tokens    INTEGER NOT NULL DEFAULT 0,
                    output_tokens   INTEGER NOT NULL DEFAULT 0,
                    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
                    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                    cost            REAL    NOT NULL DEFAULT 0.0,
                    currency        TEXT    NOT NULL DEFAULT 'EUR',
                    call_type       TEXT    NOT NULL DEFAULT 'agentic',
                    trace_id        TEXT,
                    billing_period  TEXT    NOT NULL,
                    latency_ms      INTEGER,
                    success         INTEGER NOT NULL DEFAULT 1,
                    error_type      TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_costs_agent
                    ON costs(agent_id);
                CREATE INDEX IF NOT EXISTS idx_costs_billing
                    ON costs(agent_id, billing_period);
            """)
            conn.commit()
        finally:
            conn.close()

    # ── Recording ─────────────────────────────────────────────────

    def record(
        self,
        session_id: str,
        model: str,
        provider: str,
        usage: Any,
        cost_rates: list[float],
        call_type: str = "agentic",
        trace_id: str = "",
        latency_ms: int | None = None,
        success: bool = True,
        error_type: str | None = None,
        currency: str = "EUR",
    ) -> float:
        """Record an API call and return calculated cost."""
        if not cost_rates:
            return 0.0

        input_rate = cost_rates[0] if len(cost_rates) > 0 else 0.0
        output_rate = cost_rates[1] if len(cost_rates) > 1 else 0.0
        cache_rate = cost_rates[2] if len(cost_rates) > 2 else 0.0

        cost_val = (
            usage.input_tokens * input_rate / 1_000_000
            + usage.output_tokens * output_rate / 1_000_000
            + usage.cache_read_tokens * cache_rate / 1_000_000
        )

        now = int(time.time())
        billing_period = _current_billing_period()

        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO costs (
                    timestamp, agent_id, session_id, model, provider,
                    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                    cost, currency, call_type, trace_id,
                    billing_period, latency_ms, success, error_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    now, self._agent_id, session_id, model, provider,
                    usage.input_tokens, usage.output_tokens,
                    usage.cache_read_tokens, usage.cache_write_tokens,
                    cost_val, currency, call_type, trace_id,
                    billing_period, latency_ms,
                    1 if success else 0, error_type,
                ),
            )
            conn.commit()
        except Exception as e:
            log.warning("Failed to record cost: %s", e)
        finally:
            conn.close()

        return cost_val

    # ── Queries ───────────────────────────────────────────────────

    def query(self, sql: str, params: tuple = ()) -> list:
        """Read-only query.  Returns [] on error or missing DB."""
        if not Path(self._path).exists():
            return []
        conn = self._connect()
        try:
            return conn.execute(sql, params).fetchall()
        except Exception:
            return []
        finally:
            conn.close()

    def month_total(self, billing_period: str = "") -> float:
        """Total cost for current agent in a billing period.  Default: current month."""
        if not billing_period:
            billing_period = _current_billing_period()
        rows = self.query(
            "SELECT COALESCE(SUM(cost), 0.0) AS total FROM costs "
            "WHERE agent_id = ? AND billing_period = ? AND success = 1",
            (self._agent_id, billing_period),
        )
        return float(rows[0]["total"]) if rows else 0.0

    # ── Records ─────────────────────────────────────────────────────

    def get_records(self, billing_period: str = "") -> dict:
        """Return raw cost records for a billing period.

        No aggregation — consumers (jq, Grafana, scripts) do that.
        Default period: current month.
        """
        if not billing_period:
            billing_period = _current_billing_period()
        rows = self.query(
            """SELECT timestamp, model, provider, call_type,
                      input_tokens, output_tokens,
                      cache_read_tokens, cache_write_tokens,
                      cost, currency, session_id, trace_id,
                      latency_ms, success, error_type
               FROM costs
               WHERE agent_id = ? AND billing_period = ?
               ORDER BY timestamp""",
            (self._agent_id, billing_period),
        )
        return {
            "agent_id": self._agent_id,
            "billing_period": billing_period,
            "currency": "EUR",
            "records": [dict(r) for r in rows],
        }

    # ── Maintenance ───────────────────────────────────────────────

    def enforce_retention(self, max_months: int = 12) -> int:
        """Delete records older than max_months.  Returns count deleted."""
        today = datetime.date.today()
        month = today.month - max_months
        year = today.year
        while month <= 0:
            month += 12
            year -= 1
        cutoff_ts = int(time.mktime(datetime.date(year, month, 1).timetuple()))

        conn = self._connect()
        try:
            cursor = conn.execute("DELETE FROM costs WHERE timestamp < ?", (cutoff_ts,))
            deleted = cursor.rowcount
            conn.commit()
            if deleted > 0:
                log.info("Metering retention: deleted %d records older than %d months",
                         deleted, max_months)
            return deleted
        finally:
            conn.close()


