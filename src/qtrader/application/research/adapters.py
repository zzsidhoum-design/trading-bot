"""Concrete adapters implementing the research interfaces over existing services.

Each adapter is a thin, dependency-injected wrapper around a production service
or repository. Third-party libraries (pandas/numpy) are never exposed here —
they stay behind ``IndicatorEngine``/``BacktestRunner``/``prediction_model``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from qtrader.application.research.interfaces import (
    BacktestInterface,
    IndicatorInterface,
    MarketDataInterface,
    PortfolioInterface,
    PredictionInterface,
    StrategyInterface,
)
from qtrader.application.research.strategy.engine import (
    ResearchReport,
    ResearchRequest,
    StrategyResearchEngine,
)
from qtrader.application.research.strategy.registry import StrategyRegistry
from qtrader.application.services.backtest import BacktestParams, BacktestResult, BacktestRunner
from qtrader.application.services.indicators import IndicatorEngine
from qtrader.application.services.portfolio_service import PortfolioService
from qtrader.application.services.prediction_model import (
    HeuristicModel,
    LogisticModel,
    ModelOutput,
    PredictionModel,
)
from qtrader.application.services.strategies.base import Strategy, StrategyInputs
from qtrader.domain.entities import IndicatorSnapshot, Portfolio, Position
from qtrader.domain.ports import (
    ModelRepository,
    PortfolioRepository,
    PositionRepository,
    PriceRepository,
)
from qtrader.domain.value_objects import Interval, Money, PriceBar


@dataclass(frozen=True, slots=True)
class MarketDataAdapter(MarketDataInterface):
    """Wraps :class:`PriceRepository` as the research market-data seam."""

    prices: PriceRepository

    async def history(
        self,
        symbol: str,
        interval: Interval,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> list[PriceBar]:
        return await self.prices.history(symbol, interval, start, end, limit)

    async def latest(self, symbol: str, interval: Interval) -> PriceBar | None:
        return await self.prices.latest(symbol, interval)


@dataclass(frozen=True, slots=True)
class StrategyAdapter(StrategyInterface):
    """Wraps a concrete :class:`Strategy` under the strategy contract."""

    strategy: Strategy

    @property
    def name(self) -> str:
        return self.strategy.name

    def generate_probs(
        self, inputs: StrategyInputs
    ) -> dict[str, dict[datetime, float]]:
        return self.strategy.generate_probs(inputs)


@dataclass(frozen=True, slots=True)
class BacktestAdapter(BacktestInterface):
    """Wraps :class:`BacktestRunner` (fills, costs, ATR sizing, exits)."""

    runner: BacktestRunner

    async def run(
        self,
        name: str,
        symbols: list[str],
        start: date,
        end: date,
        initial_capital: Decimal,
        params: BacktestParams | None = None,
    ) -> BacktestResult:
        return await self.runner.run(
            name=name,
            symbols=symbols,
            start=start,
            end=end,
            initial_capital=initial_capital,
            params=params,
        )


@dataclass(frozen=True, slots=True)
class IndicatorAdapter(IndicatorInterface):
    """Wraps :class:`IndicatorEngine` (pandas stays behind this seam)."""

    engine: IndicatorEngine

    def compute(
        self, bars: list[PriceBar], symbol: str, interval: Interval
    ) -> IndicatorSnapshot:
        return self.engine.compute(bars, symbol, interval)

    def series(
        self, bars: list[PriceBar], symbol: str, interval: Interval
    ) -> list[IndicatorSnapshot]:
        return self.engine.compute_series(bars, symbol, interval)


@dataclass(frozen=True, slots=True)
class PortfolioAdapter(PortfolioInterface):
    """Wraps :class:`PortfolioService` + position/portfolio repositories."""

    service: PortfolioService
    positions_repo: PositionRepository
    portfolios_repo: PortfolioRepository

    async def default_portfolio(self) -> Portfolio:
        return await self.service.default_portfolio()

    async def positions(self, portfolio_id: int) -> list[Position]:
        return await self.positions_repo.open_positions(portfolio_id)

    async def cash(self, portfolio_id: int) -> Money:
        portfolio = await self.portfolios_repo.get(portfolio_id)
        if portfolio is None:
            raise ValueError(f"no portfolio with id {portfolio_id}")
        return portfolio.current_cash


@dataclass(frozen=True, slots=True)
class PredictionAdapter(PredictionInterface):
    """Wraps a :class:`PredictionModel` (LogisticModel or heuristic fallback)."""

    model: PredictionModel

    @classmethod
    async def from_registry(
        cls, model_repo: ModelRepository, name: str
    ) -> PredictionAdapter:
        """Load the active registered model, falling back to the heuristic."""
        registered = await model_repo.load_active(name)
        model: PredictionModel = HeuristicModel()
        if registered is not None:
            loaded = LogisticModel.from_registered(registered)
            if loaded is not None:
                model = loaded
        return cls(model=model)

    def predict(self, features: dict[str, float]) -> ModelOutput:
        return self.model.predict(features)


@dataclass(frozen=True, slots=True)
class StrategyResearchAdapter:
    """Research seam: run the automated strategy research workflow."""

    engine: StrategyResearchEngine

    async def run(self, request: ResearchRequest) -> ResearchReport:
        return await self.engine.run(request)

    @property
    def registry(self) -> StrategyRegistry:
        return self.engine.registry


__all__ = [
    "BacktestAdapter",
    "IndicatorAdapter",
    "MarketDataAdapter",
    "PortfolioAdapter",
    "PredictionAdapter",
    "StrategyAdapter",
    "StrategyResearchAdapter",
]
