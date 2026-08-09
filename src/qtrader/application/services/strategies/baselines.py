"""Baseline strategies — no-edge controls, run through the same engine.

- ``AlwaysLongStrategy``: constant 0.55 probability (buy on the first OOS bar,
  hold to the end) — the market direction baseline.
- ``RandomStrategy``: seeded random sparse entries/exits — the no-information
  baseline.
"""

from __future__ import annotations

import random
from datetime import datetime

from qtrader.application.services.strategies.base import (
    EVENT_BUY,
    EVENT_SELL,
    HOLD,
    Strategy,
    StrategyInputs,
)


class AlwaysLongStrategy(Strategy):
    name = "always_long"
    kind = "baseline"

    def probs_for_symbol(
        self, inputs: StrategyInputs, symbol: str
    ) -> dict[datetime, float]:
        return {b.ts: 0.55 for b in inputs.oos.get(symbol, [])}


class RandomStrategy(Strategy):
    name = "random"
    kind = "baseline"

    def __init__(
        self, seed: int = 2026, buy_prob: float = 0.05, sell_prob: float = 0.02
    ) -> None:
        self.seed = seed
        self.buy_prob = buy_prob
        self.sell_prob = sell_prob

    def probs_for_symbol(
        self, inputs: StrategyInputs, symbol: str
    ) -> dict[datetime, float]:
        rng = random.Random(f"{self.seed}:{symbol}")
        probs: dict[datetime, float] = {}
        for bar in inputs.oos.get(symbol, []):
            roll = rng.random()
            if roll < self.buy_prob:
                prob = EVENT_BUY
            elif roll < self.buy_prob + self.sell_prob:
                prob = EVENT_SELL
            else:
                prob = HOLD
            probs[bar.ts] = prob
        return probs
