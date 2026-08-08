"""Unit tests for the pure performance metrics module."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from qtrader.application.services.performance_metrics import PerformanceMetrics
from qtrader.domain.value_objects import Interval, TradingMode


def _curve(points: list[tuple[str, str]]) -> list[tuple[datetime, Decimal]]:
    start = datetime(2026, 1, 2, tzinfo=UTC)
    return [
        (start.replace(day=start.day + i), Decimal(price))
        for i, (_, price) in enumerate(points)
    ]


def test_total_return_and_max_drawdown() -> None:
    curve = _curve(
        [("d1", "100"), ("d2", "110"), ("d3", "105"), ("d4", "95"), ("d5", "120")]
    )
    summary = PerformanceMetrics.from_series(
        strategy="test",
        mode=TradingMode.BACKTEST,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 5),
        equity_curve=curve,
        trade_pnl_pcts=[Decimal("0.10"), Decimal("-0.05")],
    )
    assert summary.total_return is not None
    assert summary.total_return == Decimal("0.2")
    assert summary.max_drawdown is not None
    assert summary.max_drawdown < 0
    assert summary.trades_count == 2
    assert summary.win_rate == Decimal("0.5")
    assert summary.final_equity == Decimal("120")


def test_profit_factor_and_metrics_on_wins_only() -> None:
    curve = _curve([("d1", "100"), ("d2", "103"), ("d3", "106"), ("d4", "108"), ("d5", "110")])
    summary = PerformanceMetrics.from_series(
        strategy="test",
        mode=TradingMode.BACKTEST,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 5),
        equity_curve=curve,
        trade_pnl_pcts=[Decimal("0.05"), Decimal("0.08")],
    )
    assert summary.win_rate == Decimal("1")
    assert summary.profit_factor is not None and summary.profit_factor > 1
    assert summary.sharpe is not None
    assert summary.sortino is None  # no downside periods


def test_profit_factor_dollar_weighted_when_amounts_given() -> None:
    # A $1 win on a big position and a $100 loss on a tiny position: the
    # pnl_pct basis (0.01 / 0.10 = PF 0.1) hides that the loss is 100x the win
    # in dollars. The dollar-weighted basis reports gross $1 / gross $100.
    curve = _curve([("d1", "100"), ("d2", "100.5"), ("d3", "99")])
    summary = PerformanceMetrics.from_series(
        strategy="test",
        mode=TradingMode.BACKTEST,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 3),
        equity_curve=curve,
        trade_pnl_pcts=[Decimal("0.01"), Decimal("-0.10")],
        trade_pnl_amounts=[Decimal("1"), Decimal("-100")],
    )
    assert summary.profit_factor == Decimal("0.01")
    assert summary.win_rate == Decimal("0.5")


def test_sharpe_positive_for_steady_uptrend() -> None:
    curve = _curve(
        [(str(i), str(100 + 2 * i)) for i in range(1, 21)]
    )
    summary = PerformanceMetrics.from_series(
        strategy="test",
        mode=TradingMode.BACKTEST,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 20),
        equity_curve=curve,
        trade_pnl_pcts=[],
    )
    assert summary.sharpe is not None and summary.sharpe > 0
    assert summary.win_rate is None  # no trades


def test_empty_series_is_safe() -> None:
    summary = PerformanceMetrics.from_series(
        strategy="test",
        mode=TradingMode.BACKTEST,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 2),
        equity_curve=[],
        trade_pnl_pcts=[],
    )
    assert summary.total_return is None
    assert summary.final_equity is None
    assert summary.trades_count == 0


def test_interval_annualization_key_present() -> None:
    assert Interval.D1.value in PerformanceMetrics.ANNUALIZATION_FACTORS
    assert Interval.M5.value in PerformanceMetrics.ANNUALIZATION_FACTORS


def test_deterministic_output() -> None:
    curve = _curve([("d1", "100"), ("d2", "101"), ("d3", "99"), ("d4", "103")])
    pnl = [Decimal("0.01"), Decimal("-0.02")]
    first = PerformanceMetrics.from_series(
        strategy="t",
        mode=TradingMode.BACKTEST,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 4),
        equity_curve=curve,
        trade_pnl_pcts=pnl,
    )
    second = PerformanceMetrics.from_series(
        strategy="t",
        mode=TradingMode.BACKTEST,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 4),
        equity_curve=curve,
        trade_pnl_pcts=pnl,
    )
    assert first == second


def test_loss_only_summary() -> None:
    curve = _curve([("d1", "100"), ("d2", "90"), ("d3", "80")])
    summary = PerformanceMetrics.from_series(
        strategy="test",
        mode=TradingMode.BACKTEST,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 3),
        equity_curve=curve,
        trade_pnl_pcts=[Decimal("-0.05"), Decimal("-0.10")],
    )
    assert summary.total_return == Decimal("-0.2")
    assert summary.profit_factor == Decimal("0")  # no gross profit
