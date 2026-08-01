"""Unit tests for the Prediction Agent."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from qtrader.application.agents.prediction import PredictionAgent
from qtrader.application.services.feature_store import FeatureStore
from qtrader.domain.entities import (
    IndicatorSnapshot,
    Prediction,
    RegisteredModel,
)
from qtrader.domain.events import DomainEvent, PredictionGenerated, ScanCompleted
from qtrader.domain.ports import (
    EventBus,
    IndicatorRepository,
    ModelRepository,
    PredictionRepository,
    PriceRepository,
    SignalRepository,
)
from qtrader.domain.value_objects import Interval, PriceBar

BASE = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _bars(count: int = 160, step: float = 0.2) -> list[PriceBar]:
    bars = []
    for i in range(count):
        close = 100.0 + step * i
        bars.append(
            PriceBar(
                symbol="AAPL",
                interval=Interval.M5,
                ts=BASE - timedelta(minutes=5 * (count - 1 - i)),
                open=Decimal(str(round(close - 0.1, 4))),
                high=Decimal(str(round(close + 0.5, 4))),
                low=Decimal(str(round(close - 0.5, 4))),
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
    async def save(self, signal) -> object:
        return signal

    async def latest_for_symbol(self, symbol, agent=None) -> list:
        return []


class FakeModelRepository(ModelRepository):
    def __init__(self, active: RegisteredModel | None = None) -> None:
        self._active = active

    async def load_active(self, name: str) -> RegisteredModel | None:
        return self._active

    async def create_version(self, name, hyperparams, training_window, offline_metrics) -> int:
        return 1

    async def promote(self, name: str, version: int) -> None:
        pass


class FakePredictionRepository(PredictionRepository):
    def __init__(self) -> None:
        self.predictions: list[Prediction] = []

    async def save(self, prediction: Prediction) -> Prediction:
        self.predictions.append(prediction)
        return prediction

    async def latest_for_symbol(self, symbol: str, limit: int = 20) -> list[Prediction]:
        return [p for p in self.predictions if p.symbol == symbol][:limit]


class FakeEventBus(EventBus):
    def __init__(self) -> None:
        self.published: list[DomainEvent] = []

    async def publish(self, event: DomainEvent) -> None:
        self.published.append(event)

    def subscribe(self, event_type, handler) -> None:
        pass

    async def close(self) -> None:
        pass


def _store() -> FeatureStore:
    return FeatureStore(
        prices=FakePriceRepository({"AAPL": _bars()}),
        indicators=FakeIndicatorRepository(),
        signals=FakeSignalRepository(),
    )


def _agent(
    bus: FakeEventBus | None = None,
) -> tuple[PredictionAgent, FakePredictionRepository, FakeEventBus]:
    predictions = FakePredictionRepository()
    bus = bus or FakeEventBus()
    agent = PredictionAgent(
        features=_store(),
        models=FakeModelRepository(),
        predictions=predictions,
        bus=bus,
    )
    return agent, predictions, bus


@pytest.mark.asyncio
async def test_predict_symbol_persists_and_publishes() -> None:
    agent, predictions, bus = _agent()
    result = await agent.predict_symbol("AAPL")
    assert result is not None
    assert len(predictions.predictions) == 1
    assert predictions.predictions[0].symbol == "AAPL"
    assert predictions.predictions[0].model_version == 0
    assert len(bus.published) == 1
    event = bus.published[0]
    assert isinstance(event, PredictionGenerated)
    assert event.symbol == "AAPL"
    assert 0.0 <= event.prob_up <= 1.0
    assert event.prob_up + event.prob_down == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_predict_symbol_uses_active_registered_model() -> None:
    registered = RegisteredModel(
        name="momentum",
        version=7,
        hyperparams={
            "feature_names": ["momentum_20"],
            "coef": [3.0],
            "intercept": 0.0,
        },
        is_active=True,
    )
    predictions = FakePredictionRepository()
    bus = FakeEventBus()
    agent = PredictionAgent(
        features=_store(),
        models=FakeModelRepository(registered),
        predictions=predictions,
        bus=bus,
    )
    result = await agent.predict_symbol("AAPL")
    assert result is not None
    assert result.model_name == "momentum"
    assert result.model_version == 7
    assert predictions.predictions[0].model_version == 7


@pytest.mark.asyncio
async def test_predict_symbol_insufficient_bars_returns_none() -> None:
    agent = PredictionAgent(
        features=FeatureStore(
            prices=FakePriceRepository({"AAPL": _bars(count=10)}),
            indicators=FakeIndicatorRepository(),
            signals=FakeSignalRepository(),
        ),
        models=FakeModelRepository(),
        predictions=FakePredictionRepository(),
        bus=FakeEventBus(),
    )
    assert await agent.predict_symbol("AAPL") is None


@pytest.mark.asyncio
async def test_scan_completed_triggers_predictions() -> None:
    agent, predictions, _ = _agent()
    await agent.on_event(
        ScanCompleted(
            candidates=[
                {"symbol": "AAPL", "score": 0.5},
                {"symbol": "MSFT", "score": 0.1},
            ]
        )
    )
    assert len(predictions.predictions) == 1
    assert predictions.predictions[0].symbol == "AAPL"
