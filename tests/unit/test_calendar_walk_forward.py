"""Unit tests for the calendar-aligned, point-in-time walk-forward validator."""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from qtrader.application.services.calendar_walk_forward import (
    CALENDAR_STRATEGY_LABEL,
    CalendarWalkForwardValidator,
)
from qtrader.application.services.indicators import IndicatorEngine
from qtrader.application.services.risk_calculator import RiskCalculator, RiskPolicy
from qtrader.domain.value_objects import Interval, TradingMode
from tests.unit.fakes_phase6 import FakeBacktestRepository, FakePriceRepository, bar


def _bars_from(
    symbol: str,
    start: datetime,
    days: int,
    seed: int,
    start_price: float = 100.0,
) -> list:
    rng = random.Random(seed)
    bars: list = []
    price = float(start_price)
    for i in range(days):
        drift = 0.0015 if i % 3 == 0 else -0.0015
        price *= 1.0 + drift + rng.gauss(0, 0.018)
        ts = start + timedelta(days=i)
        bars.append(
            bar(
                symbol,
                ts,
                open=f"{price:.2f}",
                high=f"{price * 1.01:.2f}",
                low=f"{price * 0.99:.2f}",
                close=f"{price:.2f}",
                volume="1000000",
            )
        )
    return bars


def _validator(bars: dict[str, list]) -> CalendarWalkForwardValidator:
    return CalendarWalkForwardValidator(
        prices=FakePriceRepository(bars),
        risk_calculator=RiskCalculator(RiskPolicy(risk_per_trade_pct=0.01)),
        indicator_engine=IndicatorEngine(),
    )


def test_folds_are_calendar_blocks_partitioning_the_window() -> None:
    start = date(2025, 1, 1)
    end = date(2026, 12, 31)
    bars = {"AAPL": _bars_from("AAPL", datetime(2024, 1, 1, tzinfo=UTC), 800, seed=1)}
    validator = _validator(bars)
    folds = validator.make_folds(bars, start, end, n_folds=4)
    assert len(folds) == 4
    assert folds[0].fold_start.date() == start
    for prev, cur in zip(folds[:-1], folds[1:], strict=True):
        assert prev.fold_end == cur.fold_start
    assert folds[-1].fold_end.date() >= end


def test_pit_eligibility_excludes_symbols_listed_during_later_folds() -> None:
    """A symbol listed in 2026-06 must not appear in folds whose block starts
    before its listing date (no look-ahead training or trading)."""
    start = date(2025, 1, 1)
    end = date(2026, 12, 31)
    old = _bars_from("OLD", datetime(2024, 1, 1, tzinfo=UTC), 800, seed=1)
    late = _bars_from("LATE", datetime(2026, 6, 1, tzinfo=UTC), 220, seed=2)
    validator = _validator({"OLD": old, "LATE": late})
    folds = validator.make_folds({"OLD": old, "LATE": late}, start, end, n_folds=4)
    assert "LATE" not in folds[0].train and "LATE" not in folds[0].full
    assert "LATE" not in folds[0].oos
    assert "LATE" not in folds[1].full
    # Only the final block (start 2026-07-02) is after the listing date.
    assert "LATE" in folds[3].full and "LATE" in folds[3].oos


def test_probs_only_cover_oos_bars_and_hold_outside() -> None:
    start = date(2025, 1, 1)
    end = date(2026, 12, 31)
    bars = {"AAPL": _bars_from("AAPL", datetime(2024, 1, 1, tzinfo=UTC), 800, seed=1)}
    validator = _validator(bars)
    folds = validator.make_folds(bars, start, end, n_folds=4)
    model = validator.fit_model(folds[1].train)
    assert model is not None
    probs = validator.precompute_probs(model, folds[1])
    oos_ts = {b.ts for b in folds[1].oos["AAPL"]}
    assert set(probs["AAPL"].keys()) == oos_ts


