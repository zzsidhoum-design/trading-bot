"""Paper execution brokers — recording + shadow wrappers.

Two wrappers install into the container so the *real* pipeline feeds the paper
ledger during continuous operation:

* :class:`PaperExecutionBroker` wraps the Paper/Alpaca broker and records every
  lifecycle event (submit latency, fill, slippage, rejection reason) into the
  ledger and telemetry. It never fabricates a fill.
* :class:`ShadowBroker` records decisions but never submits anything — not even
  a paper order. It is the "shadow deployment" instrument: the full decision
  pipeline runs, and the ledger captures what *would* have been traded.
"""

from __future__ import annotations

import time
import uuid
from decimal import Decimal

from qtrader.application.paper.ledger import PaperOrderLedger
from qtrader.application.paper.models import PaperOrderStatus
from qtrader.application.paper.telemetry import TelemetryRecorder
from qtrader.domain.entities import Order
from qtrader.domain.exceptions import NotFoundError
from qtrader.domain.ports import BrokerGateway, PriceRepository
from qtrader.domain.value_objects import (
    Interval,
    Money,
    OrderFill,
    OrderStatus,
)


def _ref_for(order: Order, prefix: str) -> str:
    return order.decision_ref or order.idempotency_key or f"{prefix}-{uuid.uuid4()}"


def _requested_price(order: Order) -> Decimal | None:
    if order.limit_price is not None:
        return order.limit_price.amount
    if order.stop_price is not None:
        return order.stop_price.amount
    return None


class PaperExecutionBroker(BrokerGateway):
    """Decorates a broker with ledger + telemetry recording.

    Every submit is audited as a :class:`PaperOrderRecord`; on fill the record
    is updated with fill price, slippage (fill - requested, per share) and the
    total execution latency. Broker failures are recorded as ``REJECTED`` with
    the reason and a telemetry ``api_failure`` before re-raising so the caller
    still sees the original error.
    """

    def __init__(
        self,
        inner: BrokerGateway,
        ledger: PaperOrderLedger,
        telemetry: TelemetryRecorder,
        *,
        prices: PriceRepository | None = None,
        default_price: Decimal = Decimal("100"),
    ) -> None:
        self._inner = inner
        self._ledger = ledger
        self._telemetry = telemetry
        self._prices = prices
        self._default_price = default_price

    async def submit_order(self, order: Order) -> str:
        ref = _ref_for(order, "order")
        started = time.perf_counter()
        try:
            broker_order_id = await self._inner.submit_order(order)
        except Exception as exc:  # noqa: BLE001 - record the rejection reason
            await self._telemetry.api_failure("broker", str(exc))
            self._ledger.update(
                ref,
                status=PaperOrderStatus.REJECTED,
                rejection_reason=str(exc),
                broker_order_id=None,
                decision_ref=order.decision_ref or ref,
                asset=order.symbol or "",
                side=order.side.value,
                quantity=Decimal(order.quantity),
                order_type=order.order_type.value,
                requested_price=_requested_price(order),
            )
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000
        await self._telemetry.latency("submit", elapsed_ms)
        self._ledger.update(
            ref,
            status=PaperOrderStatus.SUBMITTED,
            broker_order_id=broker_order_id,
            decision_ref=order.decision_ref or ref,
            asset=order.symbol or "",
            side=order.side.value,
            quantity=Decimal(order.quantity),
            order_type=order.order_type.value,
            requested_price=_requested_price(order),
            strategy=str(order.reason.get("strategy", "default")) if order.reason else "default",
            context=dict(order.reason or {}),
        )
        return broker_order_id

    async def get_order_status(self, broker_order_id: str) -> OrderFill:
        started = time.perf_counter()
        try:
            fill = await self._inner.get_order_status(broker_order_id)
        except Exception as exc:  # noqa: BLE001
            await self._telemetry.api_failure("broker", str(exc))
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000
        await self._telemetry.latency("fill_poll", elapsed_ms)
        if fill.status in (OrderStatus.FILLED, OrderStatus.PARTIAL):
            for record in self._ledger.all():
                if record.broker_order_id == broker_order_id and not record.is_terminal:
                    fill_price = fill.avg_fill_price
                    requested = record.requested_price
                    slippage = None
                    if fill_price is not None and requested is not None:
                        slippage = fill_price - requested
                    self._ledger.update(
                        record.key,
                        status=(
                            PaperOrderStatus.FILLED
                            if fill.status is OrderStatus.FILLED
                            else PaperOrderStatus.PARTIAL
                        ),
                        fill_price=fill_price,
                        slippage=slippage,
                        commission=fill.commission,
                        execution_latency_ms=elapsed_ms,
                    )
        return fill

    async def cancel_order(self, broker_order_id: str) -> None:
        started = time.perf_counter()
        try:
            await self._inner.cancel_order(broker_order_id)
        except Exception as exc:  # noqa: BLE001
            await self._telemetry.api_failure("broker", str(exc))
            raise
        await self._telemetry.latency("cancel", (time.perf_counter() - started) * 1000)
        for record in self._ledger.all():
            if record.broker_order_id == broker_order_id and not record.is_terminal:
                self._ledger.update(record.key, status=PaperOrderStatus.CANCELED)

    async def modify_brackets(
        self, position_id: str, stop_loss: Money, take_profit: Money
    ) -> None:
        started = time.perf_counter()
        try:
            await self._inner.modify_brackets(position_id, stop_loss, take_profit)
        except Exception as exc:  # noqa: BLE001
            await self._telemetry.api_failure("broker", str(exc))
            raise
        await self._telemetry.latency("modify_brackets", (time.perf_counter() - started) * 1000)

    async def close(self) -> None:
        await self._inner.close()


