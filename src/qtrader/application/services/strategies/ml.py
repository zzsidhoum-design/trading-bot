"""ML probability strategy — the production logistic model as a Strategy.

Wraps a fitted ``LogisticModel`` and reproduces the fold's probability
precomputation (``CalendarWalkForwardValidator.precompute_probs``) so the
trained model is tested through the same interface as the rule-based
strategies.
"""

from __future__ import annotations

from datetime import datetime

from qtrader.application.services.feature_store import price_features_from_bars
from qtrader.application.services.prediction_model import LogisticModel
from qtrader.application.services.strategies.base import (
    HOLD,
    Strategy,
    StrategyInputs,
)


class MLProbabilityStrategy(Strategy):
    name = "ml"
    kind = "ml"

    def __init__(self, model: LogisticModel, lookback_bars: int = 60) -> None:
        self.model = model
        self.lookback_bars = lookback_bars

    def probs_for_symbol(
        self, inputs: StrategyInputs, symbol: str
    ) -> dict[datetime, float]:
        bars = list(inputs.bars_by_symbol.get(symbol, []))
        idx_of = {b.ts: i for i, b in enumerate(bars)}
        lb = self.lookback_bars
        probs: dict[datetime, float] = {}
        for bar in inputs.oos.get(symbol, []):
            i = idx_of[bar.ts]
            window = bars[max(0, i - lb + 1) : i + 1]
            if len(window) < lb:
                probs[bar.ts] = HOLD
                continue
            feats = price_features_from_bars(window)
            probs[bar.ts] = self.model.predict(feats).prob_up
        return probs
