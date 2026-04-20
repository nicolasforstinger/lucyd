"""Tests for the metering module (asyncpg backend)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from metering import MeteringDB, _current_billing_period

TEST_CLIENT_ID = "test"
TEST_AGENT_ID = "test_agent"


@dataclass
class MockUsage:
    input_tokens: int = 100
    output_tokens: int = 50
    cache_read_tokens: int = 20
    cache_write_tokens: int = 10


@pytest.fixture
async def metering_db(pool: Any) -> MeteringDB:
    """Fresh MeteringDB backed by test pool."""
    return MeteringDB(pool)


@pytest.fixture
async def populated_db(pool: Any) -> MeteringDB:
    """MeteringDB with 4 sample records in the current billing period."""
    usage = MockUsage()
    db = MeteringDB(pool)
    await db.record("sess_1", "mistral-large", "mistral",
                    usage, [3.0, 9.0, 0.3], call_type="agentic", trace_id="t1")
    await db.record("sess_1", "mistral-small", "mistral",
                    usage, [0.2, 0.6, 0.02], call_type="agentic", trace_id="t2")
    await db.record("sess_2", "mistral-large", "mistral",
                    usage, [3.0, 9.0, 0.3], call_type="compaction", trace_id="t3")
    await db.record("sess_3", "mistral-large", "mistral",
                    usage, [3.0, 9.0, 0.3], call_type="agentic", trace_id="t4")
    return db


# ── Schema ────────────────────────────────────────────────────────


class TestSchema:
    @pytest.mark.asyncio
    async def test_table_exists(self, pool: Any) -> None:
        row = await pool.fetchrow(
            "SELECT 1 FROM pg_tables "
            "WHERE schemaname = 'metering' AND tablename = 'costs'"
        )
        assert row is not None

    @pytest.mark.asyncio
    async def test_idempotent_schema(self, pool: Any) -> None:
        """Second MeteringDB on same pool should not fail."""
        MeteringDB(pool)
        MeteringDB(pool)


# ── Recording ─────────────────────────────────────────────────────


class TestRecord:
    @pytest.mark.asyncio
    async def test_basic_record(self, metering_db: MeteringDB) -> None:
        usage = MockUsage(input_tokens=1000, output_tokens=500,
                          cache_read_tokens=200, cache_write_tokens=0)
        # rates: [3.0, 9.0, 0.3] per Mtok
        cost = await metering_db.record(
            "sess_1", "mistral-large", "mistral",
            usage, [3.0, 9.0, 0.3], trace_id="abc",
        )
        expected = 1000 * 3.0 / 1e6 + 500 * 9.0 / 1e6 + 200 * 0.3 / 1e6
        assert abs(cost - expected) < 1e-9

        rows = await metering_db.query("SELECT * FROM metering.costs")
        assert len(rows) == 1
        r = rows[0]
        assert r["session_id"] == "sess_1"
        assert r["model"] == "mistral-large"
        assert r["provider"] == "mistral"
        assert r["fx_rate"] is None
        assert r["billing_period"] == _current_billing_period()
        assert r["success"] is True
        assert r["call_type"] == "agentic"

    @pytest.mark.asyncio
    async def test_failed_call(self, metering_db: MeteringDB) -> None:
        usage = MockUsage(input_tokens=100, output_tokens=0)
        await metering_db.record("sess_1", "model", "prov", usage, [1.0],
                                 success=False, error_type="transient")
        rows = await metering_db.query(
            "SELECT success, error_type FROM metering.costs"
        )
        assert rows[0]["success"] is False
        assert rows[0]["error_type"] == "transient"

    @pytest.mark.asyncio
    async def test_latency_ms(self, metering_db: MeteringDB) -> None:
        usage = MockUsage()
        await metering_db.record("s", "m", "p", usage, [1.0], latency_ms=450)
        rows = await metering_db.query(
            "SELECT latency_ms FROM metering.costs"
        )
        assert rows[0]["latency_ms"] == 450

    @pytest.mark.asyncio
    async def test_empty_rates_returns_zero(self, metering_db: MeteringDB) -> None:
        cost = await metering_db.record("s", "m", "p", MockUsage(), [])
        assert cost == 0.0

    @pytest.mark.asyncio
    async def test_call_types(self, metering_db: MeteringDB) -> None:
        for ct in ("agentic", "compaction", "consolidation", "embedding"):
            await metering_db.record("s", "m", "p", MockUsage(), [1.0],
                                     call_type=ct)
        rows = await metering_db.query(
            "SELECT call_type FROM metering.costs ORDER BY id"
        )
        types = [r["call_type"] for r in rows]
        assert types == ["agentic", "compaction", "consolidation", "embedding"]

    @pytest.mark.asyncio
    async def test_four_element_rates_include_cache_write(
        self, metering_db: MeteringDB,
    ) -> None:
        usage = MockUsage(input_tokens=1000, output_tokens=500,
                          cache_read_tokens=200, cache_write_tokens=100)
        cost = await metering_db.record(
            "s", "m", "p", usage, [3.0, 15.0, 0.3, 3.75],
        )
        expected = (1000 * 3.0 + 500 * 15.0 + 200 * 0.3 + 100 * 3.75) / 1e6
        assert abs(cost - expected) < 1e-9

    @pytest.mark.asyncio
    async def test_three_element_rates_omits_cache_write(
        self, metering_db: MeteringDB,
    ) -> None:
        """3-element cost_rates list omits cache_write (defaults to 0)."""
        usage = MockUsage(input_tokens=1000, output_tokens=500,
                          cache_read_tokens=200, cache_write_tokens=100)
        cost = await metering_db.record(
            "s", "m", "p", usage, [3.0, 15.0, 0.3],
        )
        expected = (1000 * 3.0 + 500 * 15.0 + 200 * 0.3) / 1e6
        assert abs(cost - expected) < 1e-9

    @pytest.mark.asyncio
    async def test_converter_applied_for_non_eur(
        self, metering_db: MeteringDB,
    ) -> None:
        """Converter is called when currency != EUR."""
        usage = MockUsage(input_tokens=1000000, output_tokens=0)
        converter = MagicMock()
        converter.convert.return_value = (2.6087, 1.15)
        cost = await metering_db.record(
            "s", "m", "p", usage, [3.0],
            converter=converter, currency="USD",
        )
        converter.convert.assert_called_once()
        assert cost == 2.6087
        rows = await metering_db.query(
            "SELECT fx_rate FROM metering.costs ORDER BY id DESC LIMIT 1"
        )
        assert float(rows[0]["fx_rate"]) == 1.15

    @pytest.mark.asyncio
    async def test_converter_not_called_for_eur(
        self, metering_db: MeteringDB,
    ) -> None:
        """Converter is skipped when currency is EUR; fx_rate is NULL."""
        usage = MockUsage(input_tokens=1000000, output_tokens=0)
        converter = MagicMock()
        await metering_db.record(
            "s", "m", "p", usage, [3.0],
            converter=converter, currency="EUR",
        )
        converter.convert.assert_not_called()
        rows = await metering_db.query(
            "SELECT fx_rate FROM metering.costs ORDER BY id DESC LIMIT 1"
        )
        assert rows[0]["fx_rate"] is None

    @pytest.mark.asyncio
    async def test_cost_override_bypasses_token_math(
        self, metering_db: MeteringDB,
    ) -> None:
        """cost_override skips rate calculation and uses the value directly."""
        usage = MockUsage(input_tokens=1000000, output_tokens=500000)
        cost = await metering_db.record(
            "s", "m", "p", usage, [3.0, 15.0],
            cost_override=42.0,
        )
        assert cost == 42.0
        rows = await metering_db.query("SELECT cost_eur FROM metering.costs")
        assert float(rows[0]["cost_eur"]) == 42.0

    @pytest.mark.asyncio
    async def test_cost_override_with_empty_rates(
        self, metering_db: MeteringDB,
    ) -> None:
        """cost_override works even when cost_rates is empty."""
        usage = MockUsage()
        cost = await metering_db.record(
            "s", "m", "p", usage, [],
            cost_override=5.0,
        )
        assert cost == 5.0

    @pytest.mark.asyncio
    async def test_prometheus_api_cost_emitted(
        self, metering_db: MeteringDB,
    ) -> None:
        """record() emits API_COST to Prometheus."""
        import metrics
        mock_counter = MagicMock()
        with patch.object(metrics, "ENABLED", True), \
             patch.object(metrics, "API_COST", mock_counter):
            await metering_db.record(
                "s", "model-x", "prov-y", MockUsage(input_tokens=1000),
                [3.0],
            )
        mock_counter.labels.assert_called_with(model="model-x", provider="prov-y")
        mock_counter.labels().inc.assert_called_once()


# ── Queries ───────────────────────────────────────────────────────


class TestQueries:
    @pytest.mark.asyncio
    async def test_month_total(self, populated_db: MeteringDB) -> None:
        bp = _current_billing_period()
        total = await populated_db.month_total(bp)
        assert total > 0

    @pytest.mark.asyncio
    async def test_month_total_default_period(
        self, populated_db: MeteringDB,
    ) -> None:
        total = await populated_db.month_total()
        assert total > 0

    @pytest.mark.asyncio
    async def test_empty_db_returns_zero(self, pool: Any) -> None:
        db = MeteringDB(pool)
        assert await db.month_total() == 0.0


# ── Records ──────────────────────────────────────────────────────


class TestGetRecords:
    @pytest.mark.asyncio
    async def test_returns_raw_records(self, populated_db: MeteringDB) -> None:
        bp = _current_billing_period()
        data = await populated_db.get_records(bp)
        assert data["billing_period"] == bp
        assert data["currency"] == "EUR"  # summary-level, always EUR
        assert len(data["records"]) == 4

    @pytest.mark.asyncio
    async def test_record_fields(self, populated_db: MeteringDB) -> None:
        bp = _current_billing_period()
        rec = (await populated_db.get_records(bp))["records"][0]
        assert "timestamp" in rec
        assert "model" in rec
        assert "provider" in rec
        assert "call_type" in rec
        assert "input_tokens" in rec
        assert "output_tokens" in rec
        assert "cost_eur" in rec
        assert "fx_rate" in rec
        assert "session_id" in rec
        assert "trace_id" in rec
        assert "latency_ms" in rec
        assert "success" in rec

    @pytest.mark.asyncio
    async def test_empty_db(self, metering_db: MeteringDB) -> None:
        data = await metering_db.get_records()
        assert data["records"] == []

    @pytest.mark.asyncio
    async def test_defaults_to_current_month(
        self, populated_db: MeteringDB,
    ) -> None:
        data = await populated_db.get_records()
        assert data["billing_period"] == _current_billing_period()
        assert len(data["records"]) == 4


# ── Maintenance ───────────────────────────────────────────────────


class TestMaintenance:
    @pytest.mark.asyncio
    async def test_enforce_retention(self, pool: Any) -> None:
        db = MeteringDB(pool)
        # Insert an old record (2 years ago) directly via pool
        old_ts = int(time.time()) - 2 * 365 * 86400
        await pool.execute(
            """INSERT INTO metering.costs
               (timestamp, session_id, model,
                provider, cost_eur, call_type, billing_period, success)
               VALUES (to_timestamp($1), 's', 'm',
                       'p', 0.1, 'agentic', '2024-01', TRUE)""",
            old_ts,
        )
        # Insert a recent record
        await pool.execute(
            """INSERT INTO metering.costs
               (timestamp, session_id, model,
                provider, cost_eur, call_type, billing_period, success)
               VALUES (to_timestamp($1), 's', 'm',
                       'p', 0.1, 'agentic', $2, TRUE)""",
            int(time.time()),
            _current_billing_period(),
        )

        deleted = await db.enforce_retention(max_months=12)
        assert deleted == 1
        rows = await db.query("SELECT COUNT(*) AS cnt FROM metering.costs")
        assert rows[0]["cnt"] == 1
