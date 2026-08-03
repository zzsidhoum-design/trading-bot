"""Unit of Work — one transaction across multiple SQLAlchemy repositories.

``SQLAlchemyUnitOfWork`` opens a single session and binds the trading
repositories (stocks, portfolios, positions, orders, trades) to it. Writes
defer commit to the UoW, so a multi-repo operation commits atomically or
rolls back entirely. Use it as ``async with uow_factory() as uow:``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from qtrader.domain.ports import (
    OrderRepository,
    PortfolioRepository,
    PositionRepository,
    StockRepository,
    TradeRepository,
    UnitOfWork,
    UnitOfWorkFactory,
)
from qtrader.infrastructure.database.repositories import (
    SQLAlchemyOrderRepository,
    SQLAlchemyPortfolioRepository,
    SQLAlchemyPositionRepository,
    SQLAlchemyStockRepository,
    SQLAlchemyTradeRepository,
)


class BoundRepos:
    """Repositories bound to one session, as exposed by a UnitOfWork."""

    def __init__(
        self,
        *,
        stocks: StockRepository,
        portfolios: PortfolioRepository,
        positions: PositionRepository,
        orders: OrderRepository,
        trades: TradeRepository,
    ) -> None:
        self.stocks = stocks
        self.portfolios = portfolios
        self.positions = positions
        self.orders = orders
        self.trades = trades


ReposBuilder = Callable[[AsyncSession], BoundRepos]


class SQLAlchemyUnitOfWork(UnitOfWork):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        build: ReposBuilder,
    ) -> None:
        self._session_factory = session_factory
        self._build = build
        self._session: AsyncSession | None = None
        self._repos: BoundRepos | None = None

    @property
    def stocks(self) -> StockRepository:
        return self._require().stocks

    @property
    def portfolios(self) -> PortfolioRepository:
        return self._require().portfolios

    @property
    def positions(self) -> PositionRepository:
        return self._require().positions

    @property
    def orders(self) -> OrderRepository:
        return self._require().orders

    @property
    def trades(self) -> TradeRepository:
        return self._require().trades

    def _require(self) -> BoundRepos:
        if self._repos is None:
            raise RuntimeError("UnitOfWork used outside its async context")
        return self._repos

    async def __aenter__(self) -> UnitOfWork:
        self._session = self._session_factory()
        self._repos = self._build(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any | None,
    ) -> None:
        try:
            if exc_type is None:
                await self.commit()
            else:
                await self.rollback()
        finally:
            await self.close()

    async def commit(self) -> None:
        if self._session is not None:
            await self._session.commit()

    async def rollback(self) -> None:
        if self._session is not None:
            await self._session.rollback()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
            self._repos = None


class SQLAlchemyUnitOfWorkFactory(UnitOfWorkFactory):
    """Builds a UnitOfWork whose repositories share one session."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    def _build(self, session: AsyncSession) -> BoundRepos:
        return BoundRepos(
            stocks=SQLAlchemyStockRepository(self._session_factory, session=session),
            portfolios=SQLAlchemyPortfolioRepository(self._session_factory, session=session),
            positions=SQLAlchemyPositionRepository(self._session_factory, session=session),
            orders=SQLAlchemyOrderRepository(self._session_factory, session=session),
            trades=SQLAlchemyTradeRepository(self._session_factory, session=session),
        )

    def __call__(self) -> UnitOfWork:
        return SQLAlchemyUnitOfWork(self._session_factory, self._build)
