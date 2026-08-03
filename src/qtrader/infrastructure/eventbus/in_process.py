"""In-process event bus with optional outbox persistence.

Dispatch semantics: handlers are awaited in subscription order (deterministic,
easy to test) — ordering matters for trading correctness, so dispatch stays
synchronous; heavy work inside handlers runs concurrently with the process.
A failing handler is logged and isolated: the remaining subscribers still
receive the event and the publisher is never crashed by one bad subscriber.

For horizontal scaling, a Redis transport can be swapped in later; the bus is
behind the :class:`EventBus` port, so no agent code changes. The outbox writer
is optional — inject an ``EventRepository`` to persist every published event
(audit trail + replay for the WS hub).
"""

from __future__ import annotations

from collections import defaultdict

from qtrader.config.logging import get_logger
from qtrader.domain.events import DomainEvent
from qtrader.domain.ports import EventBus, EventHandler, EventRepository

_logger = get_logger("qtrader.eventbus")


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
                try:
                    await handler(event)
                except Exception:
                    _logger.exception(
                        "eventbus.handler_failed",
                        event_type=event.type_name,
                        handler=getattr(handler, "__qualname__", str(handler)),
                    )

    def subscriber_count(self) -> int:
        return sum(len(h) for h in self._subscribers.values())

    async def close(self) -> None:
        self._closed = True
