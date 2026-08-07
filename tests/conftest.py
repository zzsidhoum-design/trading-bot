"""Shared test fixtures & fakes."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from qtrader.config.settings import Settings
from qtrader.domain.events import DomainEvent
from qtrader.domain.ports import EventRepository

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _integration_test_db_name() -> str:
    return os.environ.get("QTRADER_TEST_DB_NAME", "qtrader_test")


async def _database_exists(conn: Any, name: str) -> bool:
    row = await conn.fetchrow(
        "SELECT 1 FROM pg_database WHERE datname = $1", name
    )
    return row is not None


def _create_test_database_if_missing() -> None:
    """Create the dedicated integration-test database (no-op if present)."""
    import asyncpg

    settings = Settings(_env_file=None)
    admin_url = (
        f"postgresql://{settings.postgres_user}:{settings.postgres_password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/postgres"
    )

    async def _create() -> None:
        conn = await asyncpg.connect(admin_url)
        try:
            if not await _database_exists(conn, _integration_test_db_name()):
                await conn.execute(f'CREATE DATABASE "{_integration_test_db_name()}"')
        finally:
            await conn.close()

    asyncio.run(_create())


def pytest_sessionstart(session: pytest.Session) -> None:
    """Redirect integration/e2e tests to an isolated database."""
    if os.environ.get("QTRADER_RUN_INTEGRATION") != "1":
        return
    if os.environ.get("QTRADER_TEST_DB") == "0":
        return
    _create_test_database_if_missing()
    os.environ["POSTGRES_DB"] = _integration_test_db_name()
    from alembic import command
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(str(_REPO_ROOT / "alembic.ini"))
    command.upgrade(cfg, "head")


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
