"""Portfolio Agent â€” capital allocation & rebalancing (docs/02-agents.md Â§8).

Consumes ``RiskApproved``, applies the pluggable :class:`AllocationPolicy` to
turn the risk-sized plan into an executable quantity (never exceeding
available cash), and publishes an ``AllocationProposal`` for the Execution
Agent. Order lifecycle is owned by the Execution Agent.
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from qtrader.application.agents.base import AgentBase, AgentContext
from qtrader.application.services.portfolio_service import PortfolioService
from qtrader.domain.events import AllocationProposal, DomainEvent, RiskApproved
from qtrader.domain.ports import AllocationPolicy, EventBus, PositionRepository


class PortfolioAgent(AgentBase):
    name: ClassVar[str] = "portfolio"
    consumes: ClassVar[tuple[type[DomainEvent], ...]] = (RiskApproved,)
    produces: ClassVar[tuple[type[DomainEvent], ...]] = (AllocationProposal,)

    def __init__(
        self,
        policy: AllocationPolicy,
        portfolio_service: PortfolioService,
        positions: PositionRepository,
        bus: EventBus,
    ) -> None:
        self._policy = policy
        self._portfolios = portfolio_service
        self._positions = positions
        self._bus = bus

    async def allocate(self, event: RiskApproved) -> AllocationProposal | None:
        portfolio = await self._portfolios.default_portfolio()
        portfolio_id = portfolio.portfolio_id
        assert portfolio_id is not None

        open_positions = await self._positions.open_positions(portfolio_id)
        quantity = self._policy.quantity_for(
            event.plan, portfolio.current_cash, len(open_positions)
        )
        if quantity <= 0:
            self._logger.warning(
                "portfolio.skip",
                symbol=event.plan.symbol,
                reason="insufficient cash",
            )
            return None

        proposal = AllocationProposal(
            decision_uuid=event.decision_uuid,
            order_id=str(uuid.uuid4()),
            symbol=event.plan.symbol,
            side=event.plan.side,
            quantity=str(int(quantity)),
            order_type=event.plan.order_type.value,
            mode=portfolio.mode,
            stop_loss=(
                str(event.plan.stop_loss) if event.plan.stop_loss is not None else None
            ),
            take_profit=(
                str(event.plan.take_profit) if event.plan.take_profit is not None else None
            ),
        )
        self._logger.info(
            "portfolio.allocate",
            symbol=proposal.symbol,
            qty=proposal.quantity,
            cash=str(portfolio.current_cash.amount),
        )
        await self._bus.publish(proposal)
        return proposal

    async def on_event(self, event: DomainEvent) -> None:
        if isinstance(event, RiskApproved):
            try:
                await self.allocate(event)
            except Exception:
                self._logger.exception("portfolio.allocate_failed", symbol=event.plan.symbol)

    async def run(self, ctx: AgentContext) -> None:
        self._logger.warning(
            "portfolio.run_standalone", detail="Portfolio agent is event-driven only"
        )
