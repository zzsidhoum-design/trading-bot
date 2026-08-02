"""Integration test for the Phase 6 pipeline: backtest → metrics → SystemGate.

Seeds a deterministic bar history through the real PriceRepository, replays it
with the BacktestRunner, asserts persistence in ``backtest_runs`` /
``strategy_performance`` / ``system_logs``, then exercises the SystemGate
graduation decision against the live Postgres. Runs only when
``QTRADER_RUN_INTEGRATION=1``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from qtrader.application.services.backtest import BacktestParams, BacktestRunner
from qtrader.application.services.risk_calculator import RiskCalculator, RiskPolicy
from qtrader.application.services.system_gate import GateThresholds, SystemGate
from qtrader.config.settings import Settings
from qtrader.domain.entities import PerformanceSummary, Stock
from qtrader.domain.value_objects import Interval, PriceBar, TradingMode
from qtrader.infrastructure.database.models import (
    BacktestRunModel,
    PriceModel,
    StockModel,
    StrategyPerformanceModel,
    SystemLogModel,
)
from qtrader.infrastructure.database.repositories import (
    SQLAlchemyBacktestRepository,
    SQLAlchemyPerformanceRepository,
    SQLAlchemyPriceRepository,
    SQLAlchemyStockRepository,
    SQLAlchemySystemLogRepository,
)
from qtrader.infrastructure.database.session import build_engine, build_session_factory

pytestmark = pytest.mark.integration

SYMBOL = "BTST"


@pytest.fixture(scope="module")
def session_factory() -> async_sessionmaker:
    engine = build_engine(Settings(_env_file=None))
    return build_session_factory(engine)


async def _wipe(session_factory: async_sessionmaker) -> None:
    async with session_factory() as session:
        await session.execute(delete(SystemLogModel))
        await session.execute(delete(StrategyPerformanceModel))
        await session.execute(delete(BacktestRunModel))
        rows = await session.scalars(select(StockModel).where(StockModel.symbol == SYMBOL))
        stock_ids = [r.id for r in rows]
        if stock_ids:
            await session.execute(delete(PriceModel).where(PriceModel.stock_id.in_(stock_ids)))
        await session.execute(delete(StockModel).where(StockModel.symbol == SYMBOL))
        await session.commit()


def _trend_bars(days: int = 120) -> list[PriceBar]:
    start = datetime(2026, 1, 1, 16, 0, tzinfo=UTC)
    bars: list[PriceBar] = []
    price = 100.0
    for i in range(days):
        if i < 40:
            step = 0.0
        elif i < 44:
            step = -2.0
        elif i < 74:
            step = 1.0
        else:
            step = -1.5
        price += step
        bars.append(
            PriceBar(
                symbol=SYMBOL,
                interval=Interval.D1,
                ts=start + timedelta(days=i),
                open=Decimal(f"{price:.2f}"),
                high=Decimal(f"{price + 1.5:.2f}"),
                low=Decimal(f"{price - 1.5:.2f}"),
                close=Decimal(f"{price:.2f}"),
                volume=Decimal("1000000"),
            )
        )
    return bars


def _runner(session_factory: async_sessionmaker) -> BacktestRunner:
    return BacktestRunner(
        prices=SQLAlchemyPriceRepository(session_factory),
        backtests=SQLAlchemyBacktestRepository(session_factory),
        performance=SQLAlchemyPerformanceRepository(session_factory),
        risk_calculator=RiskCalculator(RiskPolicy(risk_per_trade_pct=0.01)),
        logs=SQLAlchemySystemLogRepository(session_factory),
    )


@pytest.mark.asyncio
async def test_backtest_pipeline_persists_and_gate_decides(
    session_factory: async_sessionmaker,
) -> None:
    await _wipe(session_factory)
    stock_repo = SQLAlchemyStockRepository(session_factory)
    price_repo = SQLAlchemyPriceRepository(session_factory)

    await stock_repo.upsert(Stock(symbol=SYMBOL, exchange="XNAS", name="Backtest F"))
    inserted = await price_repo.upsert_bars(_trend_bars())
    assert inserted == 120

    start = datetime(2026, 1, 1, tzinfo=UTC).date()
    end = datetime(2026, 4, 30, tzinfo=UTC).date()
    result = await _runner(session_factory).run(
        name="integration-run",
        symbols=[SYMBOL],
        start=start,
        end=end,
        initial_capital=Decimal("100000"),
        params=BacktestParams(commission_bps=1.0),
    )

    assert result.run.run_id is not None
    assert result.run.status == "completed"
    assert result.run.final_capital is not None
    assert result.summary.trades_count == len(result.trades)

    # Backtest row is persisted with metrics.
    async with session_factory() as session:
        row = await session.get(BacktestRunModel, result.run.run_id)
        assert row is not None
        assert row.status == "completed"
        assert row.final_capital is not None
        assert row.metrics is not None
        assert row.interval == "1d"
        assert row.strategy == "ensemble"
        assert row.commission_bps == Decimal("1")

    # Performance row upserted under the unique key.
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(StrategyPerformanceModel)
            .where(StrategyPerformanceModel.strategy == "ensemble")
        )
        assert count == 1

    # System logs recorded start + completion.
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(SystemLogModel)
            .where(SystemLogModel.component == "backtest")
        )
        assert count >= 2

    # Gate: evaluate PAPER against real stored performance; always a decision.
    gate = SystemGate(
        thresholds=GateThresholds(),
        performance=SQLAlchemyPerformanceRepository(session_factory),
        logs=SQLAlchemySystemLogRepository(session_factory),
    )
    decision = await gate.evaluate("ensemble", TradingMode.PAPER)
    assert decision.approved in (True, False)

    # A qualifying summary graduates the gate through the real DB.
    perf_repo = SQLAlchemyPerformanceRepository(session_factory)
    await perf_repo.upsert(
        PerformanceSummary(
            strategy="ensemble",
            mode=TradingMode.BACKTEST,
            period_start=start,
            period_end=end,
            total_return=Decimal("0.30"),
            sharpe=Decimal("1.8"),
            sortino=Decimal("2.2"),
            max_drawdown=Decimal("-0.12"),
            win_rate=Decimal("0.60"),
            profit_factor=Decimal("1.7"),
            trades_count=60,
            final_equity=Decimal("130000"),
        )
    )
    graduated = await gate.evaluate("ensemble", TradingMode.PAPER)
    assert graduated.approved is True
    assert graduated.status.value == "graduated"
    assert await gate.can_trade("ensemble", TradingMode.PAPER) is True
