"""Event outbox repository backed by the ``events`` table."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from qtrader.domain.events import DomainEvent
from qtrader.domain.ports import EventRepository
from qtrader.infrastructure.database.models import EventRecordModel


def _deserialize(
    type_name: str, payload: dict[str, Any], event_uuid: str, occurred_at: datetime
) -> DomainEvent | None:
    """Best-effort reconstruct a typed event from outbox payload.

    Falls back to ``None`` if the event class is unavailable; the raw row
    remains in the journal regardless.
    """
    from qtrader.domain import events as ev

    event_cls = getattr(ev, type_name, None)
    if event_cls is None:
        return None
    try:
        kwargs = dict(payload or {})
        kwargs["event_uuid"] = event_uuid
        kwargs["occurred_at"] = occurred_at
        return cast(DomainEvent | None, event_cls(**kwargs))
    except (TypeError, ValueError):
        return None


class SQLAlchemyEventRepository(EventRepository):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(self, event: DomainEvent) -> None:
        async with self._session_factory() as session:
            row = EventRecordModel(
                event_uuid=event.event_uuid,
                type=event.type_name,
                payload=event.payload(),
                occurred_at=event.occurred_at,
            )
            session.add(row)
            await session.commit()

    async def list_after(
        self, event_uuid: str | None, event_type: str | None, limit: int
    ) -> list[DomainEvent]:
        async with self._session_factory() as session:
            stmt = select(EventRecordModel).order_by(
                EventRecordModel.occurred_at, EventRecordModel.id
            )
            if event_uuid is not None:
                anchor = await session.scalar(
                    select(EventRecordModel.occurred_at).where(
                        EventRecordModel.event_uuid == event_uuid
                    )
                )
                if anchor is not None:
                    stmt = stmt.where(EventRecordModel.occurred_at >= anchor)
            if event_type is not None:
                stmt = stmt.where(EventRecordModel.type == event_type)
            rows = await session.scalars(stmt.limit(limit))
            events: list[DomainEvent] = []
            for row in rows:
                event = _deserialize(row.type, row.payload or {}, row.event_uuid, row.occurred_at)
                if event is not None:
                    events.append(event)
            return events

    async def count_by_type(self, limit: int = 1000) -> dict[str, int]:
        async with self._session_factory() as session:
            recent = (
                select(EventRecordModel.type)
                .order_by(EventRecordModel.occurred_at.desc(), EventRecordModel.id.desc())
                .limit(limit)
                .subquery()
            )
            rows = await session.execute(
                select(recent.c.type, func.count()).group_by(recent.c.type)
            )
            return {type_name: int(count) for type_name, count in rows}