def test_simulate_fold_trades_only_inside_calendar_window() -> None:
    start = date(2025, 1, 1)
    end = date(2026, 12, 31)
    bars = {"AAPL": _bars_from("AAPL", datetime(2024, 1, 1, tzinfo=UTC), 800, seed=1)}
    validator = _validator(bars)
    folds = validator.make_folds(bars, start, end, n_folds=4)
    model = validator.fit_model(folds[1].train)
    assert model is not None
    result = validator.simulate_fold(
        prices=FakePriceRepository(bars),
        backtests=FakeBacktestRepository(),
        fold=folds[1],
        model=model,
        initial_capital=Decimal("100000"),
    )
    fold_oos_start = folds[1].fold_start
    fold_oos_end = folds[1].fold_end
    for trade in result.trades:
        assert fold_oos_start <= trade.entry_time < fold_oos_end
    # The whole fold's sim window must be inside the block, so no trade can
    # happen on pre-window bars either.
    assert result.run.start >= fold_oos_start.date()
    assert result.run.strategy == CALENDAR_STRATEGY_LABEL


def test_full_pipeline_produces_aggregate_summary() -> None:
    start = date(2025, 1, 1)
    end = date(2026, 12, 31)
    bars = {"AAPL": _bars_from("AAPL", datetime(2024, 1, 1, tzinfo=UTC), 800, seed=1)}
    validator = _validator(bars)
    folds = validator.make_folds(bars, start, end, n_folds=4)
    prices = FakePriceRepository(bars)
    curve: list[tuple[datetime, Decimal]] = []
    equity = Decimal("100000")
    all_pnl: list[Decimal] = []
    for fold in folds:
        model = validator.fit_model(fold.train)
        if model is None:
            continue
        result = validator.simulate_fold(
            prices=prices,
            backtests=FakeBacktestRepository(),
            fold=fold,
            model=model,
            initial_capital=equity,
        )
        all_pnl.extend(t.pnl_pct for t in result.trades)
        curve, equity = validator.chain_curve(curve, result.equity_curve, equity)
    assert curve
    from qtrader.application.services.performance_metrics import PerformanceMetrics

    summary = PerformanceMetrics.from_series(
        strategy=CALENDAR_STRATEGY_LABEL,
        mode=TradingMode.BACKTEST,
        period_start=start,
        period_end=end,
        equity_curve=curve,
        trade_pnl_pcts=all_pnl,
        interval=Interval.D1,
    )
    assert summary.trades_count is not None
    assert summary.final_equity is not None
    assert summary.strategy == CALENDAR_STRATEGY_LABEL


def test_simulate_fold_momentum_mode_without_model() -> None:
    """model=None with no model_outputs drives the momentum heuristic, not
    the calibrated-probability path — matching the live fallback baseline."""
    start = date(2025, 1, 1)
    end = date(2026, 12, 31)
    bars = {"AAPL": _bars_from("AAPL", datetime(2024, 1, 1, tzinfo=UTC), 800, seed=1)}
    validator = _validator(bars)
    folds = validator.make_folds(bars, start, end, n_folds=4)
    result = validator.simulate_fold(
        prices=FakePriceRepository(bars),
        backtests=FakeBacktestRepository(),
        fold=folds[1],
        model=None,
        initial_capital=Decimal("100000"),
    )
    assert result.summary is not None
    assert result.run.strategy == CALENDAR_STRATEGY_LABEL


def test_chain_curve_compounds_across_folds() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    fold1 = [
        (start + timedelta(days=i), Decimal("1000") * (Decimal("1.10") if i else 1))
        for i in range(2)
    ]
    fold2 = [
        (start + timedelta(days=2 + i), Decimal("1000") * (Decimal("1.10") if i else 1))
        for i in range(2)
    ]
    curve, equity = CalendarWalkForwardValidator.chain_curve([], fold1, Decimal("1000"))
    assert equity == Decimal("1100")
    curve, equity = CalendarWalkForwardValidator.chain_curve(curve, fold2, equity)
    assert equity == Decimal("1210")
    assert curve[-1][0] == start + timedelta(days=3)
    assert curve[-1][1] == Decimal("1210")
