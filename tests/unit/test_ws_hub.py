"""Unit tests for the live WebSocket hub (fan-out, topics, replay, auth)."""

from __future__ import annotations

import asyncio

from qtrader.config.container import get_container
from qtrader.domain.events import OrderFilled, PriceUpdated
from qtrader.domain.ports import EventRepository
from qtrader.infrastructure.eventbus.in_process import InProcessEventBus
from qtrader.interfaces.api.ws import LiveHub, _parse_topics, ws_live


class _FakeEventRepository(EventRepository):
    def __init__(self, events: list) -> None:
        self._events = events

    async def record(self, event) -> None:
        self._events.append(event)

    async def list_after(self, event_uuid, event_type, limit) -> list:
        if event_uuid is None:
            return self._events[:limit]
        seen = False
        result = []
        for event in self._events:
            if event.event_uuid == event_uuid:
                seen = True
                continue
            if seen:
                result.append(event)
        return result[:limit]


class _FakeWebSocket:
    def __init__(self) -> None:
        self.accepted = False
        self.sent: list[dict] = []
        self.close_code: int | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, frame: dict) -> None:
        self.sent.append(frame)

    async def close(self, code: int = 1000) -> None:
        self.close_code = code


def _price_event() -> PriceUpdated:
    return PriceUpdated(
        symbol="AAPL",
        interval="5m",
        ts="2026-08-01T12:00:00Z",
        open="179.5",
        high="181",
        low="179",
        close="180.5",
        volume="1250000",
    )


def _order_event() -> OrderFilled:
    return OrderFilled(
        order_id="1",
        broker_order_id="brk-1",
        fill_price="180.5",
        fill_qty="10",
        fees="0",
    )


def test_parse_topics() -> None:
    assert _parse_topics(None) is None
    assert _parse_topics("") is None
    assert _parse_topics("Order, trade ") == {"order", "trade"}
    assert _parse_topics(" , ") is None


async def test_live_hub_fans_out_to_all_clients() -> None:
    bus = InProcessEventBus(_FakeEventRepository([]))
    hub = LiveHub(bus, _FakeEventRepository([]))
    hub.start()

    ws1, ws2 = _FakeWebSocket(), _FakeWebSocket()
    t1 = asyncio.create_task(hub.connect(ws1, None, None))
    t2 = asyncio.create_task(hub.connect(ws2, None, None))
    await asyncio.sleep(0)

    await bus.publish(_order_event())
    await asyncio.sleep(0)
    assert len(ws1.sent) == 1
    assert ws1.sent[0]["type"] == "OrderFilled"
    assert ws1.sent[0]["data"]["fill_price"] == "180.5"
    assert ws2.sent == ws1.sent

    t1.cancel()
    t2.cancel()


async def test_live_hub_topic_filter_restricts_clients() -> None:
    bus = InProcessEventBus(_FakeEventRepository([]))
    hub = LiveHub(bus, _FakeEventRepository([]))
    hub.start()

    filtered, unfiltered = _FakeWebSocket(), _FakeWebSocket()
    t1 = asyncio.create_task(hub.connect(filtered, None, {"order"}))
    t2 = asyncio.create_task(hub.connect(unfiltered, None, None))
    await asyncio.sleep(0)

    await bus.publish(_price_event())
    await bus.publish(_order_event())
    await asyncio.sleep(0)

    assert [f["type"] for f in filtered.sent] == ["OrderFilled"]
    assert [f["type"] for f in unfiltered.sent] == ["PriceUpdated", "OrderFilled"]

    t1.cancel()
    t2.cancel()


async def test_live_hub_replays_journal_before_live() -> None:
    events = [_price_event()]
    bus = InProcessEventBus(_FakeEventRepository(events))
    hub = LiveHub(bus, _FakeEventRepository(events))
    hub.start()

    ws = _FakeWebSocket()
    task = asyncio.create_task(hub.connect(ws, events[0].event_uuid, None))
    await asyncio.sleep(0)

    await bus.publish(_order_event())
    await asyncio.sleep(0)

    assert [f["type"] for f in ws.sent] == ["OrderFilled"]
    task.cancel()


async def test_live_hub_removes_client_on_disconnect() -> None:
    bus = InProcessEventBus(_FakeEventRepository([]))
    hub = LiveHub(bus, _FakeEventRepository([]))
    hub.start()

    ws = _FakeWebSocket()
    task = asyncio.create_task(hub.connect(ws, None, None))
    await asyncio.sleep(0)
    assert len(hub._clients) == 1

    task.cancel()
    await asyncio.sleep(0)
    assert hub._clients == []


async def test_ws_live_rejects_bad_api_key(monkeypatch) -> None:
    from qtrader.config.settings import Settings

    class _FakeContainer:
        def resolve(self, service_type: type) -> object:
            if service_type is EventRepository:
                return _FakeEventRepository([])
            raise KeyError(service_type)

    monkeypatch.setattr(
        "qtrader.interfaces.api.ws.Settings",
        lambda: Settings(_env_file=None, api_key="secret-key"),
    )
    monkeypatch.setattr(get_container, "__call__", lambda: _FakeContainer())

    ws = _FakeWebSocket()
    await ws_live(ws, since=None, topics=None, api_key="wrong-key")
    assert ws.close_code == 4401
    assert not ws.accepted
