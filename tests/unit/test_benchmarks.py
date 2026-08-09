"""Unit tests for the direct benchmark curves (Buy & Hold, index, SMA200)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from qtrader.application.services.benchmarks import (
    buy_and_hold_curve,
    market_index_curve,
    sma200_curve,
)
from qtrader.domain.value_objects import Interval, PriceBar

_START = date(2026, 1, 1)


def _end(days: int) -> date:
    return _START + timedelta(days=days - 1)


def _bar(symbol: str, day: int, close: float) -> PriceBar:
    ts = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day)
    return PriceBar(
        symbol=symbol,
        interval=Interval.D1,
        ts=ts,
        open=Decimal(str(close)),
        high=Decimal(str(close)),
        low=Decimal(str(close)),
        close=Decimal(str(close)),
        volume=Decimal("1000"),
    )


def _sym(symbol: str, closes: list[float]) -> list[PriceBar]:
    return [_bar(symbol, i, c) for i, c in enumerate(closes)]


def test_buy_and_hold_single_symbol_compounds() -> None:
    bars = {"A": _sym("A", [100.0, 110.0, 121.0])}
    curve = buy_and_hold_curve(bars, _START, _end(3))
    assert [float(e) for _, e in curve] == [1.0, 1.1, 1.21]


def test_market_index_single_symbol_matches_buy_and_hold() -> None:
    bars = {"A": _sym("A", [100.0, 110.0, 121.0])}
    curve = market_index_curve(bars, _START, _end(3))
    assert [float(e) for _, e in curve] == [1.0, 1.1, 1.21]


def test_market_index_averages_daily_returns() -> None:
    # A gains +50%, B loses -50% each day -> index flat every day.
    bars = {
        "A": _sym("A", [100.0, 150.0, 225.0]),
        "B": _sym("B", [100.0, 50.0, 25.0]),
    }
    curve = market_index_curve(bars, _START, _end(3))
    assert [float(e) for _, e in curve] == [1.0, 1.0, 1.0]


def test_buy_and_hold_equal_weight_not_daily() -> None:
    # B&H (no rebalance) of A=+100%/day and B=-50%/day ends at (2+0.5)/2.
    bars = {
        "A": _sym("A", [100.0, 200.0]),
        "B": _sym("B", [100.0, 50.0]),
    }
    curve = buy_and_hold_curve(bars, _START, _end(2))
    assert abs(float(curve[-1][1]) - 1.25) < 1e-9


def test_sma_filter_goes_long_only_after_cross() -> None:
    closes = [10.0, 10.0, 10.0, 11.0, 12.0]
    bars = {"A": _sym("A", closes)}
    curve = sma200_curve(bars, _START, _end(5), sma_period=2)
    values = [float(e) for _, e in curve]
    assert len(values) == len(closes)
    assert values[0] == values[1] == values[2] == values[3] == 1.0
    assert abs(values[4] - (12.0 / 11.0)) < 1e-9


def test_sma_filter_flat_when_below_average() -> None:
    closes = [10.0, 10.0, 10.0, 9.0, 8.0]
    bars = {"A": _sym("A", closes)}
    curve = sma200_curve(bars, _START, _end(5), sma_period=2)
    values = [float(e) for _, e in curve]
    assert all(v == 1.0 for v in values)


def test_empty_inputs_return_empty_curve() -> None:
    assert buy_and_hold_curve({}, _START, _START) == []
    assert market_index_curve({}, _START, _START) == []
    assert sma200_curve({}, _START, _START) == []


def test_curves_are_causal_no_future_leak() -> None:
    # A single uptrend: SMA2 long must only earn returns strictly after the
    # close that established the long signal.
    closes = [10.0, 10.0, 10.0, 11.0, 12.0, 13.0]
    bars = {"A": _sym("A", closes)}
    curve = sma200_curve(bars, _START, _end(6), sma_period=2)
    # Day 3 (index 3) close 11 > sma2(10) -> long for day 4 only.
    assert float(curve[3][1]) == 1.0
    assert abs(float(curve[4][1]) - (12.0 / 11.0)) < 1e-9
