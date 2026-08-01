"""Domain entities â€” pure business objects with no ORM/IO knowledge.

Repositories (infrastructure) are responsible for mapping these to/from
persistence; entities never import SQLAlchemy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from qtrader.domain.value_objects import (
    Interval,
    Money,
    OrderStatus,
    OrderType,
    PositionStatus,
    SignalType,
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


@dataclass(frozen=True, slots=True)
class Signal:
    """A composite, persisted analysis signal produced by an agent."""

    symbol: str
    agent: str
    signal_type: SignalType
    score: Decimal
    interval: Interval | None = None
    strength: Decimal | None = None
    horizon: str | None = None
    metadata: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=_now)
    signal_id: int | None = None


@dataclass(frozen=True, slots=True)
class IndicatorSnapshot:
    """One row of indicator values for a (symbol, interval, ts) point."""

    symbol: str
    interval: Interval
    ts: datetime
    rsi: Decimal | None = None
    ema_9: Decimal | None = None
    ema_21: Decimal | None = None
    sma_50: Decimal | None = None
    sma_200: Decimal | None = None
    macd: Decimal | None = None
    macd_signal: Decimal | None = None
    macd_hist: Decimal | None = None
    atr: Decimal | None = None
    vwap: Decimal | None = None
    boll_upper: Decimal | None = None
    boll_middle: Decimal | None = None
    boll_lower: Decimal | None = None
    adx: Decimal | None = None
    stoch_k: Decimal | None = None
    stoch_d: Decimal | None = None
    ichimoku_tenkan: Decimal | None = None
    ichimoku_kijun: Decimal | None = None
    ichimoku_senkou_a: Decimal | None = None
    ichimoku_senkou_b: Decimal | None = None
    ichimoku_chikou: Decimal | None = None
    volume_profile: dict | None = None
    extras: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NewsItem:
    """A single news article/event with optional LLM analysis."""

    symbol: str | None
    source: str | None
    title: str
    url: str
    published_at: datetime
    content: str | None = None
    categories: list[str] | None = None
    sentiment_score: Decimal | None = None
    summary: str | None = None
    expected_market_impact: str | None = None
    impact_direction: int | None = None
    analysis_confidence: Decimal | None = None
    analyzed_at: datetime | None = None
    metadata: dict = field(default_factory=dict)
    news_id: int | None = None


@dataclass(frozen=True, slots=True)
class FundamentalData:
    """Latest fundamentals for a symbol/period."""

    symbol: str
    period: str
    report_date: date | None = None
    revenue: Decimal | None = None
    eps: Decimal | None = None
    pe_ratio: Decimal | None = None
    debt_total: Decimal | None = None
    cash_flow: Decimal | None = None
    roe: Decimal | None = None
    roa: Decimal | None = None
    gross_margin: Decimal | None = None
    operating_margin: Decimal | None = None
    net_margin: Decimal | None = None
    revenue_growth: Decimal | None = None
    earnings_growth: Decimal | None = None
    price_to_book: Decimal | None = None
    fundamental_id: int | None = None
