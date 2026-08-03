"""Unit tests for the in-process event bus + outbox."""

from __future__ import annotations

import pytest

from qtrader.domain.events import DomainEvent, OrderFilled, PriceUpdated, RiskRejected
from qtrader.domain.value_objects import Interval
from qtrader.infrastructure.eventbus import InProcessEventBus
from tests.conftest import FakeEventRepository


def _price_event() -> PriceUpdated:
    return PriceUpdated(
        symbol="AAPL",
        interval=Interval.M1,
        ts="2026-08-01T12:00:00Z",
        open="100",
        high="101",
        low="99",
        close="100.5",
        volume="1000",
    )


@pytest.mark.asyncio
async def test_subscribe_publish_order(fake_outbox: FakeEventRepository) -> None:
    bus = InProcessEventBus(fake_outbox)
    seen: list[DomainEvent] = []

    async def handler(event: DomainEvent) -> None:
        seen.append(event)

    bus.subscribe(PriceUpdated, handler)
    await bus.publish(_price_event())

    assert len(seen) == 1
    assert isinstance(seen[0], PriceUpdated)
    await bus.close()


@pytest.mark.asyncio
async def test_only_matching_types_delivered(fake_outbox: FakeEventRepository) -> None:
    bus = InProcessEventBus(fake_outbox)
    seen: list[str] = []

    async def handler(event: DomainEvent) -> None:
        seen.append(event.type_name)

    bus.subscribe(OrderFilled, handler)
    await bus.publish(_price_event())
    await bus.publish(
        OrderFilled(order_id="1", broker_order_id="b1", fill_price="100", fill_qty="10", fees="0")
    )

    assert seen == ["OrderFilled"]
    await bus.close()


@pytest.mark.asyncio
async def test_outbox_records_every_event(fake_outbox: FakeEventRepository) -> None:
    bus = InProcessEventBus(fake_outbox)
    await bus.publish(_price_event())
    await bus.publish(RiskRejected(decision_uuid="d1", symbol="AAPL", reasons=["max daily loss"]))

    assert len(fake_outbox.records) == 2
    assert {e.type_name for e in fake_outbox.records} == {"PriceUpdated", "RiskRejected"}
    await bus.close()


@pytest.mark.asyncio
async def test_publish_after_close_raises(fake_outbox: FakeEventRepository) -> None:
    bus = InProcessEventBus(fake_outbox)
    await bus.close()
    with pytest.raises(RuntimeError):
        await bus.publish(_price_event())


@pytest.mark.asyncio
async def test_base_class_subscription_catches_all(fake_outbox: FakeEventRepository) -> None:
    bus = InProcessEventBus(fake_outbox)
    seen: list[DomainEvent] = []

    async def handler(event: DomainEvent) -> None:
        seen.append(event)

    bus.subscribe(DomainEvent, handler)
    await bus.publish(_price_event())
    assert len(seen) == 1
    await bus.close()


@pytest.mark.asyncio
async def test_failing_handler_does_not_block_others(fake_outbox: FakeEventRepository) -> None:
    bus = InProcessEventBus(fake_outbox)
    seen: list[str] = []

    async def bad_handler(event: DomainEvent) -> None:
        raise RuntimeError("boom")

    async def good_handler(event: DomainEvent) -> None:
        seen.append(event.type_name)

    bus.subscribe(PriceUpdated, bad_handler)
    bus.subscribe(PriceUpdated, good_handler)
    await bus.publish(_price_event())

    assert seen == ["PriceUpdated"]
    await bus.close()


@pytest.mark.asyncio
async def test_failing_handler_does_not_crash_publisher(fake_outbox: FakeEventRepository) -> None:
    bus = InProcessEventBus(fake_outbox)

    async def bad_handler(event: DomainEvent) -> None:
        raise RuntimeError("boom")

    bus.subscribe(PriceUpdated, bad_handler)
    await bus.publish(_price_event())
    assert len(fake_outbox.records) == 1
    await bus.close()
