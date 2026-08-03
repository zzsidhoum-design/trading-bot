"""Yahoo Finance chart API adapter.

Transport (httpx) is thin; all payload parsing lives in the pure
``parse_chart_response`` function so it is unit-testable without a network.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from qtrader.config.logging import get_logger
from qtrader.domain.ports import MarketDataProvider
from qtrader.domain.value_objects import Interval, PriceBar
from qtrader.infrastructure.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    retry_async,
)

DEFAULT_BASE_URL = "https://query1.finance.yahoo.com"

_logger = get_logger("qtrader.yahoo")


def parse_chart_response(
    payload: dict[str, Any], symbol: str, interval: Interval
) -> list[PriceBar]:
    """Parse a Yahoo ``/v8/finance/chart`` response into sorted PriceBars.

    Bars with any missing/None field or invalid OHLCV are skipped. Returns an
    empty list for errors, no data, or empty result sets.
    """
    chart = payload.get("chart") or {}
    if chart.get("error"):
        return []
    results = chart.get("result") or []
    if not results:
        return []
    data = results[0]
    timestamps: list[Any] = data.get("timestamp") or []
    indicators = data.get("indicators") or {}
    quotes = (indicators.get("quote") or [{}])[0]
    opens = quotes.get("open") or []
    highs = quotes.get("high") or []
    lows = quotes.get("low") or []
    closes = quotes.get("close") or []
    volumes = quotes.get("volume") or []

    bars: list[PriceBar] = []
    for i, ts in enumerate(timestamps):
        try:
            raw = (opens[i], highs[i], lows[i], closes[i], volumes[i])
        except IndexError:
            continue
        if any(v is None for v in raw):
            continue
        try:
            bar = PriceBar(
                symbol=symbol,
                interval=interval,
                ts=datetime.fromtimestamp(int(ts), tz=UTC),
                open=Decimal(str(raw[0])),
                high=Decimal(str(raw[1])),
                low=Decimal(str(raw[2])),
                close=Decimal(str(raw[3])),
                volume=Decimal(str(raw[4])),
            )
        except (ValueError, TypeError, OverflowError):
            continue
        bars.append(bar)
    bars.sort(key=lambda b: b.ts)
    return bars


class YahooFinanceProvider(MarketDataProvider):
    """MarketDataProvider backed by Yahoo's free chart endpoint."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 15.0,
        client: httpx.AsyncClient | None = None,
        circuit: CircuitBreaker | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds)
        self._client = client
        self._circuit = circuit or CircuitBreaker(name="yahoo")

    def _transport(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                headers={"User-Agent": "qtrader/0.1"},
            )
        return self._client

    @retry_async()
    async def _request_chart(
        self, symbol: str, params: dict[str, Any]
    ) -> httpx.Response:
        response = await self._transport().get(
            f"/v8/finance/chart/{symbol}", params=params
        )
        response.raise_for_status()
        return response

    async def _chart(
        self, symbol: str, *, interval: Interval, params: dict[str, Any]
    ) -> list[PriceBar]:
        params = {"interval": interval.value, **params}
        try:
            response = await self._circuit.call(
                lambda: self._request_chart(symbol, params)
            )
        except CircuitOpenError as exc:
            raise RuntimeError(f"yahoo circuit open for {symbol}") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"yahoo chart request failed for {symbol}: {exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"yahoo chart returned non-JSON for {symbol}") from exc
        return parse_chart_response(payload, symbol, interval)

    async def fetch_bars(
        self, symbol: str, interval: Interval, start: datetime, end: datetime
    ) -> list[PriceBar]:
        return await self._chart(
            symbol,
            interval=interval,
            params={
                "period1": int(start.timestamp()),
                "period2": int(end.timestamp()),
                "events": "history",
            },
        )

    async def fetch_quote(self, symbol: str) -> PriceBar:
        bars = await self._chart(
            symbol,
            interval=Interval.M1,
            params={"range": "1d", "includePrePost": "false"},
        )
        if not bars:
            raise RuntimeError(f"yahoo returned no quote for {symbol}")
        return bars[-1]

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


async def _demo() -> None:
    """Sanity check against the live endpoint (dev only)."""
    provider = YahooFinanceProvider()
    try:
        bars = await provider.fetch_bars(
            "AAPL", Interval.D1, datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC)
        )
        last = bars[-1] if bars else None
        _logger.info("yahoo.demo.bars", symbol="AAPL", count=len(bars), last=last)
        quote = await provider.fetch_quote("AAPL")
        _logger.info("yahoo.demo.quote", symbol="AAPL", close=quote.close)
    finally:
        await provider.close()


if __name__ == "__main__":
    asyncio.run(_demo())
