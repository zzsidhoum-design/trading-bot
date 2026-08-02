"""Portfolio service — resolves the default (or first) portfolio lazily."""

from __future__ import annotations

from typing import cast

from qtrader.domain.entities import Portfolio
from qtrader.domain.ports import PortfolioRepository
from qtrader.domain.value_objects import Money, TradingMode


class PortfolioService:
    def __init__(
        self,
        repo: PortfolioRepository,
        *,
        name: str = "default",
        initial_capital: Money = Money(100_000),
        mode: TradingMode = TradingMode.BACKTEST,
        default_id: int = 1,
    ) -> None:
        self._repo = repo
        self._name = name
        self._initial_capital = initial_capital
        self._mode = mode
        self._default_id = default_id
        self._portfolio_id: int | None = None

    async def default_portfolio(self) -> Portfolio:
        if self._portfolio_id is not None:
            portfolio = await self._repo.get(self._portfolio_id)
            if portfolio is not None:
                return cast(Portfolio, portfolio)
        portfolio = await self._repo.get(self._default_id)
        if portfolio is None:
            portfolio = await self._repo.create(
                Portfolio(
                    name=self._name,
                    currency="USD",
                    initial_capital=self._initial_capital,
                    current_cash=self._initial_capital,
                    mode=self._mode,
                )
            )
        assert portfolio.portfolio_id is not None
        self._portfolio_id = portfolio.portfolio_id
        return cast(Portfolio, portfolio)
