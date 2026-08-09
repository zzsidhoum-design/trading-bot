"""Mean-reversion strategy — buy oversold RSI, exit on recovery.

Entries at ``entry_rsi`` (default 30), exits back above ``exit_rsi`` (default
55). Long-only, so a sustained downtrend is only escaped via the bracket or
time exits — the honest test of the reversion hypothesis.
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


class MeanReversionStrategy(Strategy):
    name = "mean_reversion"
    kind = "mean_reversion"

    def __init__(self, entry_rsi: float = 30.0, exit_rsi: float = 55.0) -> None:
        self.entry_rsi = entry_rsi
        self.exit_rsi = exit_rsi

    def probs_for_symbol(
        self, inputs: StrategyInputs, symbol: str
    ) -> dict[datetime, float]:
        series = inputs.series_by_symbol.get(symbol, [])
        oos = {b.ts for b in inputs.oos.get(symbol, [])}
        probs: dict[datetime, float] = {}
        for snap in series:
            if snap.ts not in oos or snap.rsi is None:
                continue
            rsi = float(snap.rsi)
            prob = HOLD
            if rsi < self.entry_rsi:
                prob = EVENT_BUY
            elif rsi > self.exit_rsi:
                prob = EVENT_SELL
            probs[snap.ts] = prob
        return probs
