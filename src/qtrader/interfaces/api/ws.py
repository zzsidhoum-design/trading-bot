"""Live WebSocket hub — streams domain events to dashboard clients.

Clients connect to ``/ws/live?api_key=...``. The hub subscribes to the
in-process event bus once at startup and forwards every event as a JSON frame.
Reconnects can pass ``?since=<event_uuid>`` to replay the journal from the
outbox before live streaming resumes. A comma-separated ``?topics=`` filter
(``order``, ``trade``, ``price``, ...) restricts which events are forwarded to
a client; topics match event type names case-insensitively as substrings, so
``order`` matches ``OrderFilled``/``OrderSubmitted``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from qtrader.config.container import get_container
from qtrader.config.settings import Settings
from qtrader.domain.events import DomainEvent
from qtrader.domain.ports import EventBus, EventRepository

router = APIRouter(tags=["ws"])


def _frame(event: DomainEvent) -> dict[str, Any]:
    return {
        "type": event.type_name,
        "data": event.payload(),
        "uuid": event.event_uuid,
        "ts": event.occurred_at.isoformat(),
    }


class _Client:
    """A connected WebSocket plus its optional topic filter."""

    def __init__(self, queue: asyncio.Queue[dict[str, Any]], topics: set[str] | None) -> None:
        self.queue = queue
        self.topics = topics

    def accepts(self, type_name: str) -> bool:
        if self.topics is None:
            return True
        lower = type_name.lower()
        return any(t in lower for t in self.topics)


def _parse_topics(raw: str | None) -> set[str] | None:
    if not raw:
        return None
    topics = {t.strip().lower() for t in raw.split(",") if t.strip()}
    return topics or None


class LiveHub:
    """Fan-out hub. One queue per client; broadcast enqueues to all."""

    def __init__(self, bus: EventBus, event_repo: EventRepository) -> None:
        self._bus = bus
        self._event_repo = event_repo
        self._clients: list[_Client] = []
        self._broadcasting = False

    def start(self) -> None:
        if self._broadcasting:
            return
        self._broadcasting = True
        self._bus.subscribe(DomainEvent, self._broadcast)

    async def _broadcast(self, event: DomainEvent) -> None:
        frame = _frame(event)
        for client in list(self._clients):
            if client.accepts(event.type_name):
                client.queue.put_nowait(frame)

    async def connect(
        self,
        websocket: WebSocket,
        since: str | None,
        topics: set[str] | None = None,
    ) -> None:
        await websocket.accept()
        if since is not None:
            await self._replay(websocket, since, topics)

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        client = _Client(queue, topics)
        self._clients.append(client)
        try:
            while True:
                frame = await queue.get()
                await websocket.send_json(frame)
        except WebSocketDisconnect:
            pass
        finally:
            if client in self._clients:
                self._clients.remove(client)

    async def _replay(
        self,
        websocket: WebSocket,
        since: str,
        topics: set[str] | None = None,
    ) -> None:
        events = await self._event_repo.list_after(since, None, 500)
        for event in events:
            if topics is None or any(t in event.type_name.lower() for t in topics):
                await websocket.send_json(_frame(event))


_hub: LiveHub | None = None


def _get_hub() -> LiveHub:
    global _hub
    if _hub is None:
        container = get_container()
        _hub = LiveHub(
            container.resolve(EventBus),
            container.resolve(EventRepository),
        )
        _hub.start()
    return _hub


@router.websocket("/ws/live")
async def ws_live(
    websocket: WebSocket,
    since: str | None = Query(default=None),
    topics: str | None = Query(default=None),
    api_key: str | None = Query(default=None),
) -> None:
    settings = Settings()
    if settings.api_key == "change-me" or api_key != settings.api_key:
        await websocket.close(code=4401)
        return
    await _get_hub().connect(websocket, since, _parse_topics(topics))
