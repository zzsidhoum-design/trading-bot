"""Composition root — builds the full object graph via DI.

Production container wires real adapters; tests build their own container
with fakes. Application code never constructs dependencies directly.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TypeVar, cast

import punq
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from qtrader.config.settings import Settings
from qtrader.domain.ports import (
    Cache,
    EventBus,
    EventRepository,
    Lock,
    PortfolioRepository,
    PriceRepository,
    StockRepository,
)
from qtrader.infrastructure.cache import RedisCache, RedisLock
from qtrader.infrastructure.database.repositories import (
    SQLAlchemyEventRepository,
    SQLAlchemyPortfolioRepository,
    SQLAlchemyPriceRepository,
    SQLAlchemyStockRepository,
)
from qtrader.infrastructure.database.session import build_engine, build_session_factory
from qtrader.infrastructure.eventbus import InProcessEventBus

T = TypeVar("T")


class Container:
    """Thin wrapper over punq that registers the production graph."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()
        self._container = punq.Container()
        self._engine: AsyncEngine | None = None
        self._redis_client: Redis | None = None
        self._build()

    def _build(self) -> None:
        c = self._container
        c.register(Settings, instance=self._settings)

        engine = build_engine(self._settings)
        self._engine = engine
        session_factory = build_session_factory(engine)
        c.register(async_sessionmaker, instance=session_factory)

        self._redis_client = Redis.from_url(self._settings.redis_url, decode_responses=False)
        c.register(Redis, instance=self._redis_client)
        c.register(Cache, instance=RedisCache(self._redis_client))
        c.register(Lock, instance=RedisLock(self._redis_client))

        c.register(EventRepository, instance=SQLAlchemyEventRepository(session_factory))
        c.register(EventBus, instance=InProcessEventBus(c.resolve(EventRepository)))

        c.register(StockRepository, instance=SQLAlchemyStockRepository(session_factory))
        c.register(PortfolioRepository, instance=SQLAlchemyPortfolioRepository(session_factory))
        c.register(PriceRepository, instance=SQLAlchemyPriceRepository(session_factory))

    def resolve(self, service_type: type[T]) -> T:
        return cast(T, self._container.resolve(service_type))

    async def database_healthy(self) -> bool:
        if self._engine is None:
            return False
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def cache_healthy(self) -> bool:
        if self._redis_client is None:
            return False
        try:
            await RedisCache(self._redis_client).set("health:probe", "1", ttl_seconds=5)
            return True
        except Exception:
            return False

    async def aclose(self) -> None:
        """Best-effort release of engine pool and redis connection."""
        if self._redis_client is not None:
            await self._redis_client.aclose()
        if self._engine is not None:
            await self._engine.dispose()


@lru_cache
def get_container() -> Container:
    return Container()
