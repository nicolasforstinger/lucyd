"""Tests for the metering module."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from metering import MeteringDB, _current_billing_period


@dataclass
class MockUsage:
    input_tokens: int = 100
    output_tokens: int = 50
    cache_read_tokens: int = 20
    cache_write_tokens: int = 10


@pytest.fixture
def metering_db(tmp_path):
    """Fresh MeteringDB in temp directory."""
    return MeteringDB(str(tmp_path / "metering.db"), agent_id="cust_1")


@pytest.fixture
def populated_db(tmp_path):
    """MeteringDB with sample records for agent_id=cust_a."""
    usage = MockUsage()
    db_a = MeteringDB(str(tmp_path / "metering.db"), agent_id="cust_a")
    # 3 records for cust_a
    db_a.record("sess_1", "mistral-large", "mistral",
                usage, [3.0, 9.0, 0.3], call_type="agentic", trace_id="t1")
    db_a.record("sess_1", "mistral-small", "mistral",
                usage, [0.2, 0.6, 0.02], call_type="agentic", trace_id="t2")
    db_a.record("sess_2", "mistral-large", "mistral",
                usage, [3.0, 9.0, 0.3], call_type="compaction", trace_id="t3")
    # 1 record for cust_b via separate instance sharing same DB
    db_b = MeteringDB(str(tmp_path / "metering.db"), agent_id="cust_b")
    db_b.record("sess_3", "mistral-large", "mistral",
                usage, [3.0, 9.0, 0.3], call_type="agentic", trace_id="t4")
    # Return the cust_a instance; tests needing cust_b create their own
    db_a._db_b = db_b  # stash for tests that need the other agent
    return db_a


# ── Schema ────────────────────────────────────────────────────────

class TestSchema:
    def test_creates_db_and_table(self, tmp_path):
        db_path = str(tmp_path / "new.db")
        MeteringDB(db_path)
        assert Path(db_path).exists()
        conn = sqlite3.connect(db_path)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "costs" in tables

    def test_creates_parent_dirs(self, tmp_path):
        db_path = str(tmp_path / "deep" / "nested" / "metering.db")
        MeteringDB(db_path)
        assert Path(db_path).exists()

    def test_rejects_empty_path(self):
        with pytest.raises(ValueError, match="requires a db_path"):
            MeteringDB("")

    def test_idempotent_schema(self, metering_db):
        # Second init on same DB should not fail
        MeteringDB(metering_db.path)

    def test_wal_mode(self, metering_db):
        conn = sqlite3.connect(metering_db.path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"


# ── Recording ─────────────────────────────────────────────────────

class TestRecord:
    def test_basic_record(self, metering_db):
        usage = MockUsage(input_tokens=1000, output_tokens=500,
                          cache_read_tokens=200, cache_write_tokens=0)
        # rates: [3.0, 9.0, 0.3] per Mtok
        cost = metering_db.record(
            "sess_1", "mistral-large", "mistral",
            usage, [3.0, 9.0, 0.3], trace_id="abc",
        )
        expected = 1000 * 3.0 / 1e6 + 500 * 9.0 / 1e6 + 200 * 0.3 / 1e6
        assert abs(cost - expected) < 1e-9

        rows = metering_db.query("SELECT * FROM costs")
        assert len(rows) == 1
        r = rows[0]
        assert r["agent_id"] == "cust_1"
        assert r["session_id"] == "sess_1"
        assert r["model"] == "mistral-large"
        assert r["provider"] == "mistral"
        assert r["currency"] == "EUR"
        assert r["billing_period"] == _current_billing_period()
        assert r["success"] == 1
        assert r["call_type"] == "agentic"

    def test_failed_call(self, metering_db):
        usage = MockUsage(input_tokens=100, output_tokens=0)
        metering_db.record("sess_1", "model", "prov", usage, [1.0],
                           success=False, error_type="transient")
        rows = metering_db.query("SELECT success, error_type FROM costs")
        assert rows[0]["success"] == 0
        assert rows[0]["error_type"] == "transient"

    def test_latency_ms(self, metering_db):
        usage = MockUsage()
        metering_db.record("s", "m", "p", usage, [1.0], latency_ms=450)
        rows = metering_db.query("SELECT latency_ms FROM costs")
        assert rows[0]["latency_ms"] == 450

    def test_empty_rates_returns_zero(self, metering_db):
        cost = metering_db.record("s", "m", "p", MockUsage(), [])
        assert cost == 0.0

    def test_call_types(self, metering_db):
        for ct in ("agentic", "compaction", "consolidation", "embedding"):
            metering_db.record("s", "m", "p", MockUsage(), [1.0], call_type=ct)
        rows = metering_db.query("SELECT call_type FROM costs ORDER BY rowid")
        types = [r["call_type"] for r in rows]
        assert types == ["agentic", "compaction", "consolidation", "embedding"]

    def test_four_element_rates_include_cache_write(self, metering_db):
        usage = MockUsage(input_tokens=1000, output_tokens=500,
                          cache_read_tokens=200, cache_write_tokens=100)
        cost = metering_db.record(
            "s", "m", "p", usage, [3.0, 15.0, 0.3, 3.75],
        )
        expected = (1000 * 3.0 + 500 * 15.0 + 200 * 0.3 + 100 * 3.75) / 1e6
        assert abs(cost - expected) < 1e-9

    def test_three_element_rates_backward_compatible(self, metering_db):
        """Old 3-element rates still work — cache_write defaults to 0."""
        usage = MockUsage(input_tokens=1000, output_tokens=500,
                          cache_read_tokens=200, cache_write_tokens=100)
        cost = metering_db.record(
            "s", "m", "p", usage, [3.0, 15.0, 0.3],
        )
        expected = (1000 * 3.0 + 500 * 15.0 + 200 * 0.3) / 1e6
        assert abs(cost - expected) < 1e-9

    def test_converter_applied_for_non_eur(self, metering_db):
        """Converter is called when currency != EUR."""
        usage = MockUsage(input_tokens=1000000, output_tokens=0)
        from unittest.mock import MagicMock
        converter = MagicMock()
        converter.convert.return_value = 2.6087
        cost = metering_db.record(
            "s", "m", "p", usage, [3.0],
            converter=converter, currency="USD",
        )
        converter.convert.assert_called_once()
        assert cost == 2.6087

    def test_converter_not_called_for_eur(self, metering_db):
        """Converter is skipped when currency is EUR."""
        usage = MockUsage(input_tokens=1000000, output_tokens=0)
        from unittest.mock import MagicMock
        converter = MagicMock()
        metering_db.record(
            "s", "m", "p", usage, [3.0],
            converter=converter, currency="EUR",
        )
        converter.convert.assert_not_called()


# ── Queries ───────────────────────────────────────────────────────

class TestQueries:
    def test_month_total(self, populated_db):
        bp = _current_billing_period()
        total_a = populated_db.month_total(bp)
        total_b = populated_db._db_b.month_total(bp)
        assert total_a > 0
        assert total_b > 0
        assert total_a > total_b  # cust_a has 3 records

    def test_month_total_default_period(self, populated_db):
        total = populated_db.month_total()
        assert total > 0

    def test_unknown_customer_returns_zero(self, tmp_path):
        db = MeteringDB(str(tmp_path / "empty.db"), agent_id="nonexistent")
        assert db.month_total() == 0.0

    def test_query_missing_db(self, tmp_path):
        db = MeteringDB(str(tmp_path / "m.db"))
        import os
        os.remove(db.path)
        assert db.query("SELECT 1") == []


# ── Records ──────────────────────────────────────────────────────

class TestGetRecords:
    def test_returns_raw_records(self, populated_db):
        bp = _current_billing_period()
        data = populated_db.get_records(bp)
        assert data["agent_id"] == "cust_a"
        assert data["billing_period"] == bp
        assert data["currency"] == "EUR"
        assert len(data["records"]) == 3

    def test_record_fields(self, populated_db):
        bp = _current_billing_period()
        rec = populated_db.get_records(bp)["records"][0]
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

    def test_filters_by_agent(self, populated_db):
        bp = _current_billing_period()
        records = populated_db.get_records(bp)["records"]
        session_ids = {r["session_id"] for r in records}
        assert session_ids <= {"sess_1", "sess_2"}

    def test_empty_db(self, metering_db):
        data = metering_db.get_records()
        assert data["records"] == []

    def test_defaults_to_current_month(self, populated_db):
        data = populated_db.get_records()
        assert data["billing_period"] == _current_billing_period()
        assert len(data["records"]) == 3


# ── Maintenance ───────────────────────────────────────────────────

class TestMaintenance:
    def test_enforce_retention(self, metering_db):
        # Insert an old record (2 years ago)
        conn = sqlite3.connect(metering_db.path)
        old_ts = int(time.time()) - 2 * 365 * 86400
        conn.execute(
            """INSERT INTO costs (timestamp, agent_id, session_id, model,
                provider, cost, currency, call_type, billing_period, success)
               VALUES (?, 'c', 's', 'm', 'p', 0.1, 'EUR', 'agentic', '2024-01', 1)""",
            (old_ts,),
        )
        # Insert a recent record
        conn.execute(
            """INSERT INTO costs (timestamp, agent_id, session_id, model,
                provider, cost, currency, call_type, billing_period, success)
               VALUES (?, 'c', 's', 'm', 'p', 0.1, 'EUR', 'agentic', ?, 1)""",
            (int(time.time()), _current_billing_period()),
        )
        conn.commit()
        conn.close()

        deleted = metering_db.enforce_retention(max_months=12)
        assert deleted == 1
        rows = metering_db.query("SELECT COUNT(*) AS cnt FROM costs")
        assert rows[0]["cnt"] == 1

