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

_ANNUALIZATION_FACTORS = {"1d": 252.0, "5m": 252.0 * 78.0 / 390.0, "1h": 252.0 * 6.5}


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
    ) -> PerformanceSummary:
        """Build a summary from (ts, equity) points and per-trade P/L percents.

        ``trade_pnl_amounts`` (parallel to ``trade_pnl_pcts``) switches the
        profit factor onto a single dollar-weighted basis — gross dollar profit
        over gross dollar loss — so wins/losses are weighted by size, not by
        their own return percent. Win rate is unaffected (sign is identical).
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
        wins = [t for t in trade_basis if t > 0]
        losses = [t for t in trade_basis if t < 0]
        gross_profit = sum(wins, _ZERO)
        gross_loss = abs(sum(losses, _ZERO))
        win_rate = (
            Decimal(trades_count - len(losses)) / Decimal(trades_count)
            if trades_count
            else None
        )
        profit_factor = (
            (gross_profit / gross_loss)
            if gross_loss > 0
            else (None if gross_profit == 0 else _ONE_HUNDRED)
        )

        first = equity_curve[0][1] if equity_curve else _ZERO
        last = equity_curve[-1][1] if equity_curve else _ZERO
        total_return = ((last - first) / first) if first > 0 else None
        final_equity = last if equity_curve else None

        return PerformanceSummary(
            strategy=strategy,
            mode=mode,
            period_start=period_start,
            period_end=period_end,
            total_return=_dec(total_return),
            sharpe=_dec(sharpe),
            sortino=_dec(sortino),
            max_drawdown=_dec(max_dd),
            win_rate=_dec(win_rate),
            profit_factor=_dec(profit_factor),
            trades_count=trades_count,
            final_equity=_dec(final_equity),
        )