class ShadowBroker(BrokerGateway):
    """Shadow mode broker — records the intended order, never submits it.

    ``submit_order`` returns a synthetic ``shadow-`` id and the decision is
    stored as ``SHADOW_ONLY`` with a simulated reference price; nothing is ever
    sent to a paper or live venue.
    """

    def __init__(
        self,
        ledger: PaperOrderLedger,
        telemetry: TelemetryRecorder,
        *,
        prices: PriceRepository | None = None,
        default_price: Decimal = Decimal("100"),
    ) -> None:
        self._ledger = ledger
        self._telemetry = telemetry
        self._prices = prices
        self._default_price = default_price

    async def _simulated_price(self, order: Order) -> Decimal:
        if order.limit_price is not None:
            return order.limit_price.amount
        if self._prices is not None and order.symbol is not None:
            bar = await self._prices.latest(order.symbol, Interval.D1)
            if bar is not None:
                return bar.close
        return self._default_price

    async def submit_order(self, order: Order) -> str:
        ref = _ref_for(order, "shadow")
        simulated = await self._simulated_price(order)
        broker_order_id = f"shadow-{uuid.uuid4()}"
        self._ledger.update(
            ref,
            status=PaperOrderStatus.SHADOW_ONLY,
            shadow=True,
            broker_order_id=broker_order_id,
            decision_ref=order.decision_ref or ref,
            asset=order.symbol or "",
            side=order.side.value,
            quantity=Decimal(order.quantity),
            order_type=order.order_type.value,
            requested_price=_requested_price(order),
            simulated_price=simulated,
            strategy=str(order.reason.get("strategy", "default")) if order.reason else "default",
            context=dict(order.reason or {}),
        )
        await self._telemetry.record_log(
            "INFO",
            f"shadow decision recorded for {order.symbol or 'unknown'}",
            component="paper.shadow",
            context={"ref": order.decision_ref or ref},
        )
        return broker_order_id

    async def cancel_order(self, broker_order_id: str) -> None:
        return None

    async def modify_brackets(
        self, position_id: str, stop_loss: Money, take_profit: Money
    ) -> None:
        return None

    async def get_order_status(self, broker_order_id: str) -> OrderFill:
        record = next(
            (r for r in self._ledger.all() if r.broker_order_id == broker_order_id),
            None,
        )
        if record is None:
            raise NotFoundError(f"unknown shadow order {broker_order_id}")
        return OrderFill(
            broker_order_id=broker_order_id,
            status=OrderStatus.PENDING,
            filled_qty=Decimal("0"),
            avg_fill_price=record.simulated_price or Decimal("0"),
            commission=Decimal("0"),
        )

    async def close(self) -> None:
        return None


__all__ = ["PaperExecutionBroker", "ShadowBroker"]
