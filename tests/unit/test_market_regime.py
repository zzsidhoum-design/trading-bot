"""Unit tests for the market regime engine (pure, causal classification)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from qtrader.application.services.market_regime import (
    MarketRegime,
    MarketRegimeEngine,
    VolatilityRegime,
)


def _daily(start: datetime, n: int) -> list[datetime]:
    return [start + timedelta(days=i) for i in range(n)]


def _up_trend(n: int = 500, daily: float = 0.001) -> list[tuple[datetime, float]]:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    price = 100.0
    points: list[tuple[datetime, float]] = []
    for ts in _daily(start, n):
        price *= 1.0 + daily
        points.append((ts, price))
    return points


def _down_trend(n: int = 500, daily: float = -0.001) -> list[tuple[datetime, float]]:
    return _up_trend(n, daily)


def _sideways(n: int = 500, base: float = 100.0) -> list[tuple[datetime, float]]:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    return [
        (ts, base + math.sin(i / 8.0) * 2.0)
        for i, ts in enumerate(_daily(start, n))
    ]


def test_bull_trend_classified_bull() -> None:
    labels = MarketRegimeEngine().classify(_up_trend())
    assert labels[-1].market == MarketRegime.BULL
    assert all(label.market is not None for label in labels[300:])
    assert any(label.market == MarketRegime.BULL for label in labels[300:])


def test_bear_trend_classified_bear() -> None:
    labels = MarketRegimeEngine().classify(_down_trend())
    assert labels[-1].market == MarketRegime.BEAR
    assert any(label.market == MarketRegime.BEAR for label in labels[300:])


def test_sideways_classified_sideways() -> None:
    labels = MarketRegimeEngine().classify(_sideways())
    assert labels[-1].market == MarketRegime.SIDEWAYS


def test_cold_start_rows_are_none() -> None:
    labels = MarketRegimeEngine().classify(_up_trend(n=50))
    assert all(label.market is None and label.volatility is None for label in labels)


def test_calming_volatility_moves_low_to_high() -> None:
    # Long flat calm, then a 40-day volatility burst, then flat calm again:
    # the burst lands in EXTREME, the trailing calm falls back to LOW.
    start = datetime(2020, 1, 1, tzinfo=UTC)
    ts = _daily(start, 940)
    calm1 = [(t, 100.0) for t in ts[:600]]
    burst = [(t, 100.0 + math.sin(i) * 6.0) for i, t in enumerate(ts[600:640])]
    calm2 = [(t, 100.0) for t in ts[640:]]
    series = calm1 + burst + calm2
    labels = MarketRegimeEngine().classify(series)
    valid = [label for label in labels if label.volatility is not None]
    assert any(label.volatility == VolatilityRegime.EXTREME for label in valid)
    assert valid[-1].volatility == VolatilityRegime.LOW


def test_empty_input_is_safe() -> None:
    assert MarketRegimeEngine().classify([]) == []


def test_regime_label_property() -> None:
    from qtrader.application.services.market_regime import RegimeLabel

    label = RegimeLabel(
        ts=datetime(2020, 1, 1, tzinfo=UTC),
        market=MarketRegime.BULL,
        volatility=VolatilityRegime.HIGH,
    )
    assert label.label == "bull-high"
    assert RegimeLabel(
        ts=datetime(2020, 1, 1, tzinfo=UTC), market=None, volatility=None
    ).label == "n/a"


def test_no_lookahead_classify_is_causal() -> None:
    engine = MarketRegimeEngine()
    start = datetime(2020, 1, 1, tzinfo=UTC)
    # One continuous series: up for 300 days, then down for 300 days.
    points = [
        (ts, 100.0 * (1.001 ** i) if i < 300 else 100.0 * (1.001**300) * (0.999 ** (i - 300)))
        for i, ts in enumerate(_daily(start, 600))
    ]
    labels = engine.classify(points)
    ts_to_label = {label.ts.timestamp(): label for label in labels}
    prefix_labels = engine.classify(points[:320])
    for label in prefix_labels:
        full = ts_to_label[label.ts.timestamp()]
        assert full.market == label.market
        assert full.volatility == label.volatility


@pytest.mark.parametrize("engine", [MarketRegimeEngine()])
def test_classify_returns_one_label_per_input(engine: MarketRegimeEngine) -> None:
    points = _up_trend(400)
    assert len(engine.classify(points)) == len(points)
