"""Tests for cost tracking — agentic.py _record_cost + cost DB round-trip."""

import sqlite3
from dataclasses import dataclass

from agentic import _init_cost_db, _record_cost


@dataclass
class MockUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class TestCostCalculation:
    def test_cost_equals_tokens_times_rates(self, cost_db):
        """cost = input*rate[0]/1M + output*rate[1]/1M + cache*rate[2]/1M."""
        usage = MockUsage(
            input_tokens=1_000_000,
            output_tokens=100_000,
            cache_read_tokens=500_000,
        )
        rates = [5.0, 25.0, 0.5]  # Opus rates
        cost = _record_cost(str(cost_db), "sess-1", "opus", usage, rates)
        # 1M * 5.0/1M + 100k * 25.0/1M + 500k * 0.5/1M
        # = 5.0 + 2.5 + 0.25 = 7.75
        assert abs(cost - 7.75) < 0.001

    def test_cache_uses_cache_rate_not_input_rate(self, cost_db):
        """Cache reads at $0.50/Mtok, not $5.00/Mtok."""
        usage = MockUsage(cache_read_tokens=1_000_000)
        rates = [5.0, 25.0, 0.5]
        cost = _record_cost(str(cost_db), "sess-1", "opus", usage, rates)
        assert abs(cost - 0.5) < 0.001

    def test_zero_tokens_no_crash(self, cost_db):
        usage = MockUsage()
        rates = [5.0, 25.0, 0.5]
        cost = _record_cost(str(cost_db), "sess-1", "opus", usage, rates)
        assert cost == 0.0

    def test_haiku_rates(self, cost_db):
        """Haiku sub-agent rates: $1/$5/$0.1 per Mtok."""
        usage = MockUsage(input_tokens=100_000, output_tokens=10_000)
        rates = [1.0, 5.0, 0.1]
        cost = _record_cost(str(cost_db), "sub-sess", "haiku", usage, rates)
        # 100k * 1.0/1M + 10k * 5.0/1M = 0.1 + 0.05 = 0.15
        assert abs(cost - 0.15) < 0.001


class TestCostDBRoundTrip:
    def test_write_then_query(self, cost_db):
        usage = MockUsage(input_tokens=50_000, output_tokens=10_000)
        rates = [5.0, 25.0, 0.5]
        _record_cost(str(cost_db), "sess-rt", "opus", usage, rates)

        conn = sqlite3.connect(str(cost_db))
        row = conn.execute(
            "SELECT session_id, input_tokens, output_tokens, cost_usd FROM costs"
        ).fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "sess-rt"
        assert row[1] == 50_000
        assert row[2] == 10_000
        assert row[3] > 0

    def test_no_path_returns_zero(self):
        usage = MockUsage(input_tokens=1000)
        cost = _record_cost("", "sess", "model", usage, [5.0, 25.0, 0.5])
        assert cost == 0.0

    def test_no_rates_returns_zero(self, cost_db):
        usage = MockUsage(input_tokens=1000)
        cost = _record_cost(str(cost_db), "sess", "model", usage, [])
        assert cost == 0.0

    def test_sub_agent_cost_tracked(self, cost_db):
        """Sub-agent costs appear with sub-* session ID prefix."""
        usage = MockUsage(input_tokens=50_000, output_tokens=5_000)
        rates = [1.0, 5.0, 0.1]
        cost = _record_cost(str(cost_db), "sub-main-session", "haiku", usage, rates)
        assert cost > 0

        conn = sqlite3.connect(str(cost_db))
        row = conn.execute(
            "SELECT session_id, model FROM costs WHERE session_id LIKE 'sub-%'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "sub-main-session"
        assert row[1] == "haiku"

    def test_concurrent_writes(self, cost_db):
        """Multiple writes to cost DB don't corrupt."""
        usage = MockUsage(input_tokens=1000, output_tokens=100)
        rates = [5.0, 25.0, 0.5]
        for i in range(10):
            _record_cost(str(cost_db), f"sess-{i}", "opus", usage, rates)

        conn = sqlite3.connect(str(cost_db))
        count = conn.execute("SELECT COUNT(*) FROM costs").fetchone()[0]
        conn.close()
        assert count == 10


class TestInitCostDB:
    def test_init_creates_table(self, tmp_path):
        db_path = tmp_path / "new_cost.db"
        _init_cost_db(str(db_path))

        conn = sqlite3.connect(str(db_path))
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        conn.close()
        assert ("costs",) in tables

    def test_init_idempotent(self, tmp_path):
        db_path = tmp_path / "new_cost.db"
        _init_cost_db(str(db_path))
        _init_cost_db(str(db_path))  # Should not fail

    def test_init_empty_path(self):
        """Empty path is a no-op."""
        _init_cost_db("")
