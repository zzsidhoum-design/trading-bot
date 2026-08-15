"""Alpaca broker adapter (HTTP API via httpx).

Configuration comes from environment variables. The canonical names are the
project's ``ALPACA_API_KEY`` / ``ALPACA_SECRET_KEY`` / ``ALPACA_PAPER``
settings; the Alpaca-native ``APCA_API_KEY_ID`` / ``APCA_API_SECRET_KEY`` are
also honored for compatibility. Paper trading is used unless ``ALPACA_PAPER``
is ``false`` or ``ALPACA_LIVE=true``.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

import httpx

from qtrader.domain.entities import Order
from qtrader.domain.ports import BrokerGateway
from qtrader.domain.value_objects import OrderFill, OrderStatus, OrderType
from qtrader.infrastructure.resilience import retry_async

_PAPER_URL = "https://paper-api.alpaca.markets"
_LIVE_URL = "https://api.alpaca.markets"


class AlpacaBroker(BrokerGateway):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        secret: str | None = None,
        live: bool = False,
        base_url: str | None = None,
    ) -> None:
        self._api_key = (
            api_key
            or os.environ.get("APCA_API_KEY_ID")
            or os.environ.get("ALPACA_API_KEY", "")
        )
        self._secret = (
            secret
            or os.environ.get("APCA_API_SECRET_KEY")
            or os.environ.get("ALPACA_SECRET_KEY", "")
        )
        live = live or os.environ.get("ALPACA_LIVE", "").lower() == "true"
        self._base_url = base_url or (_LIVE_URL if live else _PAPER_URL)
        if not self._api_key or not self._secret:
            raise RuntimeError(
                "Alpaca credentials are not configured "
                "(ALPACA_API_KEY/ALPACA_SECRET_KEY or "
                "APCA_API_KEY_ID/APCA_API_SECRET_KEY)"
            )
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "APCA-API-KEY-ID": self._api_key,
                "APCA-API-SECRET-KEY": self._secret,
            },
            timeout=10,
        )

    @retry_async()
    async def submit_order(self, order: Order) -> str:
        payload: dict[str, Any] = {
            "symbol": order.symbol or "",
            "qty": str(order.quantity),
            "side": order.side.value.lower(),
            "type": order.order_type.value.lower(),
            "time_in_force": "day",
        }
        if order.order_type is OrderType.LIMIT and order.limit_price is not None:
            payload["limit_price"] = str(order.limit_price.amount)
        if order.stop_loss is not None:
            payload["stop_loss"] = {"stop_price": str(order.stop_loss.amount)}
        if order.take_profit is not None:
            payload["take_profit"] = {"limit_price": str(order.take_profit.amount)}
        response = await self._client.post("/v2/orders", json=payload)
        response.raise_for_status()
        data = response.json()
        return str(data["id"])

    @retry_async()
    async def cancel_order(self, broker_order_id: str) -> None:
        await self._client.delete(f"/v2/orders/{broker_order_id}")

    async def close(self) -> None:
        await self._client.aclose()

    @retry_async()
    async def modify_brackets(
        self, position_id: str, stop_loss: object, take_profit: object
    ) -> None:
        await self._client.patch(
            f"/v2/positions/{position_id}",
            json={
                "stop_loss": {"stop_price": str(stop_loss)},
                "take_profit": {"limit_price": str(take_profit)},
            },
        )

    @retry_async()
    async def get_order_status(self, broker_order_id: str) -> OrderFill:
        response = await self._client.get(f"/v2/orders/{broker_order_id}")
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return OrderFill(
            broker_order_id=broker_order_id,
            status=OrderStatus(data["status"].upper()),
            filled_qty=Decimal(str(data.get("filled_qty") or 0)),
            avg_fill_price=Decimal(str(data.get("filled_avg_price") or 0)),
            commission=Decimal("0"),
        )
