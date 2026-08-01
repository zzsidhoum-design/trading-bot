"""Unit tests for the Technical Analysis Agent."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from qtrader.application.agents.technical import TechnicalAgent
from qtrader.application.services.indicators import IndicatorEngine
from qtrader.domain.entities import IndicatorSnapshot, Signal
from qtrader.domain.events import DomainEvent, ScanCompleted, TechnicalSignalGenerated
from qtrader.domain.ports import (
    EventBus,
    IndicatorRepository,
    PriceRepository,
    SignalRepository,
)
from qtrader.domain.value_objects import Interval, PriceBar, SignalType

BASE = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _bars(count: int = 260, drift: float = 0.3) -> list[PriceBar]:
    bars = []
    for i in range(count):
        close = 50.0 + drift * i
        bars.append(
            PriceBar(
                symbol="AAPL",
                interval=Interval.M5,
                ts=BASE - timedelta(minutes=5 * (count - 1 - i)),
                open=Decimal(str(round(close - drift, 4))),
                high=Decimal(str(round(close + 2.0, 4))),
                low=Decimal(str(round(close - 2.0, 4))),
                close=Decimal(str(round(close, 4))),
                volume=Decimal("1000000"),
            )
        )
    bars.sort(key=lambda b: b.ts)
    return bars


class FakePriceRepository(PriceRepository):
    def __init__(self, data: dict[str, list[PriceBar]]) -> None:
        self._data = data

    async def upsert_bars(self, bars) -> int:
        return len(bars)

    async def latest(self, symbol, interval) -> PriceBar | None:
        series = self._data.get(symbol, [])
        return series[-1] if series else None

    async def history(self, symbol, interval, start=None, end=None, limit=500) -> list[PriceBar]:
        return self._data.get(symbol, [])[:limit]


class FakeIndicatorRepository(IndicatorRepository):
    def __init__(self) -> None:
        self.snapshots: list[IndicatorSnapshot] = []

    async def save_snapshot(self, snapshot: IndicatorSnapshot) -> None:
        self.snapshots.append(snapshot)

    async def latest(self, symbol, interval) -> IndicatorSnapshot | None:
        return self.snapshots[-1] if self.snapshots else None


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


def _agent(
    bus: FakeEventBus | None = None,
) -> tuple[TechnicalAgent, FakeIndicatorRepository, FakeSignalRepository, FakeEventBus]:
    indicators = FakeIndicatorRepository()
    signals = FakeSignalRepository()
    bus = bus or FakeEventBus()
    agent = TechnicalAgent(
        FakePriceRepository({"AAPL": _bars()}),
        indicators,
        signals,
        bus,
        engine=IndicatorEngine(),
        interval=Interval.M5,
        history_limit=260,
        min_bars=60,
    )
    return agent, indicators, signals, bus


@pytest.mark.asyncio
async def test_analyze_symbol_persists_and_publishes() -> None:
    agent, indicators, signals, bus = _agent()
    snap = await agent.analyze_symbol("AAPL")
    assert snap is not None
    assert len(indicators.snapshots) == 1
    assert indicators.snapshots[0].symbol == "AAPL"
    assert len(signals.signals) == 1
    saved = signals.signals[0]
    assert saved.agent == "technical"
    assert saved.symbol == "AAPL"
    assert saved.signal_type in {SignalType.BUY, SignalType.STRONG_BUY, SignalType.NEUTRAL}
    assert len(bus.published) == 1
    event = bus.published[0]
    assert isinstance(event, TechnicalSignalGenerated)
    assert event.interval is Interval.M5
    assert event.sub_scores and "score" in event.sub_scores


@pytest.mark.asyncio
async def test_analyze_symbol_insufficient_bars() -> None:
    agent = TechnicalAgent(
        FakePriceRepository({"AAPL": _bars(count=10)}),
        FakeIndicatorRepository(),
        FakeSignalRepository(),
        FakeEventBus(),
        engine=IndicatorEngine(),
        history_limit=260,
        min_bars=60,
    )
    snap = await agent.analyze_symbol("AAPL")
    assert snap is None


@pytest.mark.asyncio
async def test_scan_completed_triggers_analysis() -> None:
    agent, indicators, signals, bus = _agent()
    await agent.on_event(
        ScanCompleted(
            candidates=[
                {"symbol": "AAPL", "score": 0.5},
                {"symbol": "MSFT", "score": 0.1},
            ]
        )
    )
    assert len(indicators.snapshots) == 1
    assert len(signals.signals) == 1
    assert len(bus.published) == 1
