"""Domain entities â€” pure business objects with no ORM/IO knowledge.

Repositories (infrastructure) are responsible for mapping these to/from
persistence; entities never import SQLAlchemy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from qtrader.domain.value_objects import (
    Money,
    OrderStatus,
    OrderType,
    PositionStatus,
    TradeSide,
    TradingMode,
)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Stock:
    symbol: str
    exchange: str
    name: str | None = None
    currency: str = "USD"
    sector: str | None = None
    industry: str | None = None
    market_cap: Money | None = None
    is_active: bool = True
    stock_id: int | None = None


@dataclass(frozen=True, slots=True)
class Portfolio:
    name: str
    currency: str = "USD"
    initial_capital: Money = Money(0)
    current_cash: Money = Money(0)
    mode: TradingMode = TradingMode.BACKTEST
    status: str = "active"
    portfolio_id: int | None = None


@dataclass(frozen=True, slots=True)
class Position:
    portfolio_id: int
    stock_id: int
    quantity: int
    avg_entry_price: Money
    status: PositionStatus = PositionStatus.OPEN
    stop_loss: Money | None = None
    take_profit: Money | None = None
    realized_pnl: Money | None = None
    opened_at: datetime = field(default_factory=_now)
    closed_at: datetime | None = None
    position_id: int | None = None


@dataclass(frozen=True, slots=True)
class Order:
    portfolio_id: int
    stock_id: int
    side: TradeSide
    order_type: OrderType
    quantity: int
    mode: TradingMode
    idempotency_key: str
    limit_price: Money | None = None
    stop_price: Money | None = None
    status: OrderStatus = OrderStatus.PENDING
    broker_order_id: str | None = None
    filled_qty: int = 0
    avg_fill_price: Money | None = None
    commission: Money = Money(0)
    decision_ref: str | None = None
    reason: dict | None = None
    created_at: datetime = field(default_factory=_now)
    order_id: int | None = None

    @property
    def is_open(self) -> bool:
        return self.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.PARTIAL)
