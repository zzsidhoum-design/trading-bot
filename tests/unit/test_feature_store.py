"""Unit tests for the feature store & pure price-feature function."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from qtrader.application.services.feature_store import (
    FEATURE_NAMES,
    FeatureStore,
    feature_hash,
    price_features_from_bars,
)
from qtrader.domain.entities import IndicatorSnapshot, Signal
from qtrader.domain.events import DomainEvent
from qtrader.domain.ports import (
    EventBus,
    IndicatorRepository,
    PriceRepository,
    SignalRepository,
)
from qtrader.domain.value_objects import Interval, PriceBar, SignalType

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


def test_price_features_are_dimensionless_and_deterministic() -> None:
    features = price_features_from_bars(_bars())
    assert set(features) == set(FEATURE_NAMES)
    assert all(isinstance(v, float) for v in features.values())
    again = price_features_from_bars(_bars())
    assert features == again


def test_price_features_trending_up_has_positive_momentum() -> None:
    features = price_features_from_bars(_bars(step=0.2))
    assert features["momentum_20"] > 0
    assert features["ret_5"] > 0


def test_feature_hash_is_deterministic_and_sensitive() -> None:
    f1 = price_features_from_bars(_bars())
    h1 = feature_hash(f1)
    h2 = feature_hash(dict(f1))
    assert h1 == h2
    changed = dict(f1)
    changed["momentum_20"] = changed["momentum_20"] + 0.001
    assert feature_hash(changed) != h1


def test_price_features_too_few_bars_returns_zeros() -> None:
    features = price_features_from_bars(_bars(count=1))
    assert features["momentum_20"] == 0.0


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
    def __init__(self, snapshot: IndicatorSnapshot | None = None) -> None:
        self._snapshot = snapshot

    async def save_snapshot(self, snapshot: IndicatorSnapshot) -> None:
        self._snapshot = snapshot

    async def latest(self, symbol, interval) -> IndicatorSnapshot | None:
        return self._snapshot


class FakeSignalRepository(SignalRepository):
    def __init__(self, signals: list[Signal] | None = None) -> None:
        self.signals = signals or []

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
    async def publish(self, event: DomainEvent) -> None:
        pass

    def subscribe(self, event_type, handler) -> None:
        pass

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_build_features_enriches_with_indicator_and_signals() -> None:
    snapshot = IndicatorSnapshot(
        symbol="AAPL",
        interval=Interval.M5,
        ts=BASE,
        rsi=Decimal("62"),
        ema_9=Decimal("101"),
        ema_21=Decimal("100"),
        macd_hist=Decimal("0.5"),
        adx=Decimal("28"),
        stoch_k=Decimal("70"),
        boll_upper=Decimal("103"),
        boll_middle=Decimal("101"),
        boll_lower=Decimal("99"),
    )
    signals = [
        Signal(
            symbol="AAPL",
            agent="technical",
            signal_type=SignalType.BUY,
            score=Decimal("0.6"),
        ),
        Signal(
            symbol="AAPL",
            agent="news",
            signal_type=SignalType.NEUTRAL,
            score=Decimal("0.1"),
        ),
    ]
    store = FeatureStore(
        prices=FakePriceRepository({"AAPL": _bars()}),
        indicators=FakeIndicatorRepository(snapshot),
        signals=FakeSignalRepository(signals),
    )
    vector = await store.build_features("AAPL", Interval.M5)

    assert vector is not None
    assert vector.feature_hash
    assert vector.features["rsi"] == 62.0
    assert vector.features["ema_ratio"] == pytest.approx(1.01)
    assert vector.features["macd_hist"] == 0.5
    assert vector.features["signal_technical"] == pytest.approx(0.6)
    assert vector.features["signal_news"] == pytest.approx(0.1)
    assert "signal_fundamental" not in vector.features


@pytest.mark.asyncio
async def test_build_features_insufficient_bars_returns_none() -> None:
    store = FeatureStore(
        prices=FakePriceRepository({"AAPL": _bars(count=10)}),
        indicators=FakeIndicatorRepository(),
        signals=FakeSignalRepository(),
    )
    assert await store.build_features("AAPL", Interval.M5, min_bars=30) is None
