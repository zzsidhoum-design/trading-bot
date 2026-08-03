"""Unit tests for the Paper broker."""

from __future__ import annotations

from decimal import Decimal

import pytest

from qtrader.domain.entities import Order
from qtrader.domain.exceptions import NotFoundError
from qtrader.domain.value_objects import (
    Money,
    OrderStatus,
    OrderType,
    TradeSide,
    TradingMode,
)
from qtrader.infrastructure.brokers.paper import PaperBroker
from tests.unit.fakes_phase5 import FakePriceRepository


def _order(close: str = "100", limit: str | None = None) -> Order:
    return Order(
        portfolio_id=1,
        stock_id=1,
        side=TradeSide.BUY,
        order_type=OrderType.LIMIT if limit else OrderType.MARKET,
        quantity=10,
        mode=TradingMode.PAPER,
        idempotency_key="k-1",
        limit_price=Money(limit) if limit else None,
        symbol="AAPL",
    )


async def test_market_order_fills_at_latest_price() -> None:
    broker = PaperBroker(prices=FakePriceRepository(close="105"))
    broker_order_id = await broker.submit_order(_order())
    assert broker_order_id.startswith("paper-")
    fill = await broker.get_order_status(broker_order_id)
    assert fill.status is OrderStatus.FILLED
    assert fill.filled_qty == Decimal("10")
    assert fill.avg_fill_price == Decimal("105")


async def test_limit_order_fills_at_limit() -> None:
    broker = PaperBroker(prices=FakePriceRepository(close="105"))
    broker_order_id = await broker.submit_order(_order(limit="99"))
    fill = await broker.get_order_status(broker_order_id)
    assert fill.avg_fill_price == Decimal("99")


async def test_fills_at_default_price_without_price_source() -> None:
    broker = PaperBroker(default_price=Decimal("50"))
    broker_order_id = await broker.submit_order(_order())
    fill = await broker.get_order_status(broker_order_id)
    assert fill.avg_fill_price == Decimal("50")


async def test_unknown_order_raises_typed_not_found() -> None:
    broker = PaperBroker()
    with pytest.raises(NotFoundError, match="unknown broker order"):
        await broker.get_order_status("paper-missing")


async def test_cancelled_order_is_not_found() -> None:
    broker = PaperBroker(default_price=Decimal("50"))
    broker_order_id = await broker.submit_order(_order())
    await broker.cancel_order(broker_order_id)
    with pytest.raises(NotFoundError, match="unknown broker order"):
        await broker.get_order_status(broker_order_id)
