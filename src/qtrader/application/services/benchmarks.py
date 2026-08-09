"""Benchmark curves — Buy & Hold, equal-weight market index, SMA200 filter.

Computed directly from bar closes (no execution engine), so they answer
"did the strategy beat a naive alternative over the same calendar window?"
Each function returns a normalized equity curve (starting at 1.0 on the first
in-window day) that ``PerformanceMetrics.from_series`` can summarize.

All functions are causal: any indicator (SMA) uses only closes up to and
including the day being marked.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time
from decimal import Decimal

from qtrader.domain.value_objects import PriceBar


def _bars_within(
    bars_by_symbol: Mapping[str, Sequence[PriceBar]], start: date, end: date
) -> dict[str, list[PriceBar]]:
    start_dt = datetime.combine(start, time.min, tzinfo=UTC)
    end_dt = datetime.combine(end, time.max, tzinfo=UTC)
    out: dict[str, list[PriceBar]] = {}
    for symbol, bars in bars_by_symbol.items():
        selected = [b for b in bars if start_dt <= b.ts <= end_dt]
        if selected:
            out[symbol] = sorted(selected, key=lambda b: b.ts)
    return out


def _day_map(bars: Sequence[PriceBar]) -> dict[date, PriceBar]:
    return {b.ts.date(): b for b in bars}


def _all_days(symbol_days: Sequence[set[date]]) -> list[date]:
    merged: set[date] = set()
    for days in symbol_days:
        merged |= days
    return sorted(merged)


def _ts(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


def _dec(value: float) -> Decimal:
    return Decimal(f"{value:.12f}")


def buy_and_hold_curve(
    bars_by_symbol: Mapping[str, Sequence[PriceBar]],
    start: date,
    end: date,
) -> list[tuple[datetime, Decimal]]:
    """Equal notional in every symbol at its first in-window close, held flat.

    Each symbol contributes ``close(t) / close(first)`` to the 1/N average, so
    the curve is the buy-and-hold return of an equal-weighted portfolio.
    """
    window = _bars_within(bars_by_symbol, start, end)
    symbols = list(window)
    if not symbols:
        return []
    first_close = {s: float(window[s][0].close) for s in symbols}
    days = _all_days([set(_day_map(window[s]).keys()) for s in symbols])
    curve: list[tuple[datetime, Decimal]] = []
    equity = 1.0
    for day in days:
        parts: list[float] = []
        for s in symbols:
            b = _day_map(window[s]).get(day)
            if b is not None and first_close[s] > 0:
                parts.append(float(b.close) / first_close[s])
        if parts:
            equity = sum(parts) / len(parts)
        curve.append((_ts(day), _dec(equity)))
    return curve


def market_index_curve(
    bars_by_symbol: Mapping[str, Sequence[PriceBar]],
    start: date,
    end: date,
) -> list[tuple[datetime, Decimal]]:
    """Equal-weight, daily-rebalanced index (mean of daily returns, compounded)."""
    window = _bars_within(bars_by_symbol, start, end)
    symbols = list(window)
    if not symbols:
        return []
    day_bars = {s: _day_map(window[s]) for s in symbols}
    days = _all_days([set(day_bars[s].keys()) for s in symbols])
    prev_close: dict[str, float] = {}
    curve: list[tuple[datetime, Decimal]] = []
    equity = 1.0
    for day in days:
        rets: list[float] = []
        for s in symbols:
            b = day_bars[s].get(day)
            if b is not None:
                if s in prev_close and prev_close[s] > 0:
                    rets.append(float(b.close) / prev_close[s] - 1.0)
                prev_close[s] = float(b.close)
        if rets:
            equity *= 1.0 + sum(rets) / len(rets)
        curve.append((_ts(day), _dec(equity)))
    return curve


def sma200_curve(
    bars_by_symbol: Mapping[str, Sequence[PriceBar]],
    start: date,
    end: date,
    sma_period: int = 200,
) -> list[tuple[datetime, Decimal]]:
    """Long every symbol whose close is above its SMA(period), else flat.

    Equal weight among the longs. The decision uses only closes up to and
    including day ``t``; the position is held during day ``t+1``, so there is
    no same-day look-ahead.
    """
    full: dict[str, list[PriceBar]] = {}
    for symbol, bars in bars_by_symbol.items():
        if bars:
            full[symbol] = sorted(bars, key=lambda b: b.ts)

    history: dict[str, tuple[list[date], dict[date, float]]] = {}
    for s, bars in full.items():
        dates = [b.ts.date() for b in bars]
        closes = {d: float(b.close) for d, b in zip(dates, bars, strict=True)}
        history[s] = (dates, closes)

    window = _bars_within(full, start, end)
    symbols = [s for s in window if s in history]
    if not symbols:
        return []
    day_bars = {s: _day_map(window[s]) for s in symbols}
    days = _all_days([set(day_bars[s].keys()) for s in symbols])
    if not days:
        return []

    longs: set[str] = set()
    equity = 1.0
    curve: list[tuple[datetime, Decimal]] = []
    for i, day in enumerate(days):
        if i > 0:
            prev_day = days[i - 1]
            rets: list[float] = []
            for s in longs:
                prev = day_bars[s].get(prev_day)
                curr = day_bars[s].get(day)
                if prev is None or curr is None or float(prev.close) <= 0:
                    continue
                rets.append(float(curr.close) / float(prev.close) - 1.0)
            if rets:
                equity *= 1.0 + sum(rets) / len(rets)

        next_longs: set[str] = set()
        for s in symbols:
            b = day_bars[s].get(day)
            if b is None:
                continue
            dates, closes = history[s]
            hist = [(d, closes[d]) for d in dates if d <= day]
            if len(hist) < sma_period:
                continue
            sma = sum(c for _, c in hist[-sma_period:]) / sma_period
            if float(b.close) > sma:
                next_longs.add(s)
        longs = next_longs
        curve.append((_ts(day), _dec(equity)))
    return curve
