"""Value objects — immutable primitives shared across the domain.

Nothing in this module may import from infrastructure or application layers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum

MONEY_QUANT = Decimal("0.000001")
PRICE_QUANT = Decimal("0.000001")
PCT_QUANT = Decimal("0.0001")


class _UnexpectedType(TypeError):
    pass


def _as_decimal(value: Decimal | int | float | str, label: str, quant: Decimal) -> Decimal:
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise _UnexpectedType(f"{label} must be numeric, got {value!r}") from exc
    return dec.quantize(quant)


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class TradingMode(StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class Interval(StrEnum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    D1 = "1d"


class TradeSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


class PositionStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class SignalType(StrEnum):
    STRONG_BUY = "STRONG_BUY"
    BUY = "BUY"
    NEUTRAL = "NEUTRAL"
    HOLD = "HOLD"
    SELL = "SELL"
    STRONG_SELL = "STRONG_SELL"


class Decision(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class MarketImpact(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


# --------------------------------------------------------------------------- #
# Money & percentages
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Money:
    """A fixed-precision monetary amount (no float money)."""

    amount: Decimal

    def __init__(self, amount: Decimal | int | float | str) -> None:
        object.__setattr__(self, "amount", _as_decimal(amount, "amount", MONEY_QUANT))

    def __add__(self, other: Money) -> Money:
        return Money(self.amount + other.amount)

    def __sub__(self, other: Money) -> Money:
        return Money(self.amount - other.amount)

    def __mul__(self, factor: Decimal | int | float) -> Money:
        return Money(self.amount * Decimal(str(factor)))

    def __neg__(self) -> Money:
        return Money(-self.amount)

    def __repr__(self) -> str:
        return f"Money({self.amount})"


@dataclass(frozen=True, slots=True)
class Percentage:
    """Ratio in [0, 1] (e.g. 0.02 == 2%)."""

    value: Decimal

    def __init__(self, value: Decimal | int | float | str) -> None:
        object.__setattr__(self, "value", _as_decimal(value, "percentage", PCT_QUANT))
        if not (Decimal(0) <= self.value <= Decimal(1)):
            raise ValueError(f"Percentage must be in [0, 1], got {self.value}")

    @classmethod
    def from_basis_points(cls, bps: Decimal | int | float) -> Percentage:
        return cls(Decimal(str(bps)) / Decimal(10000))

    def __repr__(self) -> str:
        return f"Percentage({self.value})"


# --------------------------------------------------------------------------- #
# Market primitives
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PriceBar:
    """A single OHLCV bar in UTC."""

    symbol: str
    interval: Interval
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None:
            object.__setattr__(self, "ts", self.ts.replace(tzinfo=UTC))
        if not (self.high >= self.open and self.high >= self.low and self.high >= self.close):
            raise ValueError(f"Invalid OHLC bar for {self.symbol}: high < other prices")
        if self.low < 0 or self.open < 0 or self.close < 0:
            raise ValueError(f"Negative price for {self.symbol}")
        if self.volume < 0:
            raise ValueError(f"Negative volume for {self.symbol}")


@dataclass(frozen=True, slots=True)
class OrderPlan:
    """Risk-approved execution plan produced by the Risk Manager."""

    symbol: str
    side: TradeSide
    quantity: Decimal
    order_type: OrderType
    limit_price: Decimal | None
    stop_loss: Decimal
    take_profit: Decimal
    risk_per_trade: Percentage
    estimated_exposure: Percentage
    entry_price: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("OrderPlan quantity must be positive")


@dataclass(frozen=True, slots=True)
class OrderFill:
    """Broker result returned by ``BrokerGateway.get_order_status``."""

    broker_order_id: str
    status: OrderStatus
    filled_qty: Decimal
    avg_fill_price: Decimal
    commission: Decimal = Decimal("0")
