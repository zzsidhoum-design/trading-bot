"""Integration tests for the Phase 7 dashboard read-side against live Postgres.

Requires ``QTRADER_RUN_INTEGRATION=1`` + Postgres (``docker compose up -d postgres``)
with the schema applied (``alembic upgrade head``). Uses unique TST* symbols to
avoid clashing with earlier runs.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from qtrader.config.settings import Settings
from qtrader.domain.entities import (
    BacktestRun,
    PerformanceSummary,
    Portfolio,
    Position,
    Stock,
    SystemLog,
    Trade,
)
from qtrader.domain.value_objects import (
    Interval,
    Money,
    PositionStatus,
    PriceBar,
    TradeSide,
    TradingMode,
)
from qtrader.infrastructure.database.repositories import (
    SQLAlchemyBacktestRepository,
    SQLAlchemyDashboardRepository,
    SQLAlchemyModelRepository,
    SQLAlchemyPerformanceRepository,
    SQLAlchemyPortfolioRepository,
    SQLAlchemyPositionRepository,
    SQLAlchemyPriceRepository,
    SQLAlchemyStockRepository,
    SQLAlchemySystemLogRepository,
    SQLAlchemyTradeRepository,
)
from qtrader.infrastructure.database.session import build_engine, build_session_factory

pytestmark = pytest.mark.integration

_SYMBOL = "TSTD"


@pytest.fixture(scope="module")
def session_factory() -> async_sessionmaker:
    settings = Settings(_env_file=None)
    engine = build_engine(settings)
    return build_session_factory(engine)


@pytest.fixture
def seeded() -> int:
    """Seed dashboard data in an isolated engine/loop (avoids cross-loop pool reuse)."""

    async def _seed() -> int:
        settings = Settings(_env_file=None)
        engine = build_engine(settings)
        factory = build_session_factory(engine)
        try:
            stocks = SQLAlchemyStockRepository(factory)
            stock = await stocks.upsert(
                Stock(symbol=_SYMBOL, exchange="XNAS", name="Test Dashboard")
            )
            assert stock.stock_id is not None
            stock_id = stock.stock_id

            portfolios = SQLAlchemyPortfolioRepository(factory)
            portfolio = await portfolios.create(
                Portfolio(
                    name="dash-test",
                    initial_capital=Money("100000"),
                    current_cash=Money("90000"),
                    mode=TradingMode.BACKTEST,
                )
            )
            assert portfolio.portfolio_id is not None
            portfolio_id = portfolio.portfolio_id

            prices = SQLAlchemyPriceRepository(factory)
            await prices.upsert_bars(
                [
                    PriceBar(
                        symbol=_SYMBOL,
                        interval=Interval.D1,
                        ts=datetime(2026, 8, 1, tzinfo=UTC),
                        open=Decimal("50"),
                        high=Decimal("52"),
                        low=Decimal("49"),
                        close=Decimal("51"),
                        volume=Decimal("1000000"),
                    )
                ]
            )

            positions = SQLAlchemyPositionRepository(factory)
            saved = await positions.save(
                Position(
                    portfolio_id=portfolio_id,
                    stock_id=stock_id,
                    quantity=10,
                    avg_entry_price=Money("50"),
                    symbol=_SYMBOL,
                    status=PositionStatus.OPEN,
                )
            )
            assert saved.position_id is not None

            trades = SQLAlchemyTradeRepository(factory)
            await trades.record(
                Trade(
                    portfolio_id=portfolio_id,
                    stock_id=stock_id,
                    symbol=_SYMBOL,
                    strategy="ensemble",
                    side=TradeSide.SELL,
                    quantity=Decimal("10"),
                    entry_price=Decimal("50"),
                    exit_price=Decimal("52"),
                    pnl=Decimal("20"),
                    pnl_pct=Decimal("0.04"),
                    fees=Decimal("0"),
                    entry_time=datetime(2026, 8, 1, 9, 30, tzinfo=UTC),
                    exit_time=datetime(2026, 8, 1, 15, 0, tzinfo=UTC),
                    outcome="closed",
                    mode=TradingMode.BACKTEST,
                )
            )

            backtests = SQLAlchemyBacktestRepository(factory)
            await backtests.create(
                BacktestRun(
                    name="dash-test",
                    universe=[_SYMBOL],
                    start=datetime(2026, 1, 1).date(),
                    end=datetime(2026, 6, 1).date(),
                    initial_capital=Money("100000"),
                    status="completed",
                )
            )

            logs = SQLAlchemySystemLogRepository(factory)
            await logs.record(
                SystemLog(level="INFO", component="dashboard", message="integration seed")
            )

            metrics_repo = SQLAlchemyModelRepository(factory)
            await metrics_repo.create_version(
                name="dash-momentum",
                hyperparams={"coef": [0.1]},
                training_window="200x5",
                offline_metrics={"accuracy": 0.6},
            )

            performance = SQLAlchemyPerformanceRepository(factory)
            await performance.upsert(
                PerformanceSummary(
                    strategy="ensemble",
                    mode=TradingMode.BACKTEST,
                    period_start=datetime(2026, 1, 1).date(),
                    period_end=datetime(2026, 6, 1).date(),
                    total_return=Decimal("0.05"),
                    trades_count=1,
                )
            )
            await metrics_repo.promote("dash-momentum", 1)
            return portfolio_id
        finally:
            await engine.dispose()

    return asyncio.run(_seed())


@pytest.mark.asyncio
async def test_dashboard_positions_and_trades(
    session_factory: async_sessionmaker, seeded: int
) -> None:
    dashboard = SQLAlchemyDashboardRepository(session_factory)
    positions = await dashboard.positions(seeded)
    assert any(p.symbol == _SYMBOL for p in positions)
    trades = await dashboard.trades(seeded, limit=10)
    assert any(t.symbol == _SYMBOL for t in trades)


@pytest.mark.asyncio
async def test_dashboard_logs_and_metrics(
    session_factory: async_sessionmaker, seeded: int
) -> None:
    dashboard = SQLAlchemyDashboardRepository(session_factory)
    logs = await dashboard.logs(level="INFO", component="dashboard", limit=5)
    assert logs
    assert logs[0].level == "INFO"
    metrics = await dashboard.agent_metrics(limit=5)
    assert isinstance(metrics, list)


@pytest.mark.asyncio
async def test_dashboard_performance_and_models(
    session_factory: async_sessionmaker, seeded: int
) -> None:
    dashboard = SQLAlchemyDashboardRepository(session_factory)
    results = await dashboard.performance(strategy="ensemble", mode=None, limit=5)
    assert results
    assert results[0].strategy == "ensemble"
    models = await dashboard.models()
    assert models
    assert models[0].name == "dash-momentum"
