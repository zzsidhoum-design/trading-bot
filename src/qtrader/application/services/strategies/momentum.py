"""Momentum strategy — EMA9/21 crossover with an RSI>70 sell override.

Mirrors the production momentum fallback in ``backtest._SignalEngine``, but
precomputed over the series and emitted only for out-of-sample bars.
"""

from __future__ import annotations

from datetime import datetime

from qtrader.application.services.strategies.base import (
    EVENT_BUY,
    EVENT_SELL,
    HOLD,
    Strategy,
    StrategyInputs,
)


class MomentumStrategy(Strategy):
    name = "momentum"
    kind = "momentum"

    def probs_for_symbol(
        self, inputs: StrategyInputs, symbol: str
    ) -> dict[datetime, float]:
        series = inputs.series_by_symbol.get(symbol, [])
        oos = {b.ts for b in inputs.oos.get(symbol, [])}
        probs: dict[datetime, float] = {}
        prev_diff: float | None = None
        for snap in series:
            ema9 = float(snap.ema_9) if snap.ema_9 else None
            ema21 = float(snap.ema_21) if snap.ema_21 else None
            diff = (ema9 - ema21) if (ema9 is not None and ema21 is not None) else None
            if snap.ts in oos:
                prob = HOLD
                if diff is not None and prev_diff is not None:
                    if diff > 0 and prev_diff <= 0:
                        prob = EVENT_BUY
                    elif diff < 0 and prev_diff >= 0:
                        prob = EVENT_SELL
                if prob == HOLD and snap.rsi is not None and float(snap.rsi) > 70:
                    prob = EVENT_SELL
                probs[snap.ts] = prob
            if diff is not None:
                prev_diff = diff
        return probs
