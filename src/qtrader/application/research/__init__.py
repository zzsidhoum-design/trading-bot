"""Research integration layer — clean internal interfaces + adapters.

This package formalizes the research-facing contracts
(:mod:`qtrader.application.research.interfaces`) and the concrete adapters
over existing services (:mod:`qtrader.application.research.adapters`).
Third-party trading/TA libraries stay behind these adapters; agents and
research scripts depend only on the Protocols.
"""

from __future__ import annotations

from qtrader.application.research.adapters import (
    BacktestAdapter,
    IndicatorAdapter,
    MarketDataAdapter,
    PortfolioAdapter,
    PredictionAdapter,
    StrategyAdapter,
)
from qtrader.application.research.interfaces import (
    BacktestInterface,
    IndicatorInterface,
    MarketDataInterface,
    PortfolioInterface,
    PredictionInterface,
    StrategyInterface,
)

__all__ = [
    "BacktestAdapter",
    "BacktestInterface",
    "IndicatorAdapter",
    "IndicatorInterface",
    "MarketDataAdapter",
    "MarketDataInterface",
    "PortfolioAdapter",
    "PortfolioInterface",
    "PredictionAdapter",
    "PredictionInterface",
    "StrategyAdapter",
    "StrategyInterface",
]
