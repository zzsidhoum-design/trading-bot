"""Paper broker — instant fills at the last known price (or the limit).

Brackets are honored at the broker: a BUY MARKET order carrying a stop-loss /
take-profit registers a child SELL STOP order, and ``get_order_status`` on a
stop order simulates the trigger against the last known price (fill through
the stop on a break, at the take-profit on a target hit).
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from decimal import Decimal

from qtrader.domain.entities import Order
from qtrader.domain.exceptions import NotFoundError
from qtrader.domain.ports import BrokerGateway, PriceRepository
from qtrader.domain.value_objects import (
    Interval,
    OrderFill,
    OrderStatus,
    OrderType,
    TradeSide,
)


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
        if (
            order.side is TradeSide.BUY
            and order.order_type is OrderType.MARKET
            and order.quantity > 0
            and (order.stop_loss is not None or order.take_profit is not None)
        ):
            stop = replace(
                order,
                side=TradeSide.SELL,
                order_type=OrderType.STOP,
                status=OrderStatus.SUBMITTED,
                stop_price=order.stop_loss,
            )
            self._orders[f"{broker_order_id}-stop"] = stop
        return broker_order_id

    async def cancel_order(self, broker_order_id: str) -> None:
        self._orders.pop(broker_order_id, None)
        self._orders.pop(f"{broker_order_id}-stop", None)

    async def close(self) -> None:
        return None

    async def modify_brackets(
        self, position_id: str, stop_loss: object, take_profit: object
    ) -> None:
        return None

    async def get_order_status(self, broker_order_id: str) -> OrderFill:
        order = self._orders.get(broker_order_id)
        if order is None:
            raise NotFoundError(f"unknown broker order {broker_order_id}")
        if order.order_type is OrderType.STOP:
            return await self._stop_order_status(order)
        price = await self._fill_price(order)
        return OrderFill(
            broker_order_id=broker_order_id,
            status=OrderStatus.FILLED,
            filled_qty=Decimal(order.quantity),
            avg_fill_price=price,
            commission=Decimal("0"),
        )

    async def _stop_order_status(self, order: Order) -> OrderFill:
        price = await self._fill_price(order)
        if order.stop_price is not None and price <= order.stop_price.amount:
            fill_price = price
        elif order.take_profit is not None and price >= order.take_profit.amount:
            fill_price = order.take_profit.amount
        else:
            return OrderFill(
                broker_order_id=order.broker_order_id or "paper-stop",
                status=OrderStatus.PENDING,
                filled_qty=Decimal(0),
                avg_fill_price=price,
                commission=Decimal("0"),
            )
        return OrderFill(
            broker_order_id=order.broker_order_id or "paper-stop",
            status=OrderStatus.FILLED,
            filled_qty=Decimal(order.quantity),
            avg_fill_price=fill_price,
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
