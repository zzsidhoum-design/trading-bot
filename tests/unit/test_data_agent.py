"""Unit tests for the Data Agent (fake provider/repo/cache/bus)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from qtrader.application.agents.base import AgentContext
from qtrader.application.agents.data import DataAgent
from qtrader.application.services.bar_cleaner import BarCleaner
from qtrader.domain.events import BackfillCompleted, DomainEvent, PriceUpdated
from qtrader.domain.ports import Cache, EventBus, MarketDataProvider, PriceRepository
from qtrader.domain.value_objects import Interval, PriceBar

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


class FakeMarketDataProvider(MarketDataProvider):
    def __init__(self, bars: list[PriceBar] | None = None, quote: PriceBar | None = None) -> None:
        self.bars = bars or []
        self.quote = quote
        self.fetch_bars_calls = 0
        self.fetch_quote_calls = 0

    async def fetch_bars(self, symbol, interval, start, end) -> list[PriceBar]:
        self.fetch_bars_calls += 1
        return self.bars

    async def fetch_quote(self, symbol: str) -> PriceBar:
        self.fetch_quote_calls += 1
        if self.quote is None:
            raise RuntimeError("no quote")
        return self.quote


class FakePriceRepository(PriceRepository):
    def __init__(self) -> None:
        self.stored: list[PriceBar] = []

    async def upsert_bars(self, bars) -> int:
        self.stored.extend(bars)
        return len(bars)

    async def latest(self, symbol, interval) -> PriceBar | None:
        return self.stored[-1] if self.stored else None

    async def history(self, symbol, interval, start=None, end=None, limit=500) -> list[PriceBar]:
        return self.stored


class FakeCache(Cache):
    def __init__(self) -> None:
        self.values: dict[str, tuple[str, int | None]] = {}
        self.sets: dict[str, dict[str, float]] = {}

    async def get(self, key: str) -> str | None:
        entry = self.values.get(key)
        return entry[0] if entry else None

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        self.values[key] = (value, ttl_seconds)

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)

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


def _bar(
    ts: datetime,
    *,
    symbol: str = "AAPL",
    interval: Interval = Interval.M5,
    close: str = "103",
    volume: str = "1000",
) -> PriceBar:
    return PriceBar(
        symbol=symbol,
        interval=interval,
        ts=ts,
        open=Decimal("100"),
        high=Decimal("105"),
        low=Decimal("99"),
        close=Decimal(close),
        volume=Decimal(volume),
    )


def _build(
    provider: MarketDataProvider,
) -> tuple[DataAgent, FakePriceRepository, FakeCache, FakeEventBus]:
    prices = FakePriceRepository()
    cache = FakeCache()
    bus = FakeEventBus()
    agent = DataAgent(provider, prices, cache, bus, BarCleaner(), quote_cache_ttl_seconds=300)
    return agent, prices, cache, bus


@pytest.mark.asyncio
async def test_backfill_persists_and_publishes() -> None:
    bars = [_bar(NOW - timedelta(minutes=10)), _bar(NOW - timedelta(minutes=5))]
    agent, prices, _, bus = _build(FakeMarketDataProvider(bars))
    inserted = await agent.backfill("AAPL", Interval.M5, NOW - timedelta(days=1), NOW)
    assert inserted == 2
    assert len(prices.stored) == 2
    events = bus.published
    assert len(events) == 1
    assert isinstance(events[0], BackfillCompleted)
    assert events[0].symbol == "AAPL"


@pytest.mark.asyncio
async def test_backfill_drops_invalid_bars() -> None:
    bad = _bar(NOW - timedelta(minutes=5), close="0")
    good = _bar(NOW - timedelta(minutes=10))
    agent, prices, _, bus = _build(FakeMarketDataProvider([bad, good]))
    inserted = await agent.backfill("AAPL", Interval.M5, NOW - timedelta(days=1), NOW)
    assert inserted == 1
    assert prices.stored == [good]
    assert len(bus.published) == 1
    assert isinstance(bus.published[0], BackfillCompleted)


@pytest.mark.asyncio
async def test_refresh_publishes_price_updated_and_caches_quote() -> None:
    recent = datetime.now(UTC) - timedelta(minutes=1)
    quote = _bar(recent, close="104")
    agent, prices, cache, bus = _build(FakeMarketDataProvider(quote=quote))
    bar = await agent.refresh("AAPL")
    assert bar is not None
    assert bar.close == Decimal("104")
    assert len(prices.stored) == 1

    event = bus.published[-1]
    assert isinstance(event, PriceUpdated)
    assert event.symbol == "AAPL"
    assert event.close == "104"

    cached = json.loads((await cache.get("quote:AAPL")) or "")
    assert cached["close"] == "104"


@pytest.mark.asyncio
async def test_refresh_no_quote_returns_none() -> None:
    agent, prices, cache, bus = _build(FakeMarketDataProvider(quote=None))
    bar = await agent.refresh("AAPL")
    assert bar is None
    assert prices.stored == []
    assert bus.published == []


@pytest.mark.asyncio
async def test_run_with_range_backfills() -> None:
    bars = [_bar(NOW - timedelta(minutes=5))]
    agent, prices, _, bus = _build(FakeMarketDataProvider(bars))
    await agent.run(
        AgentContext(symbol="AAPL", interval=Interval.M5, start=NOW - timedelta(days=1), end=NOW)
    )
    assert len(prices.stored) == 1
