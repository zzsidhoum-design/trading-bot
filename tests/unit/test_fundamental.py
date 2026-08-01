"""Unit tests for fundamental scoring and the Fundamental Agent."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from qtrader.application.agents.fundamental import FundamentalAgent
from qtrader.application.services.fundamental_score import score_fundamentals
from qtrader.domain.entities import FundamentalData, Signal
from qtrader.domain.events import DomainEvent, FundamentalSignalGenerated, ScanCompleted
from qtrader.domain.ports import (
    EventBus,
    FundamentalProvider,
    FundamentalRepository,
    SignalRepository,
)
from qtrader.domain.value_objects import SignalType
from qtrader.infrastructure.data_providers.fundamental import StubFundamentalProvider


def _data(symbol: str = "AAPL") -> FundamentalData:
    return FundamentalData(
        symbol=symbol,
        period="annual",
        report_date=date.today(),
        revenue=Decimal("1000000000"),
        eps=Decimal("5.00"),
        pe_ratio=Decimal("12"),
        debt_total=Decimal("200000000"),
        cash_flow=Decimal("100000000"),
        roe=Decimal("0.20"),
        roa=Decimal("0.10"),
        gross_margin=Decimal("0.45"),
        operating_margin=Decimal("0.25"),
        net_margin=Decimal("0.18"),
        revenue_growth=Decimal("0.25"),
        earnings_growth=Decimal("0.30"),
        price_to_book=Decimal("2.0"),
    )


class FakeFundamentalRepository(FundamentalRepository):
    def __init__(self, stored: FundamentalData | None = None) -> None:
        self.stored = stored
        self.saved: FundamentalData | None = None

    async def upsert(self, data: FundamentalData) -> FundamentalData:
        self.saved = data
        return data

    async def latest(self, symbol: str) -> FundamentalData | None:
        return self.stored


class FakeSignalRepository(SignalRepository):
    def __init__(self) -> None:
        self.signals: list[Signal] = []

    async def save(self, signal: Signal) -> Signal:
        self.signals.append(signal)
        return signal

    async def latest_for_symbol(self, symbol, agent=None) -> list[Signal]:
        return [
            s
            for s in self.signals
            if s.symbol == symbol and (agent is None or s.agent == agent)
        ]


class FakeEventBus(EventBus):
    def __init__(self) -> None:
        self.published: list[DomainEvent] = []

    async def publish(self, event: DomainEvent) -> None:
        self.published.append(event)

    def subscribe(self, event_type, handler) -> None:
        pass

    async def close(self) -> None:
        pass


class FakeFundamentalProvider(FundamentalProvider):
    def __init__(self, data: FundamentalData | None = None) -> None:
        self._data = data

    async def fetch_fundamentals(self, symbol: str) -> FundamentalData | None:
        return self._data


@pytest.mark.asyncio
async def test_score_fundamentals_strong() -> None:
    score, signal_type, sub = score_fundamentals(_data())
    assert 0.0 <= score <= 1.0
    assert signal_type in {SignalType.BUY, SignalType.STRONG_BUY, SignalType.NEUTRAL}
    assert set(sub) >= {"growth", "margins", "profitability", "leverage", "valuation", "score"}


@pytest.mark.asyncio
async def test_score_fundamentals_weak() -> None:
    weak = _data()
    weak = FundamentalData(
        symbol=weak.symbol,
        period=weak.period,
        report_date=weak.report_date,
        revenue=Decimal("100000000"),
        eps=Decimal("-1.00"),
        pe_ratio=Decimal("0"),
        debt_total=Decimal("900000000"),
        cash_flow=Decimal("-5000000"),
        roe=Decimal("-0.10"),
        roa=Decimal("-0.05"),
        gross_margin=Decimal("0.05"),
        operating_margin=Decimal("-0.05"),
        net_margin=Decimal("-0.10"),
        revenue_growth=Decimal("-0.30"),
        earnings_growth=Decimal("-0.40"),
        price_to_book=Decimal("0.1"),
    )
    score, signal_type, _ = score_fundamentals(weak)
    assert -1.0 <= score <= 0.0
    assert signal_type in {SignalType.SELL, SignalType.STRONG_SELL, SignalType.NEUTRAL}


@pytest.mark.asyncio
async def test_stub_provider_is_deterministic() -> None:
    provider = StubFundamentalProvider()
    first = await provider.fetch_fundamentals("TST1")
    second = await provider.fetch_fundamentals("TST1")
    assert first is not None and second is not None
    assert first.revenue == second.revenue
    assert first.eps == second.eps


@pytest.mark.asyncio
async def test_analyze_symbol_saves_and_publishes() -> None:
    provider = FakeFundamentalProvider(_data())
    fundamentals = FakeFundamentalRepository()
    signals = FakeSignalRepository()
    bus = FakeEventBus()
    agent = FundamentalAgent(provider, fundamentals, signals, bus)
    result = await agent.analyze_symbol("AAPL")
    assert result is not None
    assert fundamentals.saved is not None
    assert len(signals.signals) == 1
    assert signals.signals[0].agent == "fundamental"
    assert len(bus.published) == 1
    event = bus.published[0]
    assert isinstance(event, FundamentalSignalGenerated)
    assert event.rating in {st.value for st in SignalType}


@pytest.mark.asyncio
async def test_stored_fundamentals_reused() -> None:
    stored = _data()
    provider = FakeFundamentalProvider(None)  # would fail if fetched
    fundamentals = FakeFundamentalRepository(stored=stored)
    signals = FakeSignalRepository()
    bus = FakeEventBus()
    agent = FundamentalAgent(provider, fundamentals, signals, bus, max_age_days=120)
    result = await agent.analyze_symbol("AAPL")
    assert result is not None
    assert len(signals.signals) == 1


@pytest.mark.asyncio
async def test_scan_completed_triggers_fundamental() -> None:
    provider = FakeFundamentalProvider(_data())
    fundamentals = FakeFundamentalRepository()
    signals = FakeSignalRepository()
    bus = FakeEventBus()
    agent = FundamentalAgent(provider, fundamentals, signals, bus)
    await agent.on_event(ScanCompleted(candidates=[{"symbol": "AAPL", "score": 0.9}]))
    assert len(signals.signals) == 1
    assert len(bus.published) == 1
