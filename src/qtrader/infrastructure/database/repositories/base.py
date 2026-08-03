"""Session binding support for SQLAlchemy repositories (Unit of Work).

Standalone repositories open their own session per call and commit at the
end of each method. When constructed with ``session=`` they share that
session — reads see the transaction's pending state and ``_commit`` is a
no-op so the owning UnitOfWork controls atomicity.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class SessionBoundRepo:
    _session_factory: async_sessionmaker[AsyncSession]
    _session: AsyncSession | None

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        session: AsyncSession | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._session = session

    @asynccontextmanager
    async def _session_scope(self) -> AsyncIterator[AsyncSession]:
        if self._session is not None:
            yield self._session
            return
        async with self._session_factory() as session:
            yield session

    async def _commit(self, session: AsyncSession) -> None:
        if self._session is None:
            await session.commit()
