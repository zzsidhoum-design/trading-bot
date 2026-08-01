"""Unit tests for the Market Scanner agent (fake repos/cache/bus)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from qtrader.application.agents.scanner import MarketScanner
from qtrader.domain.entities import Stock
from qtrader.domain.events import BackfillCompleted, DomainEvent, ScanCompleted
from qtrader.domain.ports import Cache, EventBus, PriceRepository, StockRepository
from qtrader.domain.value_objects import Interval, PriceBar

BASE = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _series(symbol: str, start_price: Decimal, step: Decimal = Decimal("0.5")) -> list[PriceBar]:
    bars = []
    for i in range(40):
        close = start_price + step * i
        bars.append(
            PriceBar(
                symbol=symbol,
                interval=Interval.M5,
                ts=BASE - timedelta(minutes=5 * (39 - i)),
                open=close - Decimal("0.2"),
                high=close + Decimal("1.5"),
                low=close - Decimal("1.5"),
                close=close,
                volume=Decimal("100000"),
            )
        )
    bars.sort(key=lambda b: b.ts)
    return bars


class FakePriceRepository(PriceRepository):
    def __init__(self, bars_by_symbol: dict[str, list[PriceBar]]) -> None:
        self._data = bars_by_symbol

    async def upsert_bars(self, bars) -> int:
        return len(bars)

    async def latest(self, symbol, interval) -> PriceBar | None:
        series = self._data.get(symbol, [])
        return series[-1] if series else None

    async def history(self, symbol, interval, start=None, end=None, limit=500) -> list[PriceBar]:
        return self._data.get(symbol, [])


class FakeStockRepository(StockRepository):
    def __init__(self, stocks: list[Stock]) -> None:
        self._stocks = stocks

    async def upsert(self, stock) -> None:
        pass

    async def get_by_symbol(self, symbol: str, exchange: str | None = None) -> Stock | None:
        return next((s for s in self._stocks if s.symbol == symbol), None)

    async def list_active(self) -> list[Stock]:
        return self._stocks

    async def search(self, query, sector, limit, offset) -> list[Stock]:
        return self._stocks


class FakeCache(Cache):
    def __init__(self) -> None:
        self.sets: dict[str, dict[str, float]] = {}

    async def get(self, key: str) -> str | None:
        return None

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        pass

    async def delete(self, key: str) -> None:
        pass

    async def zadd(self, key: str, mapping: dict[str, float]) -> None:
        self.sets.setdefault(key, {}).update(mapping)

    async def zrevrange(self, key: str, start: int, end: int) -> list[tuple[str, float]]:
        items = sorted(self.sets.get(key, {}).items(), key=lambda kv: kv[1], reverse=True)
        return items[start : end + 1]


class FakeEventBus(EventBus):
    def __init__(self) -> None:
        self.published: list[DomainEvent] = []

    async def publish(self, event: DomainEvent) -> None:
        self.published.append(event)

    def subscribe(self, event_type, handler) -> None:
        pass

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_scan_all_ranks_and_persists() -> None:
    data = {
        # strong momentum + liquid
        "AAPL": _series("AAPL", Decimal("100")),
        # weak momentum, still liquid/volatile
        "MSFT": _series("MSFT", Decimal("100"), step=Decimal("-0.1")),
        # insufficient history → skipped
        "NFLX": _series("NFLX", Decimal("50"))[:5],
    }
    stocks = [
        Stock(symbol="AAPL", exchange="XNAS", stock_id=1),
        Stock(symbol="MSFT", exchange="XNAS", stock_id=2),
        Stock(symbol="NFLX", exchange="XNAS", stock_id=3),
    ]
    scanner = MarketScanner(
        FakePriceRepository(data),
        FakeCache(),
        FakeStockRepository(stocks),
        FakeEventBus(),
        top_k=10,
        min_dollar_volume=0.0,
        min_atr_pct=0.0,
    )
    top = await scanner.scan_all()
    assert len(top) == 2
    assert top[0].symbol == "AAPL"
    assert top[0].score > top[1].score


@pytest.mark.asyncio
async def test_scan_publishes_scan_completed() -> None:
    data = {"AAPL": _series("AAPL", Decimal("100"))}
    stocks = [Stock(symbol="AAPL", exchange="XNAS", stock_id=1)]
    bus = FakeEventBus()
    scanner = MarketScanner(
        FakePriceRepository(data),
        FakeCache(),
        FakeStockRepository(stocks),
        bus,
        top_k=5,
        min_dollar_volume=0.0,
        min_atr_pct=0.0,
    )
    await scanner.scan_all()
    assert len(bus.published) == 1
    event = bus.published[0]
    assert isinstance(event, ScanCompleted)
    assert event.candidates[0]["symbol"] == "AAPL"
    assert "score" in event.candidates[0]


@pytest.mark.asyncio
async def test_scan_filters_illiquid() -> None:
    data = {"AAPL": _series("AAPL", Decimal("100"))}
    stocks = [Stock(symbol="AAPL", exchange="XNAS", stock_id=1)]
    scanner = MarketScanner(
        FakePriceRepository(data),
        FakeCache(),
        FakeStockRepository(stocks),
        FakeEventBus(),
        top_k=10,
        min_dollar_volume=1e18,  # impossible to pass
        min_atr_pct=0.0,
    )
    top = await scanner.scan_all()
    assert top == []


@pytest.mark.asyncio
async def test_backfill_event_triggers_scan() -> None:
    data = {"AAPL": _series("AAPL", Decimal("100"))}
    stocks = [Stock(symbol="AAPL", exchange="XNAS", stock_id=1)]
    bus = FakeEventBus()
    scanner = MarketScanner(
        FakePriceRepository(data),
        FakeCache(),
        FakeStockRepository(stocks),
        bus,
        top_k=5,
        min_dollar_volume=0.0,
        min_atr_pct=0.0,
    )
    await scanner.on_event(
        BackfillCompleted(symbol="AAPL", interval=Interval.M5, start="x", end="y")
    )
    assert len(bus.published) == 1
    assert isinstance(bus.published[0], ScanCompleted)
