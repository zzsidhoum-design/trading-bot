"""Strategy registry — one objective, testable strategy per name.

ML requires a fitted model and is therefore constructed per fold via
:class:`MLProbabilityStrategy`, not pre-registered here.
"""

from __future__ import annotations

from qtrader.application.services.strategies.base import (
    BUY_THRESHOLD,
    EVENT_BUY,
    EVENT_SELL,
    HOLD,
    SELL_THRESHOLD,
    Strategy,
    StrategyInputs,
)
from qtrader.application.services.strategies.baselines import (
    AlwaysLongStrategy,
    RandomStrategy,
)
from qtrader.application.services.strategies.breakout import BreakoutStrategy
from qtrader.application.services.strategies.mean_reversion import MeanReversionStrategy
from qtrader.application.services.strategies.ml import MLProbabilityStrategy
from qtrader.application.services.strategies.momentum import MomentumStrategy
from qtrader.application.services.strategies.trend import TrendFollowingStrategy

STRATEGIES: dict[str, Strategy] = {}


def register(strategy: Strategy) -> Strategy:
    if not strategy.name:
        raise ValueError("strategy must define a name")
    if strategy.name in STRATEGIES:
        raise ValueError(f"strategy already registered: {strategy.name}")
    STRATEGIES[strategy.name] = strategy
    return strategy


def get_strategy(name: str) -> Strategy:
    try:
        return STRATEGIES[name]
    except KeyError as exc:
        raise KeyError(f"unknown strategy: {name}") from exc


def all_strategies() -> list[Strategy]:
    return list(STRATEGIES.values())


register(MomentumStrategy())
register(TrendFollowingStrategy())
register(BreakoutStrategy())
register(MeanReversionStrategy())
register(AlwaysLongStrategy())
register(RandomStrategy())

__all__ = [
    "BUY_THRESHOLD",
    "EVENT_BUY",
    "EVENT_SELL",
    "HOLD",
    "SELL_THRESHOLD",
    "STRATEGIES",
    "Strategy",
    "StrategyInputs",
    "MLProbabilityStrategy",
    "all_strategies",
    "get_strategy",
    "register",
]
