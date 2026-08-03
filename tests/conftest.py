"""Shared test fixtures & fakes."""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from qtrader.config.settings import Settings
from qtrader.domain.events import DomainEvent
from qtrader.domain.ports import EventRepository


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(_env_file=None)


class FakeEventRepository(EventRepository):
    """In-memory outbox for unit tests."""

    def __init__(self) -> None:
        self.records: list[DomainEvent] = []

    async def record(self, event: DomainEvent) -> None:
        self.records.append(event)

    async def list_after(
        self, event_uuid: str | None, event_type: str | None, limit: int
    ) -> list[DomainEvent]:
        events = list(self.records)
        if event_uuid is not None:
            started = False
            events = [e for e in events if (started := started or e.event_uuid == event_uuid)]
        if event_type is not None:
            events = [e for e in events if e.type_name == event_type]
        return events[:limit]

    async def count_by_type(self, limit: int = 1000) -> dict[str, int]:
        return {e.type_name: self.records.count(e) for e in set(self.records)}


@pytest.fixture
def fake_outbox() -> FakeEventRepository:
    return FakeEventRepository()


@pytest.fixture
def event_fixture_factory() -> None:
    """Placeholder — real factories arrive with the fakes package."""


def pytest_collection_modifyitems(items: Iterable[pytest.Item]) -> None:
    """Integration/e2e tests are skipped unless ``QTRADER_RUN_INTEGRATION=1``."""
    import os

    if os.environ.get("QTRADER_RUN_INTEGRATION") == "1":
        return
    for item in items:
        if any(mark.name in {"integration", "e2e"} for mark in item.iter_markers()):
            item.add_marker(
                pytest.mark.skip(
                    reason="requires live Postgres/Redis; set QTRADER_RUN_INTEGRATION=1"
                )
            )
