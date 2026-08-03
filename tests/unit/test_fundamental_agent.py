"""Unit tests for the Fundamental Analysis Agent (fake provider/repos/bus)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from qtrader.application.agents.fundamental import FundamentalAgent
from qtrader.domain.entities import FundamentalData, Signal
from qtrader.domain.events import (
    DomainEvent,
    FundamentalSignalGenerated,
    ScanCompleted,
)
from qtrader.domain.ports import (
    EventBus,
    FundamentalProvider,
    FundamentalRepository,
    SignalRepository,
)
from qtrader.domain.value_objects import SignalType

TODAY = date(2026, 8, 1)


def _strong_fundamentals(symbol: str = "AAPL", report_date: date = TODAY) -> FundamentalData:
    return FundamentalData(
        symbol=symbol,
        period="quarter",
        report_date=report_date,
        revenue_growth=Decimal("0.5"),
        earnings_growth=Decimal("0.5"),
        gross_margin=Decimal("0.5"),
        operating_margin=Decimal("0.3"),
        net_margin=Decimal("0.2"),
        roe=Decimal("0.3"),
        roa=Decimal("0.15"),
        pe_ratio=Decimal("10"),
        debt_total=Decimal("10"),
        revenue=Decimal("1000"),
    )


class FakeFundamentalProvider(FundamentalProvider):
    def __init__(self, data: FundamentalData | None = None) -> None:
        self.data = data
        self.calls: list[str] = []

    async def fetch_fundamentals(self, symbol: str) -> FundamentalData | None:
        self.calls.append(symbol)
        return self.data


class FakeFundamentalRepository(FundamentalRepository):
    def __init__(self, stored: FundamentalData | None = None) -> None:
        self.stored = stored
        self.upserted: list[FundamentalData] = []

    async def upsert(self, data: FundamentalData) -> FundamentalData:
        self.upserted.append(data)
        self.stored = data
        return data

    async def latest(self, symbol: str) -> FundamentalData | None:
        if self.stored is None or self.stored.symbol != symbol:
            return None
        return self.stored


class FakeSignalRepository(SignalRepository):
    def __init__(self) -> None:
        self.saved: list[Signal] = []

    async def save(self, signal: Signal) -> Signal:
        self.saved.append(signal)
        return signal

    async def latest_for_symbol(self, symbol: str, agent: str | None = None) -> list[Signal]:
        return [s for s in self.saved if s.symbol == symbol and (agent is None or s.agent == agent)]


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
    provider: FundamentalProvider,
    stored: FundamentalData | None = None,
) -> tuple[FundamentalAgent, FakeSignalRepository, FakeEventBus]:
    signals = FakeSignalRepository()
    bus = FakeEventBus()
    agent = FundamentalAgent(
        provider=provider,
        fundamentals=FakeFundamentalRepository(stored),
        signals=signals,
        bus=bus,
        max_age_days=120,
    )
    return agent, signals, bus


@pytest.mark.asyncio
async def test_analyze_fetches_when_nothing_stored() -> None:
    provider = FakeFundamentalProvider(_strong_fundamentals())
    agent, signals, bus = _build(provider)

    result = await agent.analyze_symbol("AAPL")

    assert provider.calls == ["AAPL"]
    assert result is not None
    assert result.symbol == "AAPL"
    assert len(signals.saved) == 1
    signal = signals.saved[0]
    assert signal.agent == "fundamental"
    assert signal.symbol == "AAPL"
    assert signal.signal_type is SignalType.STRONG_BUY
    assert signal.score > Decimal("0.6")

    event = bus.published[-1]
    assert isinstance(event, FundamentalSignalGenerated)
    assert event.symbol == "AAPL"
    assert event.signal_type is SignalType.STRONG_BUY


@pytest.mark.asyncio
async def test_analyze_uses_fresh_stored_data_without_provider() -> None:
    provider = FakeFundamentalProvider(_strong_fundamentals())
    agent, signals, bus = _build(provider, stored=_strong_fundamentals())

    result = await agent.analyze_symbol("AAPL")

    assert provider.calls == []
    assert result is not None
    assert len(signals.saved) == 1
    assert isinstance(bus.published[-1], FundamentalSignalGenerated)


@pytest.mark.asyncio
async def test_analyze_refetches_stale_stored_data() -> None:
    stale = _strong_fundamentals(report_date=TODAY - timedelta(days=200))
    provider = FakeFundamentalProvider(_strong_fundamentals())
    agent, _, _ = _build(provider, stored=stale)

    await agent.analyze_symbol("AAPL")

    assert provider.calls == ["AAPL"]


@pytest.mark.asyncio
async def test_analyze_no_data_returns_none() -> None:
    agent, signals, bus = _build(FakeFundamentalProvider(None))

    result = await agent.analyze_symbol("AAPL")

    assert result is None
    assert signals.saved == []
    assert bus.published == []


@pytest.mark.asyncio
async def test_analyze_candidates_batch_tolerates_provider_failure() -> None:
    class SelectiveFailProvider(FakeFundamentalProvider):
        def __init__(self, fail_for: str) -> None:
            super().__init__(_strong_fundamentals())
            self.fail_for = fail_for

        async def fetch_fundamentals(self, symbol: str) -> FundamentalData | None:
            if symbol == self.fail_for:
                raise RuntimeError("provider exploded")
            return await super().fetch_fundamentals(symbol)

    provider = SelectiveFailProvider(fail_for="B")
    agent, signals, bus = _build(provider)

    # run_batch isolates the failing symbol and keeps the healthy one.
    count = await agent.analyze_candidates(["A", "B"])

    assert count == 1
    assert [s.symbol for s in signals.saved] == ["A"]
    assert [e.symbol for e in bus.published] == ["A"]


@pytest.mark.asyncio
async def test_on_event_scan_completed_analyzes_candidates() -> None:
    provider = FakeFundamentalProvider(_strong_fundamentals())
    agent, signals, bus = _build(provider)

    await agent.on_event(ScanCompleted(candidates=[{"symbol": "A"}, {"symbol": "B"}]))

    assert provider.calls == ["A", "B"]
    assert len(signals.saved) == 2
    assert len(bus.published) == 2
