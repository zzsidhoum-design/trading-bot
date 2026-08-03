"""Execution Agent — submits risk-approved allocations to the broker
(docs/02-agents.md Â§9).

Consumes ``AllocationProposal``, resolves the stock and order, submits through
the injected :class:`BrokerGateway`, then polls the fill status. On fill it
updates the position, portfolio cash and order lifecycle, records a closed
``Trade`` (Memory) on exits and publishes the outbox events:
``OrderSubmitted`` / ``OrderFilled`` / ``OrderStatusChanged`` / ``PositionClosed``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import ClassVar, cast

from qtrader.application.agents.base import AgentBase, AgentContext
from qtrader.application.services.portfolio_service import PortfolioService
from qtrader.application.services.system_gate import SystemGate
from qtrader.domain.entities import Order, Position, Stock, Trade
from qtrader.domain.events import (
    AllocationProposal,
    DomainEvent,
    OrderFilled,
    OrderStatusChanged,
    OrderSubmitted,
    PositionClosed,
)
from qtrader.domain.ports import (
    BrokerGateway,
    EventBus,
    OrderRepository,
    PortfolioRepository,
    PositionRepository,
    StockRepository,
    TradeRepository,
    UnitOfWork,
    UnitOfWorkFactory,
)
from qtrader.domain.value_objects import (
    Money,
    OrderFill,
    OrderStatus,
    OrderType,
    PositionStatus,
    TradeSide,
)


class ExecutionAgent(AgentBase):
    name: ClassVar[str] = "execution"
    consumes: ClassVar[tuple[type[DomainEvent], ...]] = (AllocationProposal,)
    produces: ClassVar[tuple[type[DomainEvent], ...]] = (
        OrderSubmitted,
        OrderFilled,
        OrderStatusChanged,
        PositionClosed,
    )

    def __init__(
        self,
        broker: BrokerGateway,
        portfolio_service: PortfolioService,
        portfolios: PortfolioRepository,
        positions: PositionRepository,
        orders: OrderRepository,
        stocks: StockRepository,
        trades: TradeRepository,
        bus: EventBus,
        gate: SystemGate | None = None,
        gate_strategy: str = "ensemble",
        uow_factory: UnitOfWorkFactory | None = None,
    ) -> None:
        self._broker = broker
        self._portfolios = portfolio_service
        self._portfolios_repo = portfolios
        self._positions = positions
        self._orders = orders
        self._stocks = stocks
        self._trades = trades
        self._bus = bus
        self._gate = gate
        self._gate_strategy = gate_strategy
        self._uow_factory = uow_factory

    @asynccontextmanager
    async def _trading_scope(
        self,
    ) -> AsyncIterator[tuple[UnitOfWork | None, PortfolioService]]:
        """Transaction scope for multi-repo writes; falls back to standalone
        repositories when no UnitOfWork factory is injected."""
        if self._uow_factory is not None:
            async with self._uow_factory() as uow:
                yield uow, self._portfolios.bind(uow.portfolios)
        else:
            yield None, self._portfolios

    async def execute(self, proposal: AllocationProposal) -> str | None:
        order = await self._resolve_order(proposal)
        if order is None:
            return None
        return await self.execute_order(order)

    async def execute_order(self, order: Order) -> str | None:
        if not order.is_open:
            return None

        if self._gate is not None and not await self._gate.can_trade(
            self._gate_strategy, order.mode
        ):
            self._logger.warning(
                "execution.gate_denied",
                symbol=order.symbol,
                mode=order.mode.value,
                strategy=self._gate_strategy,
            )
            denied = replace(order, status=OrderStatus.REJECTED)
            await self._orders.save(denied)
            await self._bus.publish(
                OrderStatusChanged(
                    order_id=str(denied.order_id),
                    status=OrderStatus.REJECTED,
                    detail=f"SystemGate denied {order.mode.value} trading",
                )
            )
            return None

        try:
            broker_order_id = await self._broker.submit_order(order)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("execution.rejected", symbol=order.symbol, error=str(exc))
            rejected = replace(order, status=OrderStatus.REJECTED)
            await self._orders.save(rejected)
            await self._bus.publish(
                OrderStatusChanged(
                    order_id=str(rejected.order_id),
                    status=OrderStatus.REJECTED,
                    detail=str(exc),
                )
            )
            return None

        submitted = replace(order, broker_order_id=broker_order_id, status=OrderStatus.SUBMITTED)
        await self._orders.save(submitted)
        await self._bus.publish(
            OrderSubmitted(
                order_id=str(submitted.order_id),
                symbol=submitted.symbol or "",
                side=submitted.side,
                quantity=str(submitted.quantity),
                order_type=submitted.order_type.value,
                mode=submitted.mode,
            )
        )

        try:
            fill = await self._broker.get_order_status(broker_order_id)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                "execution.poll_failed", broker_order_id=broker_order_id, error=str(exc)
            )
            failed = replace(submitted, status=OrderStatus.REJECTED)
            await self._orders.save(failed)
            await self._bus.publish(
                OrderStatusChanged(
                    order_id=str(failed.order_id),
                    status=OrderStatus.REJECTED,
                    detail=str(exc),
                )
            )
            return broker_order_id

        if fill.status is OrderStatus.FILLED:
            await self._apply_fill(submitted, fill)
        elif fill.status is OrderStatus.REJECTED:
            failed = replace(submitted, status=OrderStatus.REJECTED)
            await self._orders.save(failed)
            await self._bus.publish(
                OrderStatusChanged(order_id=str(failed.order_id), status=OrderStatus.REJECTED)
            )
        return broker_order_id

    async def _resolve_order(self, proposal: AllocationProposal) -> Order | None:
        async with self._trading_scope() as (uow, portfolio_service):
            orders = uow.orders if uow is not None else self._orders
            stocks = uow.stocks if uow is not None else self._stocks
            order = await orders.get_by_idempotency_key(
                f"{proposal.decision_uuid}:{proposal.order_id}"
            )
            if order is not None:
                return cast(Order, order)
            portfolio = await portfolio_service.default_portfolio()
            portfolio_id = portfolio.portfolio_id
            assert portfolio_id is not None
            stock = await stocks.get_by_symbol(proposal.symbol)
            if stock is None:
                stock = await stocks.upsert(
                    Stock(symbol=proposal.symbol, exchange="PAPER", name=proposal.symbol)
                )
            created = await orders.create(
                Order(
                    portfolio_id=portfolio_id,
                    stock_id=stock.stock_id or 0,
                    side=proposal.side,
                    order_type=OrderType(proposal.order_type),
                    quantity=int(Decimal(proposal.quantity)),
                    mode=proposal.mode,
                    idempotency_key=f"{proposal.decision_uuid}:{proposal.order_id}",
                    limit_price=None,
                    stop_loss=Money(proposal.stop_loss) if proposal.stop_loss else None,
                    take_profit=Money(proposal.take_profit) if proposal.take_profit else None,
                    decision_ref=proposal.decision_uuid,
                    reason={"proposal": True},
                    symbol=proposal.symbol,
                    status=OrderStatus.PENDING,
                )
            )
            return cast(Order, created)

    async def _apply_fill(self, order: Order, fill: OrderFill) -> None:
        async with self._trading_scope() as (uow, portfolio_service):
            await self._apply_fill_inner(order, fill, uow, portfolio_service)

    async def _apply_fill_inner(
        self,
        order: Order,
        fill: OrderFill,
        uow: UnitOfWork | None,
        portfolio_service: PortfolioService,
    ) -> None:
        orders = uow.orders if uow is not None else self._orders
        filled = replace(
            order,
            status=OrderStatus.FILLED,
            filled_qty=int(fill.filled_qty),
            avg_fill_price=Money(fill.avg_fill_price),
            commission=Money(fill.commission),
        )
        await orders.save(filled)
        await self._bus.publish(
            OrderFilled(
                order_id=str(filled.order_id),
                broker_order_id=fill.broker_order_id,
                fill_price=str(fill.avg_fill_price),
                fill_qty=str(fill.filled_qty),
                fees=str(fill.commission),
            )
        )
        await self._apply_position_inner(filled, fill, uow, portfolio_service)

    async def _apply_position_inner(
        self,
        order: Order,
        fill: OrderFill,
        uow: UnitOfWork | None,
        portfolio_service: PortfolioService,
    ) -> None:
        portfolios_repo = uow.portfolios if uow is not None else self._portfolios_repo
        positions = uow.positions if uow is not None else self._positions
        trades = uow.trades if uow is not None else self._trades

        portfolio = await portfolio_service.default_portfolio()
        portfolio_id = portfolio.portfolio_id
        assert portfolio_id is not None

        position = await self._find_position(portfolio_id, order.symbol or "", positions)
        fill_qty = Decimal(fill.filled_qty)
        fill_price = Decimal(fill.avg_fill_price)
        fees = Decimal(fill.commission)
        notional = fill_qty * fill_price

        if order.side is TradeSide.BUY:
            if position is None:
                position = Position(
                    portfolio_id=portfolio_id,
                    stock_id=order.stock_id,
                    quantity=int(fill_qty),
                    avg_entry_price=Money(fill_price),
                    stop_loss=order.stop_loss,
                    take_profit=order.take_profit,
                    symbol=order.symbol,
                )
            else:
                old_qty = Decimal(position.quantity)
                total_qty = old_qty + fill_qty
                avg = (old_qty * position.avg_entry_price.amount + notional) / total_qty
                position = replace(
                    position,
                    quantity=int(total_qty),
                    avg_entry_price=Money(avg),
                    stop_loss=order.stop_loss,
                    take_profit=order.take_profit,
                )
            await positions.save(position)
            new_cash = portfolio.current_cash.amount - notional - fees
        else:
            await self._close_position_inner(
                position, order, fill_qty, fill_price, fees, positions, trades
            )
            new_cash = portfolio.current_cash.amount + notional - fees

        await portfolios_repo.save(replace(portfolio, current_cash=Money(new_cash)))

    async def _close_position_inner(
        self,
        position: Position | None,
        order: Order,
        fill_qty: Decimal,
        fill_price: Decimal,
        fees: Decimal,
        positions: PositionRepository,
        trades: TradeRepository,
    ) -> None:
        if position is None:
            self._logger.warning("execution.close_missing_position", symbol=order.symbol)
            return
        remaining = Decimal(position.quantity) - fill_qty
        if remaining > 0:
            await positions.save(replace(position, quantity=int(remaining)))
            return

        cost = position.avg_entry_price.amount * Decimal(position.quantity)
        realized = (
            (fill_price - position.avg_entry_price.amount) * Decimal(position.quantity) - fees
        )
        closed = replace(
            position,
            quantity=0,
            status=PositionStatus.CLOSED,
            realized_pnl=Money(realized),
            closed_at=datetime.now(UTC),
        )
        await positions.save(closed)
        await trades.record(
            Trade(
                portfolio_id=position.portfolio_id,
                stock_id=position.stock_id,
                symbol=position.symbol,
                strategy="default",
                side=TradeSide.SELL,
                quantity=Decimal(position.quantity),
                entry_price=position.avg_entry_price.amount,
                exit_price=fill_price,
                pnl=realized,
                pnl_pct=realized / cost if cost > 0 else Decimal(0),
                fees=fees,
                decision_reason=order.reason,
                outcome="closed",
                mode=order.mode,
                position_id=position.position_id,
            )
        )
        await self._bus.publish(
            PositionClosed(
                position_id=str(position.position_id),
                symbol=position.symbol or order.symbol or "",
                pnl=str(realized),
                pnl_pct=str(realized / cost if cost > 0 else Decimal(0)),
            )
        )

    async def _find_position(
        self,
        portfolio_id: int,
        symbol: str,
        positions: PositionRepository | None = None,
    ) -> Position | None:
        repo = positions if positions is not None else self._positions
        for pos in await repo.open_positions(portfolio_id):
            if pos.symbol == symbol:
                return cast(Position, pos)
        return None

    async def on_event(self, event: DomainEvent) -> None:
        if isinstance(event, AllocationProposal):
            try:
                await self.execute(event)
            except Exception:
                self._logger.exception("execution.execute_failed", symbol=event.symbol)

    async def run(self, ctx: AgentContext) -> None:
        self._logger.warning(
            "execution.run_standalone", detail="Execution agent is event-driven only"
        )
