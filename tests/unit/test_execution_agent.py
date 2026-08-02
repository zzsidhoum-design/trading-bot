"""Unit tests for the Execution Agent (order → fill → position lifecycle)."""

from __future__ import annotations

from decimal import Decimal

from qtrader.application.agents.execution import ExecutionAgent
from qtrader.application.services.portfolio_service import PortfolioService
from qtrader.domain.entities import Position
from qtrader.domain.events import (
    AllocationProposal,
    PositionClosed,
)
from qtrader.domain.value_objects import (
    Money,
    OrderStatus,
    PositionStatus,
    TradeSide,
    TradingMode,
)
from qtrader.infrastructure.brokers.paper import PaperBroker
from tests.unit.fakes_phase5 import (
    FakeEventBus,
    FakeOrderRepository,
    FakePortfolioRepository,
    FakePositionRepository,
    FakePriceRepository,
    FakeStockRepository,
    FakeTradeRepository,
    default_portfolio,
)


def _proposal(
    side: TradeSide = TradeSide.BUY,
    qty: str = "10",
    stop: str | None = "95",
    tp: str | None = "110",
) -> AllocationProposal:
    return AllocationProposal(
        decision_uuid="d-1",
        order_id="o-1",
        symbol="AAPL",
        side=side,
        quantity=qty,
        order_type="MARKET",
        mode=TradingMode.PAPER,
        stop_loss=stop,
        take_profit=tp,
    )


def _agent(
    positions: list[Position] | None = None,
    close: str = "100",
    cash: str = "100000",
) -> tuple[
    ExecutionAgent,
    FakePortfolioRepository,
    FakeOrderRepository,
    FakeTradeRepository,
    FakeEventBus,
]:
    portfolios = FakePortfolioRepository(default_portfolio(cash=cash))
    orders = FakeOrderRepository()
    trades = FakeTradeRepository()
    bus = FakeEventBus()
    agent = ExecutionAgent(
        broker=PaperBroker(prices=FakePriceRepository(close=close)),
        portfolio_service=PortfolioService(portfolios),
        portfolios=portfolios,
        positions=FakePositionRepository(positions),
        orders=orders,
        stocks=FakeStockRepository(),
        trades=trades,
        bus=bus,
    )
    return agent, portfolios, orders, trades, bus


async def test_buy_fills_position_and_orders() -> None:
    agent, portfolios, orders, trades, bus = _agent()
    broker_order_id = await agent.execute(_proposal())
    assert broker_order_id is not None
    assert [e.type_name for e in bus.published] == ["OrderSubmitted", "OrderFilled"]

    order = orders._orders[0]
    assert order.status is OrderStatus.FILLED
    assert order.filled_qty == 10
    assert order.avg_fill_price is not None and order.avg_fill_price.amount == Decimal("100")
    assert order.stop_loss is not None and order.stop_loss.amount == Decimal("95")
    assert order.take_profit is not None and order.take_profit.amount == Decimal("110")

    saved = await agent._find_position(1, "AAPL")
    assert saved is not None
    assert saved.quantity == 10
    assert saved.avg_entry_price.amount == Decimal("100")
    assert saved.stop_loss is not None and saved.stop_loss.amount == Decimal("95")

    assert portfolios._portfolio is not None
    assert portfolios._portfolio.current_cash.amount == Decimal("99000")
    assert trades.trades == []


async def test_sell_closes_position_and_records_trade() -> None:
    position = Position(
        portfolio_id=1,
        stock_id=1,
        quantity=10,
        avg_entry_price=Money("100"),
        status=PositionStatus.OPEN,
        symbol="AAPL",
        position_id=1,
    )
    agent, portfolios, orders, trades, bus = _agent(positions=[position], close="110")
    await agent.execute(_proposal(side=TradeSide.SELL))

    assert portfolios._portfolio is not None
    assert portfolios._portfolio.current_cash.amount == Decimal("101100")
    assert len(trades.trades) == 1
    assert trades.trades[0].pnl == Decimal("100")
    assert any(isinstance(e, PositionClosed) for e in bus.published)

    closed = await agent._find_position(1, "AAPL")
    assert closed is None
    assert len(orders._orders) == 1
    assert orders._orders[0].status is OrderStatus.FILLED


async def test_duplicate_proposal_is_idempotent() -> None:
    agent, portfolios, orders, trades, bus = _agent()
    await agent.execute(_proposal())
    broker_order_id = await agent.execute(_proposal())
    assert broker_order_id is None
    assert len(orders._orders) == 1
