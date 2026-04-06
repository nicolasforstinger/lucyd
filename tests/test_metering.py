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
    return MeteringDB(pool, client_id=TEST_CLIENT_ID, agent_id="cust_1")


@pytest.fixture
async def populated_db(pool: Any) -> MeteringDB:
    """MeteringDB with sample records for agent_id=cust_a."""
    usage = MockUsage()
    db_a = MeteringDB(pool, client_id=TEST_CLIENT_ID, agent_id="cust_a")
    # 3 records for cust_a
    await db_a.record("sess_1", "mistral-large", "mistral",
                      usage, [3.0, 9.0, 0.3], call_type="agentic", trace_id="t1")
    await db_a.record("sess_1", "mistral-small", "mistral",
                      usage, [0.2, 0.6, 0.02], call_type="agentic", trace_id="t2")
    await db_a.record("sess_2", "mistral-large", "mistral",
                      usage, [3.0, 9.0, 0.3], call_type="compaction", trace_id="t3")
    # 1 record for cust_b via separate instance sharing same pool
    db_b = MeteringDB(pool, client_id=TEST_CLIENT_ID, agent_id="cust_b")
    await db_b.record("sess_3", "mistral-large", "mistral",
                      usage, [3.0, 9.0, 0.3], call_type="agentic", trace_id="t4")
    # Return the cust_a instance; stash cust_b for tests that need it
    db_a._db_b = db_b  # type: ignore[attr-defined]
    return db_a


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
        MeteringDB(pool, client_id=TEST_CLIENT_ID, agent_id="a")
        MeteringDB(pool, client_id=TEST_CLIENT_ID, agent_id="b")


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
        assert r["agent_id"] == "cust_1"
        assert r["client_id"] == TEST_CLIENT_ID
        assert r["session_id"] == "sess_1"
        assert r["model"] == "mistral-large"
        assert r["provider"] == "mistral"
        assert r["currency"] == "EUR"
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
    async def test_three_element_rates_backward_compatible(
        self, metering_db: MeteringDB,
    ) -> None:
        """Old 3-element rates still work -- cache_write defaults to 0."""
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
        converter.convert.return_value = 2.6087
        cost = await metering_db.record(
            "s", "m", "p", usage, [3.0],
            converter=converter, currency="USD",
        )
        converter.convert.assert_called_once()
        assert cost == 2.6087

    @pytest.mark.asyncio
    async def test_converter_not_called_for_eur(
        self, metering_db: MeteringDB,
    ) -> None:
        """Converter is skipped when currency is EUR."""
        usage = MockUsage(input_tokens=1000000, output_tokens=0)
        converter = MagicMock()
        await metering_db.record(
            "s", "m", "p", usage, [3.0],
            converter=converter, currency="EUR",
        )
        converter.convert.assert_not_called()

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
        rows = await metering_db.query("SELECT cost FROM metering.costs")
        assert float(rows[0]["cost"]) == 42.0

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
        total_a = await populated_db.month_total(bp)
        total_b = await populated_db._db_b.month_total(bp)  # type: ignore[attr-defined]
        assert total_a > 0
        assert total_b > 0
        assert total_a > total_b  # cust_a has 3 records

    @pytest.mark.asyncio
    async def test_month_total_default_period(
        self, populated_db: MeteringDB,
    ) -> None:
        total = await populated_db.month_total()
        assert total > 0

    @pytest.mark.asyncio
    async def test_unknown_agent_returns_zero(self, pool: Any) -> None:
        db = MeteringDB(pool, client_id=TEST_CLIENT_ID, agent_id="nonexistent")
        assert await db.month_total() == 0.0


# ── Records ──────────────────────────────────────────────────────


class TestGetRecords:
    @pytest.mark.asyncio
    async def test_returns_raw_records(self, populated_db: MeteringDB) -> None:
        bp = _current_billing_period()
        data = await populated_db.get_records(bp)
        assert data["agent_id"] == "cust_a"
        assert data["client_id"] == TEST_CLIENT_ID
        assert data["billing_period"] == bp
        assert data["currency"] == "EUR"
        assert len(data["records"]) == 3

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
        assert "cost" in rec
        assert "session_id" in rec
        assert "trace_id" in rec
        assert "latency_ms" in rec
        assert "success" in rec

    @pytest.mark.asyncio
    async def test_filters_by_agent(self, populated_db: MeteringDB) -> None:
        bp = _current_billing_period()
        records = (await populated_db.get_records(bp))["records"]
        session_ids = {r["session_id"] for r in records}
        assert session_ids <= {"sess_1", "sess_2"}

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
        assert len(data["records"]) == 3


# ── Maintenance ───────────────────────────────────────────────────


class TestMaintenance:
    @pytest.mark.asyncio
    async def test_enforce_retention(self, pool: Any) -> None:
        db = MeteringDB(pool, client_id=TEST_CLIENT_ID, agent_id="retention")
        # Insert an old record (2 years ago) directly via pool
        old_ts = int(time.time()) - 2 * 365 * 86400
        await pool.execute(
            """INSERT INTO metering.costs
               (client_id, agent_id, timestamp, session_id, model,
                provider, cost, currency, call_type, billing_period, success)
               VALUES ($1, $2, to_timestamp($3), 's', 'm',
                       'p', 0.1, 'EUR', 'agentic', '2024-01', TRUE)""",
            TEST_CLIENT_ID, "retention", old_ts,
        )
        # Insert a recent record
        await pool.execute(
            """INSERT INTO metering.costs
               (client_id, agent_id, timestamp, session_id, model,
                provider, cost, currency, call_type, billing_period, success)
               VALUES ($1, $2, to_timestamp($3), 's', 'm',
                       'p', 0.1, 'EUR', 'agentic', $4, TRUE)""",
            TEST_CLIENT_ID, "retention", int(time.time()),
            _current_billing_period(),
        )

        deleted = await db.enforce_retention(max_months=12)
        assert deleted == 1
        rows = await db.query(
            "SELECT COUNT(*) AS cnt FROM metering.costs "
            "WHERE client_id = $1 AND agent_id = $2",
            TEST_CLIENT_ID, "retention",
        )
        assert rows[0]["cnt"] == 1
