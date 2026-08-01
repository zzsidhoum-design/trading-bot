"""In-process event bus with optional outbox persistence.

Dispatch semantics: handlers are awaited in subscription order (deterministic,
easy to test). For horizontal scaling, the Redis transport (later phase) fans
out across workers while this bus remains the single-process backbone.

The outbox writer is optional — inject an ``EventRepository`` to persist every
published event (audit trail + replay).
"""

from __future__ import annotations

from collections import defaultdict

from qtrader.domain.events import DomainEvent
from qtrader.domain.ports import EventBus, EventHandler, EventRepository


class InProcessEventBus(EventBus):
    def __init__(self, outbox: EventRepository | None = None) -> None:
        self._subscribers: dict[type[DomainEvent], list[EventHandler]] = defaultdict(list)
        self._outbox = outbox
        self._closed = False

    def subscribe(self, event_type: type[DomainEvent], handler: EventHandler) -> None:
        if self._closed:
            raise RuntimeError("Event bus is closed")
        self._subscribers[event_type].append(handler)

    async def publish(self, event: DomainEvent) -> None:
        if self._closed:
            raise RuntimeError("Event bus is closed")
        if self._outbox is not None:
            await self._outbox.record(event)
        for event_type, handlers in self._subscribers.items():
            if not isinstance(event, event_type):
                continue
            for handler in handlers:
                await handler(event)

    def subscriber_count(self) -> int:
        return sum(len(h) for h in self._subscribers.values())

    async def close(self) -> None:
        self._closed = True
