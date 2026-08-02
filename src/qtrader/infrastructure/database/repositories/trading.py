"""SQLAlchemy repositories for the trading lifecycle: positions, orders,
risk history and trades."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from qtrader.domain.entities import Order, Position, RiskAssessment, Trade
from qtrader.domain.ports import (
    OrderRepository,
    PositionRepository,
    RiskRepository,
    TradeRepository,
)
from qtrader.domain.value_objects import (
    Money,
    OrderStatus,
    OrderType,
    PositionStatus,
    TradeSide,
    TradingMode,
)
from qtrader.infrastructure.database.models import (
    OrderModel,
    PositionModel,
    RiskHistoryModel,
    StockModel,
    TradeModel,
)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class SQLAlchemyPositionRepository(PositionRepository):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def open_positions(self, portfolio_id: int) -> list[Position]:
        async with self._session_factory() as session:
            stmt = (
                select(PositionModel, StockModel.symbol)
                .join(StockModel, PositionModel.stock_id == StockModel.id)
                .where(
                    PositionModel.portfolio_id == portfolio_id,
                    PositionModel.status == PositionStatus.OPEN.value,
                )
            )
            rows = await session.execute(stmt)
            return [self._to_domain(row, symbol) for row, symbol in rows]

    async def save(self, position: Position) -> Position:
        async with self._session_factory() as session:
            if position.position_id is not None:
                row = await session.get(PositionModel, position.position_id)
                if row is None:
                    raise ValueError(f"position {position.position_id} not found")
                self._update_row(row, position)
            else:
                row = PositionModel(
                    portfolio_id=position.portfolio_id,
                    stock_id=position.stock_id,
                    status=position.status.value,
                    quantity=Decimal(position.quantity),
                    avg_entry_price=position.avg_entry_price.amount,
                    stop_loss=position.stop_loss.amount if position.stop_loss else None,
                    take_profit=position.take_profit.amount if position.take_profit else None,
                    realized_pnl=position.realized_pnl.amount if position.realized_pnl else None,
                    opened_at=position.opened_at,
                    closed_at=position.closed_at,
                )
                session.add(row)
            await session.commit()
            return Position(
                portfolio_id=row.portfolio_id,
                stock_id=row.stock_id,
                quantity=int(row.quantity),
                avg_entry_price=Money(row.avg_entry_price),
                status=PositionStatus(row.status),
                stop_loss=Money(row.stop_loss) if row.stop_loss is not None else None,
                take_profit=Money(row.take_profit) if row.take_profit is not None else None,
                realized_pnl=Money(row.realized_pnl) if row.realized_pnl is not None else None,
                opened_at=_utc(row.opened_at),
                closed_at=_utc(row.closed_at) if row.closed_at else None,
                symbol=position.symbol,
                position_id=row.id,
            )

    @staticmethod
    def _update_row(row: PositionModel, position: Position) -> None:
        row.portfolio_id = position.portfolio_id
        row.stock_id = position.stock_id
        row.status = position.status.value
        row.quantity = Decimal(position.quantity)
        row.avg_entry_price = position.avg_entry_price.amount
        row.stop_loss = position.stop_loss.amount if position.stop_loss else None
        row.take_profit = position.take_profit.amount if position.take_profit else None
        row.realized_pnl = position.realized_pnl.amount if position.realized_pnl else None
        row.closed_at = position.closed_at

    @staticmethod
    def _to_domain(row: PositionModel, symbol: str) -> Position:
        return Position(
            portfolio_id=row.portfolio_id,
            stock_id=row.stock_id,
            quantity=int(row.quantity),
            avg_entry_price=Money(row.avg_entry_price),
            status=PositionStatus(row.status),
            stop_loss=Money(row.stop_loss) if row.stop_loss is not None else None,
            take_profit=Money(row.take_profit) if row.take_profit is not None else None,
            realized_pnl=Money(row.realized_pnl) if row.realized_pnl is not None else None,
            opened_at=_utc(row.opened_at),
            closed_at=_utc(row.closed_at) if row.closed_at else None,
            symbol=symbol,
            position_id=row.id,
        )


class SQLAlchemyOrderRepository(OrderRepository):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(self, order: Order) -> Order:
        async with self._session_factory() as session:
            row = OrderModel(
                idempotency_key=order.idempotency_key,
                portfolio_id=order.portfolio_id,
                stock_id=order.stock_id,
                side=order.side.value,
                order_type=order.order_type.value,
                quantity=Decimal(order.quantity),
                limit_price=order.limit_price.amount if order.limit_price else None,
                stop_price=order.stop_price.amount if order.stop_price else None,
                stop_loss=order.stop_loss.amount if order.stop_loss is not None else None,
                take_profit=order.take_profit.amount if order.take_profit is not None else None,
                status=order.status.value,
                broker_order_id=order.broker_order_id,
                filled_qty=Decimal(order.filled_qty),
                avg_fill_price=order.avg_fill_price.amount if order.avg_fill_price else None,
                commission=order.commission.amount,
                mode=order.mode.value,
                decision_ref=order.decision_ref,
                reason_json=order.reason,
                created_at=order.created_at,
            )
            session.add(row)
            await session.commit()
            return self._to_domain(row, order.symbol or "")

    async def save(self, order: Order) -> Order:
        assert order.order_id is not None
        async with self._session_factory() as session:
            row = await session.get(OrderModel, order.order_id)
            if row is None:
                raise ValueError(f"order {order.order_id} not found")
            row.status = order.status.value
            row.broker_order_id = order.broker_order_id
            row.filled_qty = Decimal(order.filled_qty)
            row.avg_fill_price = order.avg_fill_price.amount if order.avg_fill_price else None
            row.commission = order.commission.amount
            row.stop_loss = order.stop_loss.amount if order.stop_loss is not None else None
            row.take_profit = order.take_profit.amount if order.take_profit is not None else None
            row.reason_json = order.reason
            await session.commit()
            return order

    async def get_by_idempotency_key(self, key: str) -> Order | None:
        async with self._session_factory() as session:
            stmt = (
                select(OrderModel, StockModel.symbol)
                .join(StockModel, OrderModel.stock_id == StockModel.id)
                .where(OrderModel.idempotency_key == key)
            )
            row = await session.execute(stmt)
            result = row.first()
            return self._to_domain(result[0], result[1]) if result else None

    async def list_by_portfolio(
        self, portfolio_id: int, status: Any | None = None, limit: int = 100
    ) -> list[Order]:
        async with self._session_factory() as session:
            stmt = (
                select(OrderModel, StockModel.symbol)
                .join(StockModel, OrderModel.stock_id == StockModel.id)
                .where(OrderModel.portfolio_id == portfolio_id)
                .order_by(OrderModel.created_at.desc())
                .limit(limit)
            )
            if status is not None:
                stmt = stmt.where(OrderModel.status == status)
            rows = await session.execute(stmt)
            return [self._to_domain(row, symbol) for row, symbol in rows]

    @staticmethod
    def _to_domain(row: OrderModel, symbol: str) -> Order:
        return Order(
            portfolio_id=row.portfolio_id,
            stock_id=row.stock_id,
            side=TradeSide(row.side),
            order_type=OrderType(row.order_type),
            quantity=int(row.quantity),
            mode=TradingMode(row.mode),
            idempotency_key=row.idempotency_key,
            limit_price=Money(row.limit_price) if row.limit_price is not None else None,
            stop_price=Money(row.stop_price) if row.stop_price is not None else None,
            stop_loss=Money(row.stop_loss) if row.stop_loss is not None else None,
            take_profit=Money(row.take_profit) if row.take_profit is not None else None,
            status=OrderStatus(row.status),
            broker_order_id=row.broker_order_id,
            filled_qty=int(row.filled_qty),
            avg_fill_price=Money(row.avg_fill_price) if row.avg_fill_price is not None else None,
            commission=Money(row.commission),
            decision_ref=row.decision_ref,
            reason=row.reason_json,
            created_at=_utc(row.created_at),
            symbol=symbol,
            order_id=row.id,
        )


class SQLAlchemyRiskRepository(RiskRepository):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(self, assessment: RiskAssessment) -> RiskAssessment:
        async with self._session_factory() as session:
            stock_id = await session.scalar(
                select(StockModel.id).where(StockModel.symbol == assessment.symbol).limit(1)
            )
            row = RiskHistoryModel(
                decision_uuid=assessment.decision_uuid or None,
                portfolio_id=assessment.portfolio_id,
                stock_id=stock_id,
                approved=assessment.approved,
                rejection_reason="\n".join(assessment.rejection_reasons) or None,
                position_size=assessment.position_size,
                stop_loss=assessment.stop_loss,
                take_profit=assessment.take_profit,
                risk_per_trade_pct=assessment.risk_per_trade_pct,
                exposure_pct=assessment.exposure_pct,
                max_daily_loss_pct=assessment.max_daily_loss_pct,
                daily_pnl_pct=assessment.daily_pnl_pct,
                metadata_json=assessment.metadata,
            )
            session.add(row)
            await session.commit()
            return self._to_domain(row, assessment.symbol)

    async def recent(self, limit: int = 50) -> list[RiskAssessment]:
        async with self._session_factory() as session:
            stmt = (
                select(RiskHistoryModel, StockModel.symbol)
                .join(StockModel, RiskHistoryModel.stock_id == StockModel.id)
                .order_by(RiskHistoryModel.created_at.desc())
                .limit(limit)
            )
            rows = await session.execute(stmt)
            return [self._to_domain(row, symbol) for row, symbol in rows]

    @staticmethod
    def _to_domain(row: RiskHistoryModel, symbol: str) -> RiskAssessment:
        return RiskAssessment(
            decision_uuid=row.decision_uuid or "",
            symbol=symbol,
            approved=row.approved,
            rejection_reasons=row.rejection_reason.split("\n") if row.rejection_reason else [],
            position_size=row.position_size,
            stop_loss=row.stop_loss,
            take_profit=row.take_profit,
            risk_per_trade_pct=row.risk_per_trade_pct,
            exposure_pct=row.exposure_pct,
            max_daily_loss_pct=row.max_daily_loss_pct,
            daily_pnl_pct=row.daily_pnl_pct,
            metadata=row.metadata_json or {},
            portfolio_id=row.portfolio_id,
            created_at=_utc(row.created_at),
            risk_id=row.id,
        )


class SQLAlchemyTradeRepository(TradeRepository):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(self, trade: Trade) -> Trade:
        async with self._session_factory() as session:
            row = TradeModel(
                position_id=trade.position_id,
                portfolio_id=trade.portfolio_id,
                stock_id=trade.stock_id,
                strategy=trade.strategy,
                side=trade.side.value,
                quantity=trade.quantity,
                entry_price=trade.entry_price,
                exit_price=trade.exit_price,
                pnl=trade.pnl,
                pnl_pct=trade.pnl_pct,
                fees=trade.fees,
                entry_time=trade.entry_time,
                exit_time=trade.exit_time,
                decision_reason_json=trade.decision_reason,
                outcome=trade.outcome,
                mode=trade.mode.value,
            )
            session.add(row)
            await session.commit()
            return cast(Trade, replace(trade, trade_id=row.id))
