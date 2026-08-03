"""Integration tests for the Unit of Work (atomic multi-repo transactions).

Verifies that writes across the trading repositories commit atomically on
clean exit and roll back entirely on error. Runs only with
``QTRADER_RUN_INTEGRATION=1`` (needs Postgres).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from qtrader.config.settings import Settings
from qtrader.domain.entities import Order, Portfolio, Stock, Trade
from qtrader.domain.value_objects import (
    Money,
    OrderStatus,
    OrderType,
    TradeSide,
    TradingMode,
)
from qtrader.infrastructure.database.models import (
    OrderModel,
    PortfolioModel,
    PositionModel,
    PriceModel,
    RiskHistoryModel,
    StockModel,
    TradeModel,
)
from qtrader.infrastructure.database.session import build_engine, build_session_factory
from qtrader.infrastructure.database.unit_of_work import SQLAlchemyUnitOfWorkFactory

pytestmark = pytest.mark.integration

_SYMBOL = "TSTU"


@pytest.fixture(scope="module")
def session_factory() -> async_sessionmaker:
    engine = build_engine(Settings(_env_file=None))
    return build_session_factory(engine)


@pytest.fixture(scope="module")
def uow_factory(session_factory: async_sessionmaker) -> SQLAlchemyUnitOfWorkFactory:
    return SQLAlchemyUnitOfWorkFactory(session_factory)


async def _wipe(session_factory: async_sessionmaker) -> None:
    async with session_factory() as session:
        await session.execute(delete(RiskHistoryModel))
        await session.execute(delete(TradeModel))
        await session.execute(delete(PositionModel))
        await session.execute(delete(OrderModel))
        await session.execute(delete(PortfolioModel))
        await session.execute(delete(PriceModel))
        await session.execute(delete(StockModel).where(StockModel.symbol == _SYMBOL))
        await session.commit()


async def _seed_stock(session_factory: async_sessionmaker) -> Stock:
    async with session_factory() as session:
        row = StockModel(symbol=_SYMBOL, exchange="TST", name=_SYMBOL)
        session.add(row)
        await session.commit()
        return Stock(symbol=_SYMBOL, exchange="TST", name=_SYMBOL, stock_id=row.id)


@pytest.mark.asyncio
async def test_uow_commit_persists_all_repos_atomically(
    session_factory: async_sessionmaker,
    uow_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    await _wipe(session_factory)
    stock = await _seed_stock(session_factory)
    try:
        async with uow_factory() as uow:
            portfolio = await uow.portfolios.create(
                Portfolio(
                    name="uow-commit",
                    currency="USD",
                    initial_capital=Money(100_000),
                    current_cash=Money(100_000),
                    mode=TradingMode.PAPER,
                )
            )
            portfolio_id = portfolio.portfolio_id
            assert portfolio_id is not None
            order = Order(
                portfolio_id=portfolio_id,
                stock_id=stock.stock_id or 0,
                side=TradeSide.SELL,
                order_type=OrderType.MARKET,
                quantity=10,
                mode=TradingMode.PAPER,
                idempotency_key="uow-commit-1",
                symbol=_SYMBOL,
                status=OrderStatus.PENDING,
            )
            trade = Trade(
                portfolio_id=portfolio_id,
                stock_id=stock.stock_id or 0,
                symbol=_SYMBOL,
                strategy="default",
                side=TradeSide.SELL,
                quantity=Decimal(10),
                entry_price=Decimal("100"),
                exit_price=Decimal("110"),
                pnl=Decimal("100"),
                fees=Decimal("1"),
                outcome="closed",
                mode=TradingMode.PAPER,
            )

            created = await uow.orders.create(order)
            await uow.orders.save(created)
            await uow.trades.record(trade)
            await uow.stocks.get_by_symbol(_SYMBOL)

        async with session_factory() as session:
            saved_order = await session.scalar(
                select(OrderModel).where(OrderModel.idempotency_key == "uow-commit-1")
            )
            saved_trade = await session.scalar(select(TradeModel))
        assert saved_order is not None
        assert saved_order.status == "PENDING"
        assert saved_trade is not None
        assert saved_trade.stock_id == (stock.stock_id or 0)
    finally:
        await _wipe(session_factory)


@pytest.mark.asyncio
async def test_uow_rollback_discards_all_repos(
    session_factory: async_sessionmaker,
    uow_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    await _wipe(session_factory)
    stock = await _seed_stock(session_factory)
    try:
        class Boom(RuntimeError):
            pass

        with pytest.raises(Boom):
            async with uow_factory() as uow:
                portfolio = await uow.portfolios.create(
                    Portfolio(
                        name="uow-test",
                        currency="USD",
                        initial_capital=Money(100_000),
                        current_cash=Money(100_000),
                        mode=TradingMode.PAPER,
                    )
                )
                portfolio_id = portfolio.portfolio_id
                assert portfolio_id is not None
                order = Order(
                    portfolio_id=portfolio_id,
                    stock_id=stock.stock_id or 0,
                    side=TradeSide.BUY,
                    order_type=OrderType.MARKET,
                    quantity=5,
                    mode=TradingMode.PAPER,
                    idempotency_key="uow-rollback-1",
                    symbol=_SYMBOL,
                    status=OrderStatus.PENDING,
                )
                await uow.orders.create(order)
                raise Boom("boom after writes")

        async with session_factory() as session:
            saved_order = await session.scalar(
                select(OrderModel).where(OrderModel.idempotency_key == "uow-rollback-1")
            )
            saved_portfolio = await session.scalar(
                select(PortfolioModel).where(PortfolioModel.name == "uow-test")
            )
        assert saved_order is None
        assert saved_portfolio is None
    finally:
        await _wipe(session_factory)


@pytest.mark.asyncio
async def test_uow_reads_see_pending_writes(
    session_factory: async_sessionmaker,
    uow_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    await _wipe(session_factory)
    try:
        async with uow_factory() as uow:
            portfolio = await uow.portfolios.create(
                Portfolio(
                    name="uow-pending",
                    currency="USD",
                    initial_capital=Money(50_000),
                    current_cash=Money(50_000),
                    mode=TradingMode.PAPER,
                )
            )
            portfolio_id = portfolio.portfolio_id
            assert portfolio_id is not None
            fetched = await uow.portfolios.get(portfolio_id)
            assert fetched is not None
            assert fetched.name == "uow-pending"
    finally:
        await _wipe(session_factory)
