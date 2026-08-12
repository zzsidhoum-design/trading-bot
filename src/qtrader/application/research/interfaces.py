"""Research-facing integration contracts (Protocols).

The six interfaces below are the stable seam between strategies/research
scripts and the concrete services plus third-party libraries. pandas/numpy
stay *behind* the ``IndicatorEngine`` (via :class:`IndicatorAdapter`); no
agent or research module imports a third-party trading library directly.
Adapters in ``adapters.py`` implement these over the existing services, so
they can be swapped in DI without touching consumers.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from qtrader.application.services.backtest import BacktestParams, BacktestResult
from qtrader.application.services.prediction_model import ModelOutput
from qtrader.application.services.strategies.base import StrategyInputs
from qtrader.domain.entities import IndicatorSnapshot, Portfolio, Position
from qtrader.domain.value_objects import Interval, Money, PriceBar


@runtime_checkable
class MarketDataInterface(Protocol):
    """Read-side market data (bars/quotes) for research and agents."""

    async def history(
        self,
        symbol: str,
        interval: Interval,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> list[PriceBar]: ...

    async def latest(self, symbol: str, interval: Interval) -> PriceBar | None: ...


@runtime_checkable
class StrategyInterface(Protocol):
    """A deterministic, causal probability signal strategy."""

    @property
    def name(self) -> str: ...

    def generate_probs(
        self, inputs: StrategyInputs
    ) -> dict[str, dict[datetime, float]]: ...


@runtime_checkable
class BacktestInterface(Protocol):
    """Replay bars through the execution model and return a BacktestResult."""

    async def run(
        self,
        name: str,
        symbols: list[str],
        start: date,
        end: date,
        initial_capital: Decimal,
        params: BacktestParams | None = None,
    ) -> BacktestResult: ...


@runtime_checkable
class IndicatorInterface(Protocol):
    """Technical indicators — compute a snapshot or a per-bar series."""

    def compute(
        self, bars: list[PriceBar], symbol: str, interval: Interval
    ) -> IndicatorSnapshot: ...

    def series(
        self, bars: list[PriceBar], symbol: str, interval: Interval
    ) -> list[IndicatorSnapshot]: ...


@runtime_checkable
class PortfolioInterface(Protocol):
    """Account state: the default portfolio, open positions and cash."""

    async def default_portfolio(self) -> Portfolio: ...

    async def positions(self, portfolio_id: int) -> list[Position]: ...

    async def cash(self, portfolio_id: int) -> Money: ...


@runtime_checkable
class PredictionInterface(Protocol):
    """Probability prediction over a feature vector (synchronous, pure)."""

    def predict(self, features: dict[str, float]) -> ModelOutput: ...


__all__ = [
    "BacktestInterface",
    "IndicatorInterface",
    "MarketDataInterface",
    "PortfolioInterface",
    "PredictionInterface",
    "StrategyInterface",
]
