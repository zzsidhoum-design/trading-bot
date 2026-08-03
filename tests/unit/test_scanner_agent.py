"""Unit tests for the Market Scanner Agent (fake prices/cache/stocks/bus)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from qtrader.application.agents.scanner import MarketScanner
from qtrader.domain.entities import Stock
from qtrader.domain.events import BackfillCompleted, DomainEvent, ScanCompleted
from qtrader.domain.ports import Cache, EventBus, PriceRepository, StockRepository
from qtrader.domain.value_objects import Interval, PriceBar

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _trend(symbol: str, start: Decimal, step: Decimal, volume: str, n: int = 40) -> list[PriceBar]:
    """M5 bars rising ``step`` per bar from ``start``; high/low = ±1%."""
    bars: list[PriceBar] = []
    for i in range(n):
        close = start + step * i
        bars.append(
            PriceBar(
                symbol=symbol,
                interval=Interval.M5,
                ts=NOW - timedelta(minutes=5 * (n - i)),
                open=close,
                high=close * Decimal("1.01"),
                low=close * Decimal("0.99"),
                close=close,
                volume=Decimal(volume),
            )
        )
    return bars


class FakePriceRepository(PriceRepository):
    def __init__(self, bars_by_symbol: dict[str, list[PriceBar]]) -> None:
        self._bars = bars_by_symbol

    async def upsert_bars(self, bars) -> int:
        return len(bars)

    async def latest(self, symbol, interval) -> PriceBar | None:
        bars = self._bars.get(symbol, [])
        return bars[-1] if bars else None

    async def history(self, symbol, interval, start=None, end=None, limit=500) -> list[PriceBar]:
        return self._bars.get(symbol, [])[:limit]


class FakeStockRepository(StockRepository):
    def __init__(self, symbols: list[str]) -> None:
        self._symbols = symbols

    async def upsert(self, stock: Stock) -> Stock:
        return stock

    async def get_by_symbol(self, symbol: str, exchange: str | None = None) -> Stock | None:
        return Stock(symbol=symbol, exchange="XNAS") if symbol in self._symbols else None

    async def list_active(self) -> list[Stock]:
        return [Stock(symbol=s, exchange="XNAS") for s in self._symbols]

    async def search(self, query, sector, limit, offset) -> list[Stock]:
        return await self.list_active()


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


def _build(
    bars_by_symbol: dict[str, list[PriceBar]],
    *,
    top_k: int = 20,
) -> tuple[MarketScanner, FakeCache, FakeEventBus]:
    cache = FakeCache()
    bus = FakeEventBus()
    scanner = MarketScanner(
        prices=FakePriceRepository(bars_by_symbol),
        cache=cache,
        stocks=FakeStockRepository(list(bars_by_symbol)),
        bus=bus,
        top_k=top_k,
        lookback_bars=40,
        momentum_lookback=20,
        min_dollar_volume=500_000.0,
        min_atr_pct=0.3,
    )
    return scanner, cache, bus


@pytest.mark.asyncio
async def test_scan_all_ranks_and_publishes_top_k() -> None:
    bars = {
        # 40 bars * 1_000_000 shares * ~100-140 = well above the liquidity floor.
        "LIQA": _trend("LIQA", Decimal("100"), Decimal("1"), "1000000"),
        "MIDB": _trend("MIDB", Decimal("100"), Decimal("0.5"), "1000000"),
        "C": _trend("C", Decimal("100"), Decimal("0.25"), "1000000"),
    }
    scanner, _, bus = _build(bars, top_k=2)
    candidates = await scanner.scan_all()

    assert [c.symbol for c in candidates] == ["LIQA", "MIDB"]
    assert [c["symbol"] for c in bus.published[-1].candidates] == ["LIQA", "MIDB"]
    assert isinstance(bus.published[-1], ScanCompleted)


@pytest.mark.asyncio
async def test_scan_all_filters_illiquid_symbols() -> None:
    bars = {
        "LIQA": _trend("LIQA", Decimal("100"), Decimal("1"), "1000000"),
        # 40 bars * 10 shares * ~100 = 40k dollar volume < 500k floor.
        "DULL": _trend("DULL", Decimal("100"), Decimal("1"), "10"),
    }
    scanner, _, bus = _build(bars)
    candidates = await scanner.scan_all()

    assert [c.symbol for c in candidates] == ["LIQA"]
    assert [c["symbol"] for c in bus.published[-1].candidates] == ["LIQA"]


@pytest.mark.asyncio
async def test_scan_all_skips_symbols_with_insufficient_bars() -> None:
    bars = {
        "FULL": _trend("FULL", Decimal("100"), Decimal("1"), "1000000"),
        # only 5 bars < momentum_lookback + 1 = 21.
        "SHORT": _trend("SHORT", Decimal("100"), Decimal("1"), "1000000", n=5),
    }
    scanner, _, bus = _build(bars)
    candidates = await scanner.scan_all()

    assert [c.symbol for c in candidates] == ["FULL"]


@pytest.mark.asyncio
async def test_scan_all_persists_rankings_to_cache() -> None:
    bars = {
        "A": _trend("A", Decimal("100"), Decimal("1"), "1000000"),
        "B": _trend("B", Decimal("100"), Decimal("0.25"), "1000000"),
    }
    scanner, cache, _ = _build(bars)
    await scanner.scan_all()

    assert "scan:top:overall" in cache.sets
    assert set(cache.sets["scan:top:overall"]) == {"A", "B"}
    assert "scan:top:liquidity" in cache.sets
    assert "scan:top:volatility" in cache.sets
    assert "scan:top:momentum" in cache.sets


@pytest.mark.asyncio
async def test_scan_all_empty_universe_publishes_empty() -> None:
    scanner, cache, bus = _build({})
    candidates = await scanner.scan_all()

    assert candidates == []
    event = bus.published[-1]
    assert isinstance(event, ScanCompleted)
    assert event.candidates == []
    assert "scan:top:overall" not in cache.sets


@pytest.mark.asyncio
async def test_on_event_backfill_completed_triggers_scan() -> None:
    bars = {"A": _trend("A", Decimal("100"), Decimal("1"), "1000000")}
    scanner, _, bus = _build(bars)
    await scanner.on_event(
        BackfillCompleted(symbol="A", interval=Interval.M5, start="", end="")
    )

    assert len(bus.published) == 1
    assert isinstance(bus.published[0], ScanCompleted)
    assert [c["symbol"] for c in bus.published[0].candidates] == ["A"]
