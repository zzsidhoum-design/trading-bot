"""Strategy framework — pluggable, objectively testable signal strategies.

A strategy emits a per-(symbol, timestamp) ``prob_up`` series over a fold's
out-of-sample bars. The backtest engine consumes it through its
``model_outputs`` contract (0.5 = HOLD, >= 0.52 BUY, <= 0.48 SELL), so every
strategy shares the exact same execution model — fills, costs, ATR sizing,
bracket/time exits — and results are directly comparable.

Only strategies that are objectively testable (deterministic, causal, no
model-free discretion) belong here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from qtrader.domain.entities import IndicatorSnapshot
from qtrader.domain.value_objects import PriceBar

HOLD = 0.5
BUY_THRESHOLD = 0.52
SELL_THRESHOLD = 0.48
EVENT_BUY = 0.9
EVENT_SELL = 0.1


@dataclass(frozen=True, slots=True)
class StrategyInputs:
    """Everything a strategy may read for one fold.

    ``bars_by_symbol`` and ``series_by_symbol`` hold the *full* history so
    indicators/windows have complete warm-up; ``oos`` lists the tradable bars.
    """

    bars_by_symbol: Mapping[str, Sequence[PriceBar]]
    series_by_symbol: Mapping[str, Sequence[IndicatorSnapshot]]
    oos: Mapping[str, Sequence[PriceBar]]


class Strategy(ABC):
    """Base class: one deterministic, causal probability signal per symbol."""

    name: str = ""
    kind: str = ""

    def generate_probs(self, inputs: StrategyInputs) -> dict[str, dict[datetime, float]]:
        """prob_up per OOS bar for every symbol (missing bars default HOLD)."""
        out: dict[str, dict[datetime, float]] = {}
        for symbol, bars in inputs.oos.items():
            probs = self.probs_for_symbol(inputs, symbol)
            out[symbol] = {b.ts: probs.get(b.ts, HOLD) for b in bars}
        return out

    @abstractmethod
    def probs_for_symbol(
        self, inputs: StrategyInputs, symbol: str
    ) -> dict[datetime, float]:
        """prob_up for one symbol's OOS bars (0.5 = HOLD)."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r}, kind={self.kind!r})"


__all__ = [
    "BUY_THRESHOLD",
    "EVENT_BUY",
    "EVENT_SELL",
    "HOLD",
    "SELL_THRESHOLD",
    "Strategy",
    "StrategyInputs",
]
