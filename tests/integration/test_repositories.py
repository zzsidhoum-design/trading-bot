"""Integration tests for SQLAlchemy repositories.

These run only when ``QTRADER_RUN_INTEGRATION=1`` AND Postgres is reachable
(typically `docker compose up -d postgres`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from qtrader.config.settings import Settings
from qtrader.domain.entities import Portfolio, Stock
from qtrader.domain.value_objects import Interval, Money, PriceBar, TradingMode
from qtrader.infrastructure.database.models import PriceModel, StockModel
from qtrader.infrastructure.database.repositories import (
    SQLAlchemyPortfolioRepository,
    SQLAlchemyPriceRepository,
    SQLAlchemyStockRepository,
)
from qtrader.infrastructure.database.session import build_engine, build_session_factory

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def session_factory() -> async_sessionmaker:
    """Requires schema present: run `alembic upgrade head` first."""
    settings = Settings(_env_file=None)
    engine = build_engine(settings)
    return build_session_factory(engine)


@pytest.mark.asyncio
async def test_stock_upsert_and_get(session_factory: async_sessionmaker) -> None:
    repo = SQLAlchemyStockRepository(session_factory)
    saved = await repo.upsert(
        Stock(symbol="TSTU", exchange="XNAS", name="Test Unit", sector="TECH")
    )
    assert saved.stock_id is not None

    fetched = await repo.get_by_symbol("TSTU")
    assert fetched is not None
    assert fetched.symbol == "TSTU"


@pytest.mark.asyncio
async def test_portfolio_roundtrip(session_factory: async_sessionmaker) -> None:
    repo = SQLAlchemyPortfolioRepository(session_factory)
    created = await repo.create(
        Portfolio(
            name="test",
            initial_capital=Money("100000"),
            current_cash=Money("100000"),
            mode=TradingMode.BACKTEST,
        )
    )
    assert created.portfolio_id is not None
    fetched = await repo.get(created.portfolio_id)
    assert fetched is not None
    assert fetched.current_cash.amount == Decimal("100000.000000")


@pytest.mark.asyncio
async def test_price_bars_upsert_and_history(session_factory: async_sessionmaker) -> None:
    stock_repo = SQLAlchemyStockRepository(session_factory)
    price_repo = SQLAlchemyPriceRepository(session_factory)

    await stock_repo.upsert(Stock(symbol="TSTB", exchange="XNAS", name="Test Bars"))
    async with session_factory() as session:
        stock_id = await session.scalar(
            select(StockModel.id).where(StockModel.symbol == "TSTB")
        )
        assert stock_id is not None
        await session.execute(delete(PriceModel).where(PriceModel.stock_id == stock_id))
        await session.commit()
    ts = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    bars = [
        PriceBar(
            symbol="TSTB",
            interval=Interval.M5,
            ts=ts,
            open=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9.5"),
            close=Decimal("10.5"),
            volume=Decimal("1000"),
        ),
        PriceBar(
            symbol="TSTB",
            interval=Interval.M5,
            ts=datetime(2026, 8, 1, 12, 5, tzinfo=UTC),
            open=Decimal("10.5"),
            high=Decimal("12"),
            low=Decimal("10"),
            close=Decimal("11.8"),
            volume=Decimal("1500"),
        ),
    ]
    inserted = await price_repo.upsert_bars(bars)
    assert inserted == 2

    history = await price_repo.history("TSTB", Interval.M5)
    assert len(history) == 2
    assert history[0].close == Decimal("10.5")

    latest = await price_repo.latest("TSTB", Interval.M5)
    assert latest is not None
    assert latest.close == Decimal("11.8")
