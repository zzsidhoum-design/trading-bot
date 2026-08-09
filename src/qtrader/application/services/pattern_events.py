"""Pattern events — deterministic extraction of signal events from a fold.

The system uses no candlestick patterns; its tradable signals are indicator
*events* (EMA crossovers, RSI extremes, Donchian breakouts). These are the
"patterns" an audit must measure: how often they fire, how profitable the
forward window is, and their favorable vs adverse excursions.

Every event fires at the close of ``ts``; the measurable forward window starts
at the next bar's open (matching the backtest engine's next-open fills), so
there is no look-ahead.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from qtrader.domain.entities import IndicatorSnapshot
from qtrader.domain.value_objects import PriceBar


@dataclass(frozen=True, slots=True)
class PatternEvent:
    """One signal event at ``ts`` (close) for ``symbol`` under ``pattern``."""

    pattern: str
    symbol: str
    ts: datetime


def momentum_cross_events(
    series: Sequence[IndicatorSnapshot], oos: Sequence[PriceBar]
) -> list[PatternEvent]:
    """EMA9/EMA21 up/down crossovers, evaluated only on OOS bars."""
    oos_ts = {b.ts for b in oos}
    events: list[PatternEvent] = []
    prev_diff: float | None = None
    prev_ts: datetime | None = None
    for snap in series:
        ema9 = float(snap.ema_9) if snap.ema_9 else None
        ema21 = float(snap.ema_21) if snap.ema_21 else None
        diff = (ema9 - ema21) if (ema9 is not None and ema21 is not None) else None
        if snap.ts in oos_ts and diff is not None and prev_diff is not None and prev_ts is not None:
            if diff > 0 and prev_diff <= 0:
                events.append(PatternEvent("momentum_up_cross", snap.symbol, snap.ts))
            elif diff < 0 and prev_diff >= 0:
                events.append(PatternEvent("momentum_down_cross", snap.symbol, snap.ts))
        if diff is not None:
            prev_diff = diff
            prev_ts = snap.ts
    return events


def rsi_events(
    series: Sequence[IndicatorSnapshot],
    oos: Sequence[PriceBar],
    low: float = 30.0,
    high: float = 70.0,
) -> list[PatternEvent]:
    """Onsets of RSI oversold (< ``low``) and overbought (> ``high``) zones."""
    oos_ts = {b.ts for b in oos}
    events: list[PatternEvent] = []
    prev_rsi: float | None = None
    for snap in series:
        if snap.rsi is None:
            continue
        rsi = float(snap.rsi)
        if snap.ts in oos_ts:
            if rsi < low and (prev_rsi is None or prev_rsi >= low):
                events.append(PatternEvent("rsi_oversold", snap.symbol, snap.ts))
            elif rsi > high and (prev_rsi is None or prev_rsi <= high):
                events.append(PatternEvent("rsi_overbought", snap.symbol, snap.ts))
        prev_rsi = rsi
    return events


def breakout_events(
    bars: Sequence[PriceBar], oos: Sequence[PriceBar], window: int = 20
) -> list[PatternEvent]:
    """Donchian ``window``-bar high/low breakouts (close based)."""
    oos_ts = {b.ts for b in oos}
    events: list[PatternEvent] = []
    for i, bar in enumerate(bars):
        if bar.ts not in oos_ts or i < window:
            continue
        prior = bars[i - window : i]
        high = max(float(b.high) for b in prior)
        low = min(float(b.low) for b in prior)
        close = float(bar.close)
        if close > high:
            events.append(PatternEvent("breakout_up", bar.symbol, bar.ts))
        elif close < low:
            events.append(PatternEvent("breakout_down", bar.symbol, bar.ts))
    return events


def collect_events(
    bars_by_symbol: dict[str, list[PriceBar]],
    series_by_symbol: dict[str, list[IndicatorSnapshot]],
    *,
    oos: dict[str, list[PriceBar]],
    breakout_window: int = 20,
) -> list[PatternEvent]:
    """All pattern events across every symbol (momentum, RSI, breakout)."""
    events: list[PatternEvent] = []
    for symbol, bars in bars_by_symbol.items():
        series = series_by_symbol.get(symbol, [])
        symbol_oos = oos.get(symbol, [])
        events.extend(momentum_cross_events(series, symbol_oos))
        events.extend(rsi_events(series, symbol_oos))
        events.extend(breakout_events(bars, symbol_oos, breakout_window))
    return events
