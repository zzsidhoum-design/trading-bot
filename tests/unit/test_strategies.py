"""Unit tests for the pluggable strategy framework."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from qtrader.application.services.strategies import (
    STRATEGIES,
    all_strategies,
    get_strategy,
    register,
)
from qtrader.application.services.strategies.base import (
    EVENT_BUY,
    EVENT_SELL,
    HOLD,
    Strategy,
    StrategyInputs,
)
from qtrader.application.services.strategies.baselines import (
    AlwaysLongStrategy,
    RandomStrategy,
)
from qtrader.application.services.strategies.breakout import BreakoutStrategy
from qtrader.application.services.strategies.mean_reversion import MeanReversionStrategy
from qtrader.application.services.strategies.momentum import MomentumStrategy
from qtrader.application.services.strategies.trend import TrendFollowingStrategy
from qtrader.domain.entities import IndicatorSnapshot
from qtrader.domain.value_objects import Interval, PriceBar

_START = datetime(2026, 1, 1, tzinfo=UTC)


def _bar(
    symbol: str, day: int, close: float, high: float | None = None, low: float | None = None
) -> PriceBar:
    ts = _START + timedelta(days=day)
    c = Decimal(str(close))
    return PriceBar(
        symbol=symbol,
        interval=Interval.D1,
        ts=ts,
        open=c,
        high=Decimal(str(high if high is not None else close)),
        low=Decimal(str(low if low is not None else close)),
        close=c,
        volume=Decimal("1000"),
    )


def _snap(symbol: str, day: int, *, ema9: float | None = None, ema21: float | None = None,
          sma50: float | None = None, rsi: float | None = None) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        symbol=symbol,
        interval=Interval.D1,
        ts=_START + timedelta(days=day),
        rsi=Decimal(str(rsi)) if rsi is not None else None,
        ema_9=Decimal(str(ema9)) if ema9 is not None else None,
        ema_21=Decimal(str(ema21)) if ema21 is not None else None,
        sma_50=Decimal(str(sma50)) if sma50 is not None else None,
    )


def _inputs(
    symbol: str,
    bars: list[PriceBar],
    series: list[IndicatorSnapshot],
    oos_days: set[int],
) -> StrategyInputs:
    oos = {b.ts: b for b in bars}
    oos_ts = {_START + timedelta(days=d) for d in oos_days}
    return StrategyInputs(
        bars_by_symbol={symbol: bars},
        series_by_symbol={symbol: series},
        oos={symbol: [oos[b.ts] for b in bars if b.ts in oos_ts]},
    )


def test_registry_has_objective_strategies() -> None:
    names = {s.name for s in all_strategies()}
    assert names == {
        "momentum",
        "trend_following",
        "breakout",
        "mean_reversion",
        "always_long",
        "random",
    }
    assert get_strategy("momentum").name == "momentum"
    with pytest.raises(KeyError):
        get_strategy("nope")


def test_register_duplicate_raises() -> None:
    class _Dup(Strategy):
        name = "dupe_test"
        kind = "x"

        def probs_for_symbol(self, inputs: StrategyInputs, symbol: str) -> dict:
            return {}

    register(_Dup())
    with pytest.raises(ValueError):
        register(_Dup())
    STRATEGIES.pop("dupe_test")


def test_momentum_buy_on_up_cross_only_in_oos() -> None:
    # ema9 crosses above ema21 between days 2 and 3; only days 3..4 are OOS.
    series = [
        _snap("A", 0, ema9=10.0, ema21=11.0),
        _snap("A", 1, ema9=10.5, ema21=10.6),
        _snap("A", 2, ema9=10.6, ema21=10.6),
        _snap("A", 3, ema9=10.9, ema21=10.6),
        _snap("A", 4, ema9=11.2, ema21=10.7),
    ]
    bars = [_bar("A", i, 10.0) for i in range(5)]
    inputs = _inputs("A", bars, series, oos_days={3, 4})
    probs = MomentumStrategy().generate_probs(inputs)["A"]
    assert probs[bars[3].ts] == EVENT_BUY
    assert probs[bars[4].ts] == HOLD


def test_momentum_sell_on_down_cross_and_rsi_override() -> None:
    series = [
        _snap("A", 0, ema9=11.0, ema21=10.0),
        _snap("A", 1, ema9=11.0, ema21=10.0, rsi=80.0),
        _snap("A", 2, ema9=10.4, ema21=10.5),
    ]
    bars = [_bar("A", i, 10.0) for i in range(3)]
    inputs = _inputs("A", bars, series, oos_days={1, 2})
    probs = MomentumStrategy().generate_probs(inputs)["A"]
    assert probs[bars[1].ts] == EVENT_SELL  # RSI>70 override
    assert probs[bars[2].ts] == EVENT_SELL  # down cross


def test_trend_following_alignment() -> None:
    series = [
        _snap("A", 0, ema9=12.0, ema21=11.0, sma50=10.0),
        _snap("A", 1, ema9=9.0, ema21=9.5, sma50=10.0),
        _snap("A", 2, ema9=10.2, ema21=10.5, sma50=10.4),
    ]
    bars = [_bar("A", i, 10.0) for i in range(3)]
    inputs = _inputs("A", bars, series, oos_days={0, 1, 2})
    probs = TrendFollowingStrategy().generate_probs(inputs)["A"]
    assert probs[bars[0].ts] == EVENT_BUY
    assert probs[bars[1].ts] == EVENT_SELL
    assert probs[bars[2].ts] == HOLD


def test_breakout_donchian() -> None:
    closes = [10.0, 11.0, 12.0, 11.5, 11.0, 13.0, 9.5]
    bars = [
        _bar("A", i, c, high=c + 0.1, low=c - 0.1)
        for i, c in enumerate(closes)
    ]
    # window=3: day5 close 13.0 > max(12.0,11.5,11.0)=12.0 -> buy;
    # day6 close 9.5 < min(11.5,11.0,13.0)=11.0 -> sell.
    inputs = _inputs("A", bars, [], oos_days={5, 6})
    probs = BreakoutStrategy(window=3).generate_probs(inputs)["A"]
    assert probs[bars[5].ts] == EVENT_BUY
    assert probs[bars[6].ts] == EVENT_SELL


def test_mean_reversion_entry_and_exit() -> None:
    series = [
        _snap("A", 0, rsi=25.0),
        _snap("A", 1, rsi=40.0),
        _snap("A", 2, rsi=60.0),
    ]
    bars = [_bar("A", i, 10.0) for i in range(3)]
    inputs = _inputs("A", bars, series, oos_days={0, 1, 2})
    probs = MeanReversionStrategy().generate_probs(inputs)["A"]
    assert probs[bars[0].ts] == EVENT_BUY
    assert probs[bars[1].ts] == HOLD
    assert probs[bars[2].ts] == EVENT_SELL


def test_always_long_buys_every_oos_bar() -> None:
    bars = [_bar("A", i, 10.0) for i in range(3)]
    inputs = _inputs("A", bars, [], oos_days={0, 1, 2})
    probs = AlwaysLongStrategy().generate_probs(inputs)["A"]
    assert all(p == 0.55 for p in probs.values())


def test_random_strategy_is_deterministic() -> None:
    bars = [_bar("A", i, 10.0) for i in range(200)]
    inputs = _inputs("A", bars, [], oos_days=set(range(200)))
    first = RandomStrategy().generate_probs(inputs)["A"]
    second = RandomStrategy().generate_probs(inputs)["A"]
    assert first == second
    assert any(p == EVENT_BUY for p in first.values())
    assert any(p == HOLD for p in first.values())


def test_missing_symbol_yields_empty_probs() -> None:
    bars = [_bar("A", i, 10.0) for i in range(3)]
    inputs = _inputs("A", bars, [], oos_days={0})
    inputs_no_oos = StrategyInputs(
        bars_by_symbol=inputs.bars_by_symbol,
        series_by_symbol=inputs.series_by_symbol,
        oos={},
    )
    assert MomentumStrategy().generate_probs(inputs_no_oos) == {}
