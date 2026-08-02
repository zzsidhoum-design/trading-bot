"""Paper broker — instant fills at the last known price (or the limit)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from qtrader.domain.entities import Order
from qtrader.domain.ports import BrokerGateway, PriceRepository
from qtrader.domain.value_objects import Interval, OrderFill, OrderStatus, OrderType


class PaperBroker(BrokerGateway):
    def __init__(
        self,
        prices: PriceRepository | None = None,
        *,
        default_price: Decimal = Decimal("100"),
    ) -> None:
        self._prices = prices
        self._default_price = default_price
        self._orders: dict[str, Order] = {}

    async def submit_order(self, order: Order) -> str:
        broker_order_id = f"paper-{uuid.uuid4()}"
        self._orders[broker_order_id] = order
        return broker_order_id

    async def cancel_order(self, broker_order_id: str) -> None:
        self._orders.pop(broker_order_id, None)

    async def close(self) -> None:
        return None

    async def modify_brackets(
        self, position_id: str, stop_loss: object, take_profit: object
    ) -> None:
        return None

    async def get_order_status(self, broker_order_id: str) -> OrderFill:
        order = self._orders[broker_order_id]
        price = await self._fill_price(order)
        return OrderFill(
            broker_order_id=broker_order_id,
            status=OrderStatus.FILLED,
            filled_qty=Decimal(order.quantity),
            avg_fill_price=price,
            commission=Decimal("0"),
        )

    async def _fill_price(self, order: Order) -> Decimal:
        if order.order_type is OrderType.LIMIT and order.limit_price is not None:
            return order.limit_price.amount
        if self._prices is not None and order.symbol is not None:
            bar = await self._prices.latest(order.symbol, Interval.D1)
            if bar is not None:
                return bar.close
        if order.limit_price is not None:
            return order.limit_price.amount
        return self._default_price
