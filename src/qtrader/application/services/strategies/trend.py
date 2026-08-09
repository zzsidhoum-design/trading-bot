"""Trend-following strategy — EMA9 > EMA21 > SMA50 alignment (long) / reverse.

The exact trend term of the production ``score_technical`` composite: a clean
bullish stack is a buy, a clean bearish stack flattens the position.
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


class TrendFollowingStrategy(Strategy):
    name = "trend_following"
    kind = "trend"

    def probs_for_symbol(
        self, inputs: StrategyInputs, symbol: str
    ) -> dict[datetime, float]:
        series = inputs.series_by_symbol.get(symbol, [])
        oos = {b.ts for b in inputs.oos.get(symbol, [])}
        probs: dict[datetime, float] = {}
        for snap in series:
            if snap.ts not in oos:
                continue
            ema9 = float(snap.ema_9) if snap.ema_9 else None
            ema21 = float(snap.ema_21) if snap.ema_21 else None
            sma50 = float(snap.sma_50) if snap.sma_50 else None
            prob = HOLD
            if ema9 is not None and ema21 is not None and sma50 is not None:
                if ema9 > ema21 > sma50:
                    prob = EVENT_BUY
                elif ema9 < ema21 < sma50:
                    prob = EVENT_SELL
            probs[snap.ts] = prob
        return probs
