"""Unit tests for the Chief Agent."""

from __future__ import annotations

from decimal import Decimal

import pytest

from qtrader.application.agents.chief import ChiefAgent
from qtrader.application.services.decision_strategy import EnsembleDecisionStrategy
from qtrader.domain.entities import DecisionRecord, Prediction, Signal
from qtrader.domain.events import DecisionMade, DomainEvent, ScanCompleted
from qtrader.domain.ports import (
    DecisionRepository,
    EventBus,
    PredictionRepository,
    SignalRepository,
)
from qtrader.domain.value_objects import Decision, SignalType


class FakeSignalRepository(SignalRepository):
    def __init__(self, signals: list[Signal]) -> None:
        self._signals = signals

    async def save(self, signal: Signal) -> Signal:
        self._signals.append(signal)
        return signal

    async def latest_for_symbol(self, symbol, agent=None) -> list[Signal]:
        return [
            s
            for s in self._signals
            if s.symbol == symbol and (agent is None or s.agent == agent)
        ]


class FakePredictionRepository(PredictionRepository):
    def __init__(self, predictions: list[Prediction]) -> None:
        self._predictions = predictions

    async def save(self, prediction: Prediction) -> Prediction:
        self._predictions.append(prediction)
        return prediction

    async def latest_for_symbol(self, symbol: str, limit: int = 20) -> list[Prediction]:
        return [p for p in self._predictions if p.symbol == symbol][:limit]


class FakeDecisionRepository(DecisionRepository):
    def __init__(self) -> None:
        self.records: list[DecisionRecord] = []

    async def save(self, record: DecisionRecord) -> DecisionRecord:
        self.records.append(record)
        return record

    async def latest_for_symbol(self, symbol: str, limit: int = 20) -> list[DecisionRecord]:
        return [r for r in self.records if r.symbol == symbol][:limit]


class FakeEventBus(EventBus):
    def __init__(self) -> None:
        self.published: list[DomainEvent] = []

    async def publish(self, event: DomainEvent) -> None:
        self.published.append(event)

    def subscribe(self, event_type, handler) -> None:
        pass

    async def close(self) -> None:
        pass


def _buy_signals() -> list[Signal]:
    return [
        Signal(
            symbol="AAPL",
            agent="technical",
            signal_type=SignalType.STRONG_BUY,
            score=Decimal("0.8"),
        ),
        Signal(
            symbol="AAPL",
            agent="news",
            signal_type=SignalType.BUY,
            score=Decimal("0.6"),
        ),
        Signal(
            symbol="AAPL",
            agent="fundamental",
            signal_type=SignalType.BUY,
            score=Decimal("0.5"),
        ),
    ]


def _prediction(prob_up: Decimal = Decimal("0.7")) -> Prediction:
    return Prediction(
        symbol="AAPL",
        model_name="momentum",
        model_version=1,
        horizon="intraday",
        prob_up=prob_up,
        prob_down=Decimal("0.3"),
        confidence=Decimal("0.8"),
        expected_return=Decimal("0.001"),
    )


@pytest.mark.asyncio
async def test_decide_symbol_buy_persists_and_publishes() -> None:
    bus = FakeEventBus()
    agent = ChiefAgent(
        signals=FakeSignalRepository(_buy_signals()),
        predictions=FakePredictionRepository([_prediction()]),
        decisions=FakeDecisionRepository(),
        bus=bus,
        strategy=EnsembleDecisionStrategy(),
    )
    record = await agent.decide_symbol("AAPL")
    assert record is not None
    assert record.decision is Decision.BUY
    assert len(bus.published) == 1
    event = bus.published[0]
    assert isinstance(event, DecisionMade)
    assert event.decision_uuid == record.decision_uuid
    assert event.symbol == "AAPL"


@pytest.mark.asyncio
async def test_decide_symbol_hold_persists_but_does_not_publish() -> None:
    decisions = FakeDecisionRepository()
    bus = FakeEventBus()
    agent = ChiefAgent(
        signals=FakeSignalRepository([]),
        predictions=FakePredictionRepository([]),
        decisions=decisions,
        bus=bus,
        strategy=EnsembleDecisionStrategy(),
    )
    record = await agent.decide_symbol("AAPL")
    assert record is None
    assert decisions.records == []
    assert bus.published == []


@pytest.mark.asyncio
async def test_decide_symbol_conflict_holds() -> None:
    signals = [
        Signal(
            symbol="AAPL",
            agent="technical",
            signal_type=SignalType.STRONG_BUY,
            score=Decimal("0.9"),
        ),
        Signal(
            symbol="AAPL",
            agent="news",
            signal_type=SignalType.STRONG_SELL,
            score=Decimal("-0.9"),
        ),
    ]
    decisions = FakeDecisionRepository()
    agent = ChiefAgent(
        signals=FakeSignalRepository(signals),
        predictions=FakePredictionRepository([]),
        decisions=decisions,
        bus=FakeEventBus(),
        strategy=EnsembleDecisionStrategy(),
    )
    record = await agent.decide_symbol("AAPL")
    assert record is not None
    assert record.decision is Decision.HOLD
    assert "conflicting" in record.rationale
    assert decisions.records[0].decision is Decision.HOLD


@pytest.mark.asyncio
async def test_scan_completed_triggers_decisions() -> None:
    decisions = FakeDecisionRepository()
    agent = ChiefAgent(
        signals=FakeSignalRepository(_buy_signals()),
        predictions=FakePredictionRepository([_prediction()]),
        decisions=decisions,
        bus=FakeEventBus(),
        strategy=EnsembleDecisionStrategy(),
    )
    await agent.on_event(
        ScanCompleted(
            candidates=[
                {"symbol": "AAPL", "score": 0.5},
                {"symbol": "MSFT", "score": 0.1},
            ]
        )
    )
    assert len(decisions.records) == 1
    assert decisions.records[0].symbol == "AAPL"
