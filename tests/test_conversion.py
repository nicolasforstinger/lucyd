"""Tests for conversion.py — FX currency conversion module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from conversion import CurrencyConverter


class TestPassthrough:
    """EUR-to-EUR conversion returns amount unchanged."""

    def test_same_currency_returns_unchanged(self) -> None:
        converter = CurrencyConverter(api_url="", static_rate=1.15)
        amount, rate = converter.convert(10.0, "EUR")
        assert amount == 10.0
        assert rate == 1.0

    def test_same_currency_skips_api_call(self) -> None:
        converter = CurrencyConverter(api_url="http://fx/rate", static_rate=1.0)
        with patch("conversion.httpx") as mock_httpx:
            amount, rate = converter.convert(5.0, "EUR")
        assert amount == 5.0
        assert rate == 1.0
        mock_httpx.get.assert_not_called()


class TestStaticRate:
    """Static-only mode (empty api_url)."""

    def test_converts_with_static_rate(self) -> None:
        converter = CurrencyConverter(api_url="", static_rate=1.15)
        # 10 USD / 1.15 = 8.6957 EUR
        amount, rate = converter.convert(10.0, "USD")
        assert round(amount, 4) == round(10.0 / 1.15, 4)
        assert rate == 1.15

    def test_no_api_call_when_url_empty(self) -> None:
        converter = CurrencyConverter(api_url="", static_rate=1.15)
        with patch("conversion.httpx") as mock_httpx:
            converter.convert(10.0, "USD")
        mock_httpx.get.assert_not_called()

    def test_static_rate_zero_falls_back_to_one(self) -> None:
        converter = CurrencyConverter(api_url="", static_rate=0.0)
        amount, rate = converter.convert(10.0, "USD")
        assert amount == 10.0
        assert rate == 1.0


class TestApiFetch:
    """API-backed conversion."""

    def _mock_response(self, rate: float) -> MagicMock:
        resp = MagicMock()
        resp.json.return_value = {"rate": rate}
        resp.raise_for_status = MagicMock()
        return resp

    def test_converts_using_api_rate(self) -> None:
        converter = CurrencyConverter(api_url="http://fx/rate", static_rate=1.0)
        with patch("conversion.httpx") as mock_httpx:
            mock_httpx.get.return_value = self._mock_response(1.1552)
            amount, rate = converter.convert(13.43, "USD")
        assert round(amount, 4) == round(13.43 / 1.1552, 4)
        assert rate == 1.1552

    def test_api_failure_falls_back_to_static_rate(self) -> None:
        converter = CurrencyConverter(api_url="http://fx/rate", static_rate=1.15)
        with patch("conversion.httpx") as mock_httpx:
            mock_httpx.get.side_effect = Exception("connection refused")
            amount, rate = converter.convert(10.0, "USD")
        assert round(amount, 4) == round(10.0 / 1.15, 4)
        assert rate == 1.15

    def test_api_non_positive_rate_falls_back(self) -> None:
        converter = CurrencyConverter(api_url="http://fx/rate", static_rate=1.15)
        with patch("conversion.httpx") as mock_httpx:
            mock_httpx.get.return_value = self._mock_response(0.0)
            amount, rate = converter.convert(10.0, "USD")
        assert round(amount, 4) == round(10.0 / 1.15, 4)
        assert rate == 1.15

    def test_any_currency_supported(self) -> None:
        """Not just USD — GBP, JPY, etc. all work."""
        converter = CurrencyConverter(api_url="http://fx/rate", static_rate=1.0)
        with patch("conversion.httpx") as mock_httpx:
            mock_httpx.get.return_value = self._mock_response(0.856)
            amount, rate = converter.convert(100.0, "GBP")
        assert round(amount, 4) == round(100.0 / 0.856, 4)
        assert rate == 0.856


class TestPrometheusIntegration:
    """FX fetch failures increment the Prometheus counter."""

    def test_failure_increments_counter(self) -> None:
        converter = CurrencyConverter(api_url="http://fx/rate", static_rate=1.15)
        mock_counter = MagicMock()
        with patch("conversion.httpx") as mock_httpx, \
             patch("conversion.metrics", MagicMock(ENABLED=True, FX_FETCH_ERRORS_TOTAL=mock_counter)):
            mock_httpx.get.side_effect = Exception("timeout")
            amount, rate = converter.convert(10.0, "USD")
        mock_counter.inc.assert_called_once()
        assert rate == 1.15  # fell back to static
