"""Currency conversion for cost metering.

Converts provider-native costs (USD, GBP, etc.) to EUR using a configurable
FX rate API.  Falls back to a static rate from config when the API is
unreachable.  Any JSON API that returns ``{"rate": float}`` is compatible.
"""

from __future__ import annotations

import logging
from typing import Any  # Any justified: JSON-deserialized FX API response

import httpx

log = logging.getLogger(__name__)

# Timeout for FX API calls — kept short since the API is typically local.
_FX_TIMEOUT = 5


class CurrencyConverter:
    """Live FX converter backed by a rate API.

    Each :meth:`convert` call fetches the current rate from the configured
    API.  There is no framework-level caching — the API container manages
    its own rate freshness.

    Parameters
    ----------
    api_url:
        FX rate endpoint (e.g. ``http://host:8080/v2/rate/EUR/USD``).
        Empty string disables API fetching (static-only mode).
    static_rate:
        Fallback rate used when the API is unreachable or *api_url* is
        empty.  Expressed as target-currency-per-EUR (e.g. 1.15 means
        1 EUR = 1.15 USD).  ``1.0`` means no conversion.
    """

    def __init__(self, *, api_url: str, static_rate: float) -> None:
        self._api_url = api_url
        self._static_rate = static_rate

    def convert(self, amount: float, source_currency: str) -> tuple[float, float]:
        """Convert *amount* from *source_currency* to EUR.

        Returns ``(converted_amount, rate_used)`` where *rate_used* is the
        source-currency-per-EUR divisor (e.g. 1.15 means 1 EUR = 1.15 USD).

        Returns the amount unchanged with rate ``1.0`` when *source_currency*
        is already EUR.
        """
        if source_currency == "EUR":
            return amount, 1.0

        rate = self._fetch_rate() if self._api_url else None
        if rate is None:
            rate = self._static_rate if self._static_rate > 0 else 1.0
        return amount / rate, rate

    # ── Internal ────────────────────────────────────────────────────

    def _fetch_rate(self) -> float | None:
        """Fetch the current rate from the API.

        Returns ``None`` on any failure so the caller can fall back to
        the static rate.
        """
        try:
            resp = httpx.get(self._api_url, timeout=_FX_TIMEOUT)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            rate = float(data["rate"])
            if rate <= 0:
                log.warning("FX API returned non-positive rate: %s", rate)
                return None
            return rate
        except Exception as exc:
            log.warning("FX rate fetch failed (%s): %s", self._api_url, exc)
            try:
                import metrics
                if metrics.ENABLED:
                    metrics.FX_FETCH_ERRORS_TOTAL.inc()
            except Exception:  # pragma: no cover — metrics may not be importable in tests
                pass
            return None
