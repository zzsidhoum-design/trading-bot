"""Performance metrics -- pure, unit-testable statistics for backtests.

Nothing here performs I/O. Callers feed plain sequences of equity points and
closed trades (P/L percentages) and receive a ``PerformanceSummary``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal

from qtrader.domain.entities import PerformanceSummary
from qtrader.domain.value_objects import Interval, TradingMode

_ZERO = Decimal("0")
_ONE_HUNDRED = Decimal("100")

_ANNUALIZATION_FACTORS = {
    "1d": 252.0,
    "5m": 252.0 * 78.0 / 390.0,
    "1h": 252.0 * 6.5,
    "1m": 252.0 * 390.0,
    "15m": 252.0 * 26.0,
    "30m": 252.0 * 13.0,
    "4h": 252.0 * 1.625,
}


def _pct(values: Sequence[Decimal]) -> list[float]:
    """Period-over-period returns as floats (negative convention preserved)."""
    out: list[float] = []
    prev: Decimal | None = None
    for value in values:
        if prev is not None and prev > 0:
            out.append(float((value - prev) / prev))
        prev = value
    return out


def _dec(value: Decimal | float | None, digits: int = 6) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        value = Decimal(str(value))
    return value.quantize(Decimal(f"1e-{digits}"))


class PerformanceMetrics:
    """Statistical summaries computed over an equity curve and closed trades."""

    ANNUALIZATION_FACTORS = _ANNUALIZATION_FACTORS

    @staticmethod
    def from_series(
        *,
        strategy: str,
        mode: TradingMode,
        period_start: date,
        period_end: date,
        equity_curve: Sequence[tuple[datetime, Decimal]],
        trade_pnl_pcts: Sequence[Decimal],
        interval: Interval = Interval.D1,
        risk_free_rate: float = 0.0,
        trade_pnl_amounts: Sequence[Decimal] | None = None,
        trade_notionals: Sequence[Decimal] | None = None,
        trade_costs: Sequence[Decimal] | None = None,
    ) -> PerformanceSummary:
        """Build a summary from (ts, equity) points and per-trade P/L percents.

        ``trade_pnl_amounts`` (parallel to ``trade_pnl_pcts``) switches the
        profit factor, expectancy and average win/loss onto a single
        dollar-weighted basis — gross dollar profit over gross dollar loss —
        so wins/losses are weighted by size, not by their own return percent.
        Win rate is unaffected (sign is identical).

        ``trade_notionals`` (entry+exit dollar volumes) and ``trade_costs``
        (dollars of commission/slippage per trade) enable ``turnover`` and
        ``total_costs``. A zero-P/L trade is neither a win nor a loss.
        """
        pcts = _pct([eq for _, eq in equity_curve])
        n = len(pcts)

        mean = sum(pcts) / n if n else 0.0
        variance = sum((p - mean) ** 2 for p in pcts) / n if n else 0.0
        std = math.sqrt(variance) if variance else 0.0
        annual = _ANNUALIZATION_FACTORS.get(interval.value, 252.0)
        sharpe = (
            (mean - risk_free_rate / annual) / std * math.sqrt(annual)
            if std > 0
            else None
        )

        downside = [p for p in pcts if p < 0]
        down_var = sum(p * p for p in downside) / len(downside) if downside else 0.0
        down_std = math.sqrt(down_var) if down_var else 0.0
        sortino = (
            (mean - risk_free_rate / annual) / down_std * math.sqrt(annual)
            if down_std > 0
            else None
        )

        peak = equity_curve[0][1] if equity_curve else _ZERO
        max_dd = _ZERO
        for _, eq in equity_curve:
            if eq > peak:
                peak = eq
            if peak > 0:
                dd = (eq - peak) / peak
                if dd < max_dd:
                    max_dd = dd

        trades_count = len(trade_pnl_pcts)
        trade_basis = trade_pnl_amounts if trade_pnl_amounts is not None else trade_pnl_pcts
        wins = [t for t in trade_pnl_pcts if t > 0]
        win_rate = (
            Decimal(len(wins)) / Decimal(trades_count)
            if trades_count
            else None
        )
        gross_profit = sum((t for t in trade_basis if t > 0), _ZERO)
        gross_loss = abs(sum((t for t in trade_basis if t < 0), _ZERO))
        profit_factor = (
            (gross_profit / gross_loss)
            if gross_loss > 0
            else (None if gross_profit == 0 else _ONE_HUNDRED)
        )

        win_values = [t for t in trade_basis if t > 0]
        loss_values = [t for t in trade_basis if t < 0]
        avg_win = sum(win_values, _ZERO) / len(win_values) if win_values else None
        avg_loss = sum(loss_values, _ZERO) / len(loss_values) if loss_values else None
        expectancy = sum(trade_basis, _ZERO) / trades_count if trades_count else None

        first = equity_curve[0][1] if equity_curve else _ZERO
        last = equity_curve[-1][1] if equity_curve else _ZERO
        total_return = ((last - first) / first) if first > 0 else None
        final_equity = last if equity_curve else None

        cagr = None
        if first > 0 and last > 0 and n > 0:
            annual_factor = _ANNUALIZATION_FACTORS.get(interval.value, 252.0)
            exponent = Decimal(str(annual_factor)) / Decimal(n)
            cagr = (last / first) ** exponent - Decimal(1)

        turnover = None
        if trade_notionals is not None and equity_curve:
            avg_equity = sum((eq for _, eq in equity_curve), _ZERO) / len(equity_curve)
            turnover = (
                sum((abs(t) for t in trade_notionals), _ZERO) / avg_equity
                if avg_equity > 0
                else None
            )
        total_costs = sum(trade_costs, _ZERO) if trade_costs is not None else None

        return PerformanceSummary(
            strategy=strategy,
            mode=mode,
            period_start=period_start,
            period_end=period_end,
            total_return=_dec(total_return),
            cagr=_dec(cagr),
            sharpe=_dec(sharpe),
            sortino=_dec(sortino),
            max_drawdown=_dec(max_dd),
            win_rate=_dec(win_rate),
            profit_factor=_dec(profit_factor),
            expectancy=_dec(expectancy),
            avg_win=_dec(avg_win),
            avg_loss=_dec(avg_loss),
            turnover=_dec(turnover),
            total_costs=_dec(total_costs),
            trades_count=trades_count,
            final_equity=_dec(final_equity),
        )

    @staticmethod
    def expectancy_formula(
        win_rate: Decimal | None,
        loss_rate: Decimal | None,
        avg_win: Decimal | None,
        avg_loss: Decimal | None,
    ) -> Decimal | None:
        """Expected value per trade: ``EV = p(win)*avg_win + p(loss)*avg_loss``.

        ``avg_loss`` is signed (negative); zero-P/L trades enter through the
        explicitly passed ``loss_rate`` (``win_rate + loss_rate <= 1``), so
        they never masquerade as losses.
        """
        if (
            win_rate is None
            or loss_rate is None
            or avg_win is None
            or avg_loss is None
        ):
            return None
        return win_rate * avg_win + loss_rate * avg_loss
