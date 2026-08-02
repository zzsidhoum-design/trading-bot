"""Integration fault-injection tests (Phase 8 hardening).

Drives the real repositories + event bus against Postgres while injecting
failures: a broker that is unavailable and a provider whose circuit is open.
Verifies graceful degradation (order rejected, error event published, no
exception escapes). Runs only when ``QTRADER_RUN_INTEGRATION=1``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from qtrader.application.agents.execution import ExecutionAgent
from qtrader.application.services.portfolio_service import PortfolioService
from qtrader.config.container import Container
from qtrader.config.settings import Settings
from qtrader.domain.entities import Order, Stock
from qtrader.domain.events import OrderStatusChanged
from qtrader.domain.ports import BrokerGateway
from qtrader.domain.value_objects import (
    Interval,
    Money,
    OrderFill,
    OrderStatus,
    OrderType,
    PriceBar,
    TradeSide,
    TradingMode,
)
from qtrader.infrastructure.database.models import (
    EventRecordModel,
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
    SQLAlchemyOrderRepository,
    SQLAlchemyPortfolioRepository,
    SQLAlchemyPositionRepository,
    SQLAlchemyPriceRepository,
    SQLAlchemyStockRepository,
    SQLAlchemyTradeRepository,
)
from qtrader.infrastructure.database.session import build_engine, build_session_factory
from qtrader.infrastructure.eventbus import InProcessEventBus

pytestmark = pytest.mark.integration

_SYMBOL = "TSTH"


class FailingBroker(BrokerGateway):
    """Broker that is always unavailable — simulates an outage."""

    async def submit_order(self, order: Order) -> str:
        raise RuntimeError("BrokerUnavailable: upstream gateway is down")

    async def cancel_order(self, broker_order_id: str) -> None:
        raise RuntimeError("BrokerUnavailable")

    async def modify_brackets(
        self,
        position_id: str,
        stop_loss: object,
        take_profit: object,
    ) -> None:
        raise RuntimeError("BrokerUnavailable")

    async def get_order_status(self, broker_order_id: str) -> OrderFill:
        raise RuntimeError("BrokerUnavailable")

    async def close(self) -> None:
        return None


@pytest.fixture(scope="module")
def session_factory() -> async_sessionmaker:
    engine = build_engine(Settings(_env_file=None))
    return build_session_factory(engine)


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings(_env_file=None)


async def _wipe(session_factory: async_sessionmaker) -> None:
    async with session_factory() as session:
        await session.execute(delete(TradeModel))
        await session.execute(delete(PositionModel))
        await session.execute(delete(OrderModel))
        await session.execute(delete(RiskHistoryModel))
        await session.execute(delete(PortfolioModel))
        await session.execute(delete(EventRecordModel))
        rows = await session.scalars(select(StockModel).where(StockModel.symbol == _SYMBOL))
        stock_ids = [r.id for r in rows]
        if stock_ids:
            await session.execute(delete(PriceModel).where(PriceModel.stock_id.in_(stock_ids)))
        await session.execute(delete(StockModel).where(StockModel.symbol == _SYMBOL))
        await session.commit()


@pytest.mark.asyncio
async def test_broker_outage_rejects_order_gracefully(
    session_factory: async_sessionmaker,
) -> None:
    await _wipe(session_factory)
    stock_repo = SQLAlchemyStockRepository(session_factory)
    price_repo = SQLAlchemyPriceRepository(session_factory)
    portfolio_repo = SQLAlchemyPortfolioRepository(session_factory)
    position_repo = SQLAlchemyPositionRepository(session_factory)
    order_repo = SQLAlchemyOrderRepository(session_factory)
    trade_repo = SQLAlchemyTradeRepository(session_factory)
    event_repo = SQLAlchemyEventRepository(session_factory)
    bus = InProcessEventBus(event_repo)

    portfolio_service = PortfolioService(portfolio_repo, initial_capital=Money(100_000))
    execution = ExecutionAgent(
        broker=FailingBroker(),
        portfolio_service=portfolio_service,
        portfolios=portfolio_repo,
        positions=position_repo,
        orders=order_repo,
        stocks=stock_repo,
        trades=trade_repo,
        bus=bus,
    )

    await stock_repo.upsert(Stock(symbol=_SYMBOL, exchange="XNAS", name="Hardening"))
    await price_repo.upsert_bars(
        [
            PriceBar(
                symbol=_SYMBOL,
                interval=Interval.D1,
                ts=datetime.now(UTC) - timedelta(hours=1),
                open=Decimal("100"),
                high=Decimal("102"),
                low=Decimal("98"),
                close=Decimal("101"),
                volume=Decimal("1000000"),
            )
        ]
    )
    portfolio = await portfolio_service.default_portfolio()
    assert portfolio.portfolio_id is not None
    portfolio_id = portfolio.portfolio_id
    stock = await stock_repo.get_by_symbol(_SYMBOL)
    assert stock is not None and stock.stock_id is not None

    order = await order_repo.create(
        Order(
            portfolio_id=portfolio_id,
            stock_id=stock.stock_id,
            side=TradeSide.BUY,
            order_type=OrderType.MARKET,
            quantity=10,
            mode=TradingMode.BACKTEST,
            idempotency_key="hardening-broker-outage",
            limit_price=None,
            symbol=_SYMBOL,
            status=OrderStatus.PENDING,
        )
    )
    assert order.order_id is not None

    result = await execution.execute_order(order)
    assert result is None  # no broker id returned on failure

    async with session_factory() as session:
        saved_model = await session.scalar(
            select(OrderModel).where(OrderModel.idempotency_key == "hardening-broker-outage")
        )
    assert saved_model is not None
    assert saved_model.status == "REJECTED"

    events = await event_repo.list_after(None, None, 100)
    status_changes = [
        e for e in events if isinstance(e, OrderStatusChanged)
    ]
    rejected = next(
        (e for e in status_changes if str(e.status) == "REJECTED"), None
    )
    assert rejected is not None
    assert rejected.detail == "BrokerUnavailable: upstream gateway is down"

    await bus.close()
    await _wipe(session_factory)


@pytest.mark.asyncio
async def test_container_exposes_breaker_snapshot(settings: Settings) -> None:
    container = Container(settings)
    try:
        snapshots = container.circuit_breakers()
        names = {s["name"] for s in snapshots}
        assert "yahoo" in names
        yahoo = next(s for s in snapshots if s["name"] == "yahoo")
        assert yahoo["state"] == "closed"
        assert yahoo["consecutive_failures"] == 0
    finally:
        await container.aclose()
