"""Unit tests for the Risk Agent (event-driven gate)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from qtrader.application.agents.risk import RiskAgent
from qtrader.application.services.portfolio_service import PortfolioService
from qtrader.application.services.risk_calculator import RiskCalculator, RiskPolicy
from qtrader.domain.entities import Order
from qtrader.domain.events import DecisionMade, RiskApproved, RiskRejected
from qtrader.domain.value_objects import (
    Decision,
    Money,
    OrderStatus,
    OrderType,
    TradeSide,
    TradingMode,
)
from tests.unit.fakes_phase5 import (
    FakeEventBus,
    FakeIndicatorRepository,
    FakeOrderRepository,
    FakePortfolioRepository,
    FakePositionRepository,
    FakePriceRepository,
    FakeRiskRepository,
    FakeStockRepository,
    default_portfolio,
)


def _decision(decision: Decision = Decision.BUY) -> DecisionMade:
    return DecisionMade(
        decision_uuid="d-1",
        symbol="AAPL",
        decision=decision,
        confidence=0.8,
        rationale="momentum up",
    )


def _agent(**kwargs) -> RiskAgent:
    return RiskAgent(
        calculator=kwargs.get("calculator", RiskCalculator(RiskPolicy())),
        risk_repo=kwargs.get("risk_repo", FakeRiskRepository()),
        portfolio_service=kwargs.get(
            "portfolio_service", PortfolioService(FakePortfolioRepository(default_portfolio()))
        ),
        positions=kwargs.get("positions", FakePositionRepository()),
        orders=kwargs.get("orders", FakeOrderRepository()),
        prices=kwargs.get("prices", FakePriceRepository()),
        indicators=kwargs.get("indicators", FakeIndicatorRepository()),
        stocks=kwargs.get("stocks", FakeStockRepository()),
        bus=kwargs.get("bus", FakeEventBus()),
    )


async def test_approved_decision_publishes_risk_approved() -> None:
    bus = FakeEventBus()
    risk_repo = FakeRiskRepository()
    agent = _agent(bus=bus, risk_repo=risk_repo)
    assessment = await agent.assess_symbol(_decision())
    assert assessment.approved is True
    assert risk_repo.assessments[0].approved is True
    assert any(isinstance(e, RiskApproved) for e in bus.published)
    approved = next(e for e in bus.published if isinstance(e, RiskApproved))
    assert approved.plan.symbol == "AAPL"
    assert approved.plan.quantity > 0
    assert approved.plan.stop_loss < Decimal("100")
    assert approved.plan.take_profit > Decimal("100")


async def test_no_price_data_rejected() -> None:
    bus = FakeEventBus()
    agent = _agent(bus=bus, prices=FakePriceRepository(close=None))
    assessment = await agent.assess_symbol(_decision())
    assert assessment.approved is False
    assert any(isinstance(e, RiskRejected) for e in bus.published)
    assert "no price data for symbol" in assessment.rejection_reasons


async def test_cooldown_rejects() -> None:
    bus = FakeEventBus()
    recent = Order(
        portfolio_id=1,
        stock_id=1,
        side=TradeSide.BUY,
        order_type=OrderType.MARKET,
        quantity=10,
        mode=TradingMode.PAPER,
        idempotency_key="k-1",
        symbol="AAPL",
        status=OrderStatus.SUBMITTED,
        created_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    agent = _agent(bus=bus, orders=FakeOrderRepository([recent]))
    assessment = await agent.assess_symbol(_decision())
    assert assessment.approved is False
    assert any("cooldown" in r for r in assessment.rejection_reasons)


async def test_sell_with_open_position_approved() -> None:
    bus = FakeEventBus()
    from qtrader.domain.entities import Position
    from qtrader.domain.value_objects import PositionStatus

    position = Position(
        portfolio_id=1,
        stock_id=1,
        quantity=10,
        avg_entry_price=Money("100"),
        status=PositionStatus.OPEN,
        symbol="AAPL",
        position_id=1,
    )
    agent = _agent(bus=bus, positions=FakePositionRepository([position]))
    assessment = await agent.assess_symbol(_decision(Decision.SELL))
    assert assessment.approved is True
    assert assessment.position_size == Decimal("10")
    assert any(isinstance(e, RiskApproved) for e in bus.published)
