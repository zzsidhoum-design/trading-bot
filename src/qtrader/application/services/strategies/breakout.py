"""Breakout strategy — Donchian N-bar high/low breakout on closes.

A close above the highest high of the prior ``window`` bars is a long
breakout; a close below the prior window's lowest low flattens. Classical and
fully deterministic; no parameters beyond the window.
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


class BreakoutStrategy(Strategy):
    name = "breakout"
    kind = "breakout"

    def __init__(self, window: int = 20) -> None:
        self.window = window

    def probs_for_symbol(
        self, inputs: StrategyInputs, symbol: str
    ) -> dict[datetime, float]:
        bars = inputs.bars_by_symbol.get(symbol, [])
        oos = {b.ts for b in inputs.oos.get(symbol, [])}
        window = self.window
        probs: dict[datetime, float] = {}
        for i, bar in enumerate(bars):
            if bar.ts not in oos or i < window:
                continue
            prior = bars[i - window : i]
            high = max(float(b.high) for b in prior)
            low = min(float(b.low) for b in prior)
            close = float(bar.close)
            prob = HOLD
            if close > high:
                prob = EVENT_BUY
            elif close < low:
                prob = EVENT_SELL
            probs[bar.ts] = prob
        return probs
