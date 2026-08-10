"""Unit tests for the cross-sectional ValueFactorStrategy."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from qtrader.application.services.strategies.base import (
    EVENT_BUY,
    EVENT_SELL,
    HOLD,
    StrategyInputs,
)
from qtrader.application.services.strategies.value_factor import ValueFactorStrategy
from qtrader.domain.value_objects import Interval, PriceBar

_START = datetime(2026, 1, 1, tzinfo=UTC)


def _bar(symbol: str, day: int, close: float) -> PriceBar:
    c = Decimal(str(close))
    return PriceBar(
        symbol=symbol,
        interval=Interval.D1,
        ts=_START + timedelta(days=day),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=Decimal("1000"),
    )


def _fundamentals(rows: list[tuple[str, date, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [{"symbol": s, "asof": a, "book_per_share": b, "shares": sh} for s, a, b, sh in rows]
    )


def _probs(
    strat: ValueFactorStrategy, bars_by_symbol: dict[str, list[PriceBar]]
) -> dict[str, dict[datetime, float]]:
    inputs = StrategyInputs(bars_by_symbol=bars_by_symbol, series_by_symbol={}, oos=bars_by_symbol)
    return strat.generate_probs(inputs)


def test_value_factor_selects_cheapest_smallest_on_rebalance_days() -> None:
    # A is cheapest (pb 0.5) and smallest (shares 1e8); B/C/D are larger / pricier.
    symbols = ["A", "B", "C", "D"]
    closes = {"A": 10.0, "B": 100.0, "C": 50.0, "D": 20.0}
    books = {"A": 20.0, "B": 50.0, "C": 25.0, "D": 5.0}
    shares = {"A": 1e8, "B": 2e9, "C": 1e9, "D": 5e8}
    bars = {s: [_bar(s, d, closes[s]) for d in range(6)] for s in symbols}
    fund = _fundamentals(
        [(s, date(2026, 1, 1), books[s], shares[s]) for s in symbols]
    )
    strat = ValueFactorStrategy(fundamentals=fund, rebalance_bars=3, quantile=0.5)
    probs = _probs(strat, bars)

    assert set(probs) == set(symbols)
    for s in symbols:
        by_day = {
            _START + timedelta(days=d): probs[s][_START + timedelta(days=d)] for d in range(6)
        }
        # Non-rebalance days (1,2,4,5) are always HOLD.
        for d in (1, 2, 4, 5):
            assert by_day[_START + timedelta(days=d)] == HOLD
        # Rebalance days (0,3) are a strict BUY/SELL split.
        for d in (0, 3):
            assert by_day[_START + timedelta(days=d)] in (EVENT_BUY, EVENT_SELL)

    # Exactly half the universe is selected on each rebalance day.
    for d in (0, 3):
        picks = [s for s in symbols if probs[s][_START + timedelta(days=d)] == EVENT_BUY]
        assert len(picks) == 2
        # The cheapest + smallest symbol is always selected.
        assert "A" in picks


def test_value_factor_no_lookahead_via_asof() -> None:
    # A's cheap valuation is only disclosed later; earlier days must not see it.
    symbols = ["A", "B"]
    bars = {s: [_bar(s, d, 10.0) for d in range(4)] for s in symbols}
    # A: expensive (book 1.0 -> pb 10) until day 2, then cheap (book 100 -> pb 0.1).
    # B: middle (book 5 -> pb 2), same size as A.
    fund = _fundamentals(
        [
            ("A", date(2026, 1, 1), 1.0, 1e9),
            ("A", date(2026, 1, 3), 100.0, 1e9),   # disclosed on day 2
            ("B", date(2026, 1, 1), 5.0, 1e9),
        ]
    )
    strat = ValueFactorStrategy(fundamentals=fund, rebalance_bars=1, quantile=0.5)
    probs = _probs(strat, bars)
    t0, _t1, t2, t3 = (_START + timedelta(days=d) for d in range(4))
    # Day 0: A still expensive (pb 10) -> not selected; B selected.
    assert probs["A"][t0] == EVENT_SELL
    assert probs["B"][t0] == EVENT_BUY
    # From the as-of date onward A is cheapest -> selected, B drops out.
    assert probs["A"][t2] == EVENT_BUY
    assert probs["A"][t3] == EVENT_BUY
    assert probs["B"][t2] == EVENT_SELL


def test_value_factor_invalid_data_is_never_selected() -> None:
    # B has NaN book -> never ranked -> never tradeable (absent from probs).
    bars = {s: [_bar(s, d, 10.0) for d in range(4)] for s in ("A", "B")}
    fund = _fundamentals(
        [
            ("A", date(2026, 1, 1), 20.0, 1e8),
            ("B", date(2026, 1, 1), float("nan"), 1e9),
        ]
    )
    strat = ValueFactorStrategy(fundamentals=fund, rebalance_bars=1, quantile=0.5)
    probs = _probs(strat, bars)
    for d in range(4):
        ts = _START + timedelta(days=d)
        assert probs["A"][ts] in (EVENT_BUY, EVENT_SELL)
    assert "B" not in probs


def test_value_factor_rejects_bad_args() -> None:
    fund = _fundamentals([("A", date(2026, 1, 1), 20.0, 1e8)])
    with pytest.raises(ValueError):
        ValueFactorStrategy(fundamentals=fund, rebalance_bars=1, quantile=1.5)
    with pytest.raises(ValueError):
        ValueFactorStrategy(fundamentals=fund[["symbol", "asof", "book_per_share"]])
