"""Portfolio, position, order & trade tables."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from qtrader.infrastructure.database.base import Base, utcnow


class PortfolioModel(Base):
    __tablename__ = "portfolios"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    initial_capital: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    current_cash: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class PositionModel(Base):
    __tablename__ = "positions"
    __table_args__ = (Index("ix_positions_portfolio_status", "portfolio_id", "status"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("portfolios.id"), nullable=False
    )
    stock_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stocks.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    avg_entry_price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OrderModel(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_orders_idempotency_key"),
        Index("ix_orders_portfolio_status", "portfolio_id", "status"),
        Index("ix_orders_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(36), nullable=False)
    portfolio_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("portfolios.id"), nullable=False
    )
    stock_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stocks.id"), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    order_type: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    broker_order_id: Mapped[str | None] = mapped_column(String(64))
    filled_qty: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0)
    avg_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    commission: Mapped[Decimal] = mapped_column(Numeric(14, 6), default=0)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    decision_ref: Mapped[str | None] = mapped_column(String(36))
    reason_json: Mapped[dict | None] = mapped_column("reason", JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TradeModel(Base):
    __tablename__ = "trades"
    __table_args__ = (
        Index("ix_trades_portfolio_exit", "portfolio_id", "exit_time"),
        Index("ix_trades_strategy", "strategy"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    position_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("positions.id"))
    portfolio_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("portfolios.id"), nullable=False
    )
    stock_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stocks.id"), nullable=False)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    exit_price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    pnl: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    pnl_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    fees: Mapped[Decimal] = mapped_column(Numeric(14, 6), default=0)
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision_reason_json: Mapped[dict | None] = mapped_column("decision_reason", JSONB)
    outcome: Mapped[str | None] = mapped_column(String(16))
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
