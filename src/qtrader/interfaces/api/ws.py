"""Live WebSocket hub — streams domain events to dashboard clients.

Clients connect to ``/ws/live?api_key=...``. The hub subscribes to the
in-process event bus once at startup and forwards every event as a JSON frame.
Reconnects can pass ``?since=<event_uuid>`` to replay the journal from the
outbox before live streaming resumes.
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


class LiveHub:
    """Fan-out hub. One queue per client; broadcast enqueues to all."""

    def __init__(self, bus: EventBus, event_repo: EventRepository) -> None:
        self._bus = bus
        self._event_repo = event_repo
        self._clients: list[asyncio.Queue[dict[str, Any]]] = []
        self._broadcasting = False

    def start(self) -> None:
        if self._broadcasting:
            return
        self._broadcasting = True
        self._bus.subscribe(DomainEvent, self._broadcast)

    async def _broadcast(self, event: DomainEvent) -> None:
        frame = _frame(event)
        for queue in list(self._clients):
            queue.put_nowait(frame)

    async def connect(self, websocket: WebSocket, since: str | None) -> None:
        await websocket.accept()
        if since is not None:
            await self._replay(websocket, since)

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._clients.append(queue)
        try:
            while True:
                frame = await queue.get()
                await websocket.send_json(frame)
        except WebSocketDisconnect:
            pass
        finally:
            if queue in self._clients:
                self._clients.remove(queue)

    async def _replay(self, websocket: WebSocket, since: str) -> None:
        events = await self._event_repo.list_after(since, None, 500)
        for event in events:
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
    api_key: str | None = Query(default=None),
) -> None:
    settings = Settings()
    if settings.api_key == "change-me" or api_key != settings.api_key:
        await websocket.close(code=4401)
        return
    await _get_hub().connect(websocket, since)
