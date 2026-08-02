"""Unit tests for the Portfolio Agent (allocation proposals)."""

from __future__ import annotations

from qtrader.application.agents.portfolio import PortfolioAgent
from qtrader.application.services.allocation_policy import EqualWeightAllocation
from qtrader.application.services.portfolio_service import PortfolioService
from qtrader.domain.events import AllocationProposal, RiskApproved
from qtrader.domain.value_objects import OrderPlan, OrderType, Percentage, TradeSide
from tests.unit.fakes_phase5 import (
    FakeEventBus,
    FakePortfolioRepository,
    FakePositionRepository,
    default_portfolio,
)


def _approved() -> RiskApproved:
    return RiskApproved(
        decision_uuid="d-1",
        plan=OrderPlan(
            symbol="AAPL",
            side=TradeSide.BUY,
            quantity=10,
            order_type=OrderType.MARKET,
            limit_price=None,
            stop_loss=98,
            take_profit=104,
            risk_per_trade=Percentage("0.01"),
            estimated_exposure=Percentage("0.05"),
            entry_price=100,
        ),
    )


async def test_allocate_publishes_proposal() -> None:
    bus = FakeEventBus()
    agent = PortfolioAgent(
        policy=EqualWeightAllocation(weight_per_trade=0.2),
        portfolio_service=PortfolioService(FakePortfolioRepository(default_portfolio())),
        positions=FakePositionRepository(),
        bus=bus,
    )
    proposal = await agent.allocate(_approved())
    assert proposal is not None
    assert proposal.symbol == "AAPL"
    assert proposal.side is TradeSide.BUY
    assert proposal.decision_uuid == "d-1"
    assert proposal.stop_loss == "98"
    assert proposal.take_profit == "104"
    assert any(isinstance(e, AllocationProposal) for e in bus.published)


async def test_allocate_skips_when_no_cash() -> None:
    bus = FakeEventBus()
    agent = PortfolioAgent(
        policy=EqualWeightAllocation(weight_per_trade=0.2),
        portfolio_service=PortfolioService(FakePortfolioRepository(default_portfolio(cash="0"))),
        positions=FakePositionRepository(),
        bus=bus,
    )
    proposal = await agent.allocate(_approved())
    assert proposal is None
    assert bus.published == []
