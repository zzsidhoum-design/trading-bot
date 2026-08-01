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

from qtrader.application.agents.data import DataAgent
from qtrader.application.agents.scanner import MarketScanner
from qtrader.application.services.bar_cleaner import BarCleaner
from qtrader.config.settings import Settings
from qtrader.domain.events import BackfillCompleted
from qtrader.domain.ports import (
    Cache,
    EventBus,
    EventRepository,
    Lock,
    MarketDataProvider,
    PortfolioRepository,
    PriceRepository,
    StockRepository,
)
from qtrader.infrastructure.cache import RedisCache, RedisLock
from qtrader.infrastructure.data_providers.yahoo import YahooFinanceProvider
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
        self._provider: YahooFinanceProvider | None = None
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

        cleaner = BarCleaner()
        c.register(BarCleaner, instance=cleaner)

        provider = YahooFinanceProvider()
        self._provider = provider
        c.register(MarketDataProvider, instance=provider)

        bus = c.resolve(EventBus)
        data_agent = DataAgent(
            provider=provider,
            prices=c.resolve(PriceRepository),
            cache=c.resolve(Cache),
            bus=bus,
            cleaner=cleaner,
            quote_cache_ttl_seconds=self._settings.quote_cache_ttl_seconds,
        )
        c.register(DataAgent, instance=data_agent)

        scanner = MarketScanner(
            prices=c.resolve(PriceRepository),
            cache=c.resolve(Cache),
            stocks=c.resolve(StockRepository),
            bus=bus,
            top_k=self._settings.scan_top_k,
            lookback_bars=self._settings.scan_lookback_bars,
            momentum_lookback=self._settings.scan_momentum_lookback,
            min_dollar_volume=self._settings.scan_min_dollar_volume,
            min_atr_pct=self._settings.scan_min_atr_pct,
        )
        c.register(MarketScanner, instance=scanner)
        bus.subscribe(BackfillCompleted, scanner.on_event)

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
        """Best-effort release of engine pool, redis and provider connections."""
        if self._provider is not None:
            await self._provider.close()
        if self._redis_client is not None:
            await self._redis_client.aclose()
        if self._engine is not None:
            await self._engine.dispose()


@lru_cache
def get_container() -> Container:
    return Container()
