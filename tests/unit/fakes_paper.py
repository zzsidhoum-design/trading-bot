"""Shared fakes for Phase 7 (paper-trading) tests (not collected by pytest)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from qtrader.domain.entities import AgentMetric, Order, SystemLog
from qtrader.domain.ports import (
    AgentMetricRepository,
    BrokerGateway,
    PriceRepository,
    SystemLogRepository,
)
from qtrader.domain.value_objects import (
    Interval,
    Money,
    OrderFill,
    OrderStatus,
    OrderType,
    PriceBar,
    TradeSide,
    TradingMode,
)


class FakePriceRepository(PriceRepository):
    def __init__(self, prices: dict[str, str] | None = None) -> None:
        self._prices = {k: Decimal(v) for k, v in (prices or {}).items()}

    async def upsert_bars(self, bars: list[PriceBar]) -> int:
        return len(bars)

    async def history(
        self,
        symbol: str,
        interval: Interval,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> list[PriceBar]:
        bar = await self.latest(symbol, interval)
        return [bar] if bar is not None else []

    async def latest(self, symbol: str, interval: Interval) -> PriceBar | None:
        price = self._prices.get(symbol)
        if price is None:
            return None
        return PriceBar(
            symbol=symbol,
            interval=interval,
            ts=datetime.now(UTC),
            open=price,
            high=price,
            low=price,
            close=price,
            volume=Decimal("1000000"),
        )


class FakeBroker(BrokerGateway):
    """Configurable broker: instant fill at a price, or a rejection."""

    def __init__(
        self,
        *,
        fill_price: str = "101.25",
        reject: Exception | None = None,
        submit_latency_ms: float = 2.5,
    ) -> None:
        self.fill_price = Decimal(fill_price)
        self.reject = reject
        self.submit_latency_ms = submit_latency_ms
        self.submitted: list[Order] = []
        self.canceled: list[str] = []
        self.status_queries: list[str] = []

    async def submit_order(self, order: Order) -> str:
        self.submitted.append(order)
        if self.reject is not None:
            raise self.reject
        return f"fake-{len(self.submitted)}"

    async def cancel_order(self, broker_order_id: str) -> None:
        self.canceled.append(broker_order_id)

    async def modify_brackets(
        self, position_id: str, stop_loss: object, take_profit: object
    ) -> None:
        return None

    async def get_order_status(self, broker_order_id: str) -> OrderFill:
        self.status_queries.append(broker_order_id)
        return OrderFill(
            broker_order_id=broker_order_id,
            status=OrderStatus.FILLED,
            filled_qty=Decimal("10"),
            avg_fill_price=self.fill_price,
            commission=Decimal("0"),
        )

    async def close(self) -> None:
        return None


class FakeRejectingBroker(BrokerGateway):
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error or RuntimeError("rejected by broker")

    async def submit_order(self, order: Order) -> str:
        raise self.error

    async def cancel_order(self, broker_order_id: str) -> None:
        return None

    async def modify_brackets(
        self, position_id: str, stop_loss: object, take_profit: object
    ) -> None:
        return None

    async def get_order_status(self, broker_order_id: str) -> OrderFill:
        raise self.error

    async def close(self) -> None:
        return None


class FakeAgentMetricRepository(AgentMetricRepository):
    def __init__(self) -> None:
        self.metrics: list[AgentMetric] = []

    async def record(self, metric: AgentMetric) -> AgentMetric:
        self.metrics.append(metric)
        return metric


class FakeSystemLogRepository(SystemLogRepository):
    def __init__(self) -> None:
        self.logs: list[SystemLog] = []

    async def record(self, entry: SystemLog) -> SystemLog:
        self.logs.append(entry)
        return entry

    async def recent(
        self,
        level: str | None = None,
        component: str | None = None,
        limit: int = 50,
    ) -> list[SystemLog]:
        filtered = self.logs
        if level is not None:
            filtered = [e for e in filtered if e.level == level]
        if component is not None:
            filtered = [e for e in filtered if e.component == component]
        return filtered[-limit:]


def make_order(
    symbol: str = "AAPL",
    *,
    side: TradeSide = TradeSide.BUY,
    quantity: int = 10,
    decision_ref: str | None = "dec-1",
    idempotency_key: str | None = "idem-1",
    limit_price: str | None = None,
    reason: dict[str, Any] | None = None,
) -> Order:
    return Order(
        portfolio_id=1,
        stock_id=1,
        side=side,
        order_type=OrderType.LIMIT if limit_price else OrderType.MARKET,
        quantity=quantity,
        mode=TradingMode.PAPER,
        idempotency_key=idempotency_key or "",
        limit_price=Money(Decimal(limit_price)) if limit_price else None,
        decision_ref=decision_ref,
        symbol=symbol,
        reason=reason,
    )


__all__ = [
    "FakeAgentMetricRepository",
    "FakeBroker",
    "FakePriceRepository",
    "FakeRejectingBroker",
    "FakeSystemLogRepository",
    "make_order",
]
