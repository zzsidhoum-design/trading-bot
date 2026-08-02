"""WS hub fan-out concurrency test (Phase 8 hardening).

Verifies that a single broadcast reaches every connected client and that
topic filters still apply under concurrent load.
"""

from __future__ import annotations

import asyncio
from typing import Any

from qtrader.domain.events import PriceUpdated
from qtrader.domain.ports import EventRepository
from qtrader.domain.value_objects import Interval
from qtrader.infrastructure.eventbus import InProcessEventBus
from qtrader.interfaces.api.ws import LiveHub, _parse_topics


class FakeWebSocket:
    """Minimal stand-in for a connected WebSocket."""

    def __init__(self) -> None:
        self.accepted = False
        self.frames: list[dict[str, Any]] = []
        self.closed_code: int | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)

    async def close(self, code: int = 1000) -> None:
        self.closed_code = code


class InMemoryOutbox(EventRepository):
    def __init__(self) -> None:
        self.records: list = []

    async def record(self, event) -> None:
        self.records.append(event)

    async def list_after(self, event_uuid, event_type, limit):
        return list(self.records)[:limit]


def _price_event(tick: int) -> PriceUpdated:
    return PriceUpdated(
        symbol="AAPL",
        interval=Interval.M5,
        ts=f"2026-08-01T12:0{tick}:00Z",
        open="100",
        high="101",
        low="99",
        close="100.5",
        volume="1000",
    )


async def _connected(ws: FakeWebSocket) -> bool:
    return ws.accepted


async def test_broadcast_fans_out_to_all_clients() -> None:
    bus = InProcessEventBus(InMemoryOutbox())
    hub = LiveHub(bus, InMemoryOutbox())
    hub.start()

    clients = [FakeWebSocket() for _ in range(8)]
    tasks = [asyncio.create_task(hub.connect(ws, None)) for ws in clients]
    try:
        for _ in range(100):
            if all(await asyncio.gather(*(_connected(ws) for ws in clients))):
                break
            await asyncio.sleep(0.01)

        await hub._broadcast(_price_event(1))

        for _ in range(100):
            if all(len(ws.frames) >= 1 for ws in clients):
                break
            await asyncio.sleep(0.01)

        assert all(len(ws.frames) == 1 for ws in clients)
        assert all(ws.frames[0]["type"] == "PriceUpdated" for ws in clients)
        assert all(ws.frames[0]["data"]["symbol"] == "AAPL" for ws in clients)
    finally:
        for task in tasks:
            task.cancel()


async def test_broadcast_respects_topic_filters_under_load() -> None:
    bus = InProcessEventBus(InMemoryOutbox())
    hub = LiveHub(bus, InMemoryOutbox())
    hub.start()

    price_client = FakeWebSocket()
    order_client = FakeWebSocket()
    tasks = [
        asyncio.create_task(hub.connect(price_client, None, _parse_topics("price"))),
        asyncio.create_task(hub.connect(order_client, None, _parse_topics("order"))),
    ]
    try:
        for _ in range(100):
            if price_client.accepted and order_client.accepted:
                break
            await asyncio.sleep(0.01)

        for tick in range(5):
            await hub._broadcast(_price_event(tick))

        for _ in range(100):
            if len(price_client.frames) >= 5 and order_client.frames:
                break
            await asyncio.sleep(0.01)

        assert len(price_client.frames) == 5
        assert order_client.frames == []
    finally:
        for task in tasks:
            task.cancel()
