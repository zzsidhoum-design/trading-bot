"""Integration test for the Phase 5 pipeline: Risk → Portfolio → Execution.

Drives a full BUY round-trip then a SELL exit through the real repositories,
event bus and paper broker against Postgres. Runs only when
``QTRADER_RUN_INTEGRATION=1``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from qtrader.application.agents.execution import ExecutionAgent
from qtrader.application.agents.portfolio import PortfolioAgent
from qtrader.application.agents.risk import RiskAgent
from qtrader.application.services.allocation_policy import MaxCashAllocation
from qtrader.application.services.portfolio_service import PortfolioService
from qtrader.application.services.risk_calculator import RiskCalculator, RiskPolicy
from qtrader.config.settings import Settings
from qtrader.domain.entities import IndicatorSnapshot, Stock
from qtrader.domain.events import (
    AllocationProposal,
    DecisionMade,
    RiskApproved,
)
from qtrader.domain.value_objects import (
    Decision,
    Interval,
    Money,
    PriceBar,
)
from qtrader.infrastructure.brokers.paper import PaperBroker
from qtrader.infrastructure.database.models import (
    EventRecordModel,
    IndicatorModel,
    OrderModel,
    PortfolioModel,
    PositionModel,
    PriceModel,
    RiskHistoryModel,
    StockModel,
    TradeModel,
)
from qtrader.infrastructure.database.repositories import (
    SQLAlchemyEventRepository,
    SQLAlchemyIndicatorRepository,
    SQLAlchemyOrderRepository,
    SQLAlchemyPortfolioRepository,
    SQLAlchemyPositionRepository,
    SQLAlchemyPriceRepository,
    SQLAlchemyRiskRepository,
    SQLAlchemyStockRepository,
    SQLAlchemyTradeRepository,
)
from qtrader.infrastructure.database.session import build_engine, build_session_factory
from qtrader.infrastructure.eventbus import InProcessEventBus

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def session_factory() -> async_sessionmaker:
    engine = build_engine(Settings(_env_file=None))
    return build_session_factory(engine)


async def _wipe(session_factory: async_sessionmaker) -> None:
    """Delete all Phase 5 test data (portfolios, orders, risk, events, TSTF)."""
    async with session_factory() as session:
        await session.execute(delete(TradeModel))
        await session.execute(delete(PositionModel))
        await session.execute(delete(OrderModel))
        await session.execute(delete(RiskHistoryModel))
        await session.execute(delete(PortfolioModel))
        await session.execute(delete(EventRecordModel))
        rows = await session.scalars(select(StockModel).where(StockModel.symbol == "TSTF"))
        stock_ids = [r.id for r in rows]
        if stock_ids:
            await session.execute(delete(PriceModel).where(PriceModel.stock_id.in_(stock_ids)))
            await session.execute(
                delete(IndicatorModel).where(IndicatorModel.stock_id.in_(stock_ids))
            )
        await session.execute(delete(StockModel).where(StockModel.symbol == "TSTF"))
        await session.commit()


@pytest.mark.asyncio
async def test_pipeline_risk_portfolio_execution(session_factory: async_sessionmaker) -> None:
    await _wipe(session_factory)
    stock_repo = SQLAlchemyStockRepository(session_factory)
    price_repo = SQLAlchemyPriceRepository(session_factory)
    indicator_repo = SQLAlchemyIndicatorRepository(session_factory)
    portfolio_repo = SQLAlchemyPortfolioRepository(session_factory)
    position_repo = SQLAlchemyPositionRepository(session_factory)
    order_repo = SQLAlchemyOrderRepository(session_factory)
    risk_repo = SQLAlchemyRiskRepository(session_factory)
    trade_repo = SQLAlchemyTradeRepository(session_factory)
    event_repo = SQLAlchemyEventRepository(session_factory)

    bus = InProcessEventBus(event_repo)
    portfolio_service = PortfolioService(portfolio_repo, initial_capital=Money(100_000))

    risk = RiskAgent(
        calculator=RiskCalculator(RiskPolicy(min_cooldown_minutes=0)),
        risk_repo=risk_repo,
        portfolio_service=portfolio_service,
        positions=position_repo,
        orders=order_repo,
        prices=price_repo,
        indicators=indicator_repo,
        stocks=stock_repo,
        bus=bus,
    )
    portfolio_agent = PortfolioAgent(
        policy=MaxCashAllocation(),
        portfolio_service=portfolio_service,
        positions=position_repo,
        bus=bus,
    )
    execution = ExecutionAgent(
        broker=PaperBroker(prices=price_repo),
        portfolio_service=portfolio_service,
        portfolios=portfolio_repo,
        positions=position_repo,
        orders=order_repo,
        stocks=stock_repo,
        trades=trade_repo,
        bus=bus,
    )
    bus.subscribe(DecisionMade, risk.on_event)
    bus.subscribe(RiskApproved, portfolio_agent.on_event)
    bus.subscribe(AllocationProposal, execution.on_event)

    now = datetime.now(UTC)
    await stock_repo.upsert(Stock(symbol="TSTF", exchange="XNAS", name="Pipeline F"))
    await price_repo.upsert_bars(
        [
            PriceBar(
                symbol="TSTF",
                interval=Interval.D1,
                ts=now - timedelta(hours=1),
                open=Decimal("100"),
                high=Decimal("102"),
                low=Decimal("98"),
                close=Decimal("100"),
                volume=Decimal("1000000"),
            )
        ]
    )
    await indicator_repo.save_snapshot(
        IndicatorSnapshot(
            symbol="TSTF", interval=Interval.D1, ts=now, atr=Decimal("2")
        )
    )

    portfolio = await portfolio_service.default_portfolio()
    assert portfolio.portfolio_id is not None
    portfolio_id = portfolio.portfolio_id
    assert portfolio.current_cash.amount == Decimal("100000")

    # ---- BUY round-trip ------------------------------------------------ #
    buy_uuid = "phase5-buy"
    await risk.assess_symbol(
        DecisionMade(
            decision_uuid=buy_uuid,
            symbol="TSTF",
            decision=Decision.BUY,
            confidence=0.8,
            rationale="pipeline buy",
        )
    )
    assessment = await risk_repo.recent(limit=10)
    buy_assessment = next((a for a in assessment if a.decision_uuid == buy_uuid), None)
    assert buy_assessment is not None
    assert buy_assessment.approved is True
    assert buy_assessment.portfolio_id == portfolio_id

    events = await event_repo.list_after(None, None, 100)
    raw_types = {e.type_name for e in events}
    assert "RiskApproved" in raw_types
    assert "AllocationProposal" in raw_types
    assert "OrderSubmitted" in raw_types
    assert "OrderFilled" in raw_types
    assert "RiskRejected" not in raw_types

    async with session_factory() as session:
        order = await session.scalar(
            select(OrderModel)
            .where(OrderModel.portfolio_id == portfolio_id)
            .order_by(OrderModel.id.asc())
        )
    assert order is not None
    assert order.status == "FILLED"
    assert order.side == "BUY"
    assert order.filled_qty == 333
    assert order.stop_loss is not None
    assert order.take_profit is not None
    assert Decimal(order.stop_loss) == Decimal("97")
    assert Decimal(order.take_profit) == Decimal("106")

    positions = await position_repo.open_positions(portfolio_id)
    assert len(positions) == 1
    assert positions[0].symbol == "TSTF"
    assert positions[0].quantity == 333
    assert positions[0].stop_loss is not None
    assert positions[0].stop_loss.amount == Decimal("97")

    portfolio_after_buy = await portfolio_repo.get(portfolio_id)
    assert portfolio_after_buy is not None
    assert portfolio_after_buy.current_cash.amount == Decimal("66700")

    # ---- SELL exit ------------------------------------------------------ #
    await price_repo.upsert_bars(
        [
            PriceBar(
                symbol="TSTF",
                interval=Interval.D1,
                ts=now,
                open=Decimal("110"),
                high=Decimal("112"),
                low=Decimal("108"),
                close=Decimal("110"),
                volume=Decimal("1000000"),
            )
        ]
    )
    sell_uuid = "phase5-sell"
    await risk.assess_symbol(
        DecisionMade(
            decision_uuid=sell_uuid,
            symbol="TSTF",
            decision=Decision.SELL,
            confidence=0.9,
            rationale="pipeline sell",
        )
    )

    async with session_factory() as session:
        closed = await session.scalar(
            select(PositionModel)
            .where(PositionModel.portfolio_id == portfolio_id)
            .order_by(PositionModel.id.desc())
        )
        assert closed is not None
        assert closed.status == "CLOSED"
        assert closed.quantity == 0
        assert Decimal(closed.realized_pnl) == Decimal("3330")

        trade = await session.scalar(
            select(TradeModel).where(TradeModel.portfolio_id == portfolio_id)
        )
        assert trade is not None
        assert Decimal(trade.pnl) == Decimal("3330")
        assert trade.outcome == "closed"

        remaining_positions = await session.scalar(
            select(func.count())
            .select_from(PositionModel)
            .where(PositionModel.portfolio_id == portfolio_id, PositionModel.status == "OPEN")
        )
        assert remaining_positions == 0

    portfolio_after_sell = await portfolio_repo.get(portfolio_id)
    assert portfolio_after_sell is not None
    assert portfolio_after_sell.current_cash.amount == Decimal("103330")

    events = await event_repo.list_after(None, None, 200)
    raw_types = {e.type_name for e in events}
    assert "PositionClosed" in raw_types

    # ---- clean up ------------------------------------------------------- #
    async with session_factory() as session:
        await session.execute(delete(TradeModel).where(TradeModel.portfolio_id == portfolio_id))
        await session.execute(
            delete(PositionModel).where(PositionModel.portfolio_id == portfolio_id)
        )
        await session.execute(delete(OrderModel).where(OrderModel.portfolio_id == portfolio_id))
        await session.execute(
            delete(RiskHistoryModel).where(RiskHistoryModel.portfolio_id == portfolio_id)
        )
        await session.execute(delete(PortfolioModel).where(PortfolioModel.id == portfolio_id))
        await session.execute(delete(EventRecordModel))
        rows = await session.scalars(
            select(StockModel).where(StockModel.symbol == "TSTF")
        )
        stock_ids = [r.id for r in rows]
        await session.execute(delete(PriceModel).where(PriceModel.stock_id.in_(stock_ids)))
        await session.execute(
            delete(IndicatorModel).where(IndicatorModel.stock_id.in_(stock_ids))
        )
        await session.execute(delete(StockModel).where(StockModel.symbol == "TSTF"))
        await session.commit()

    await bus.close()
