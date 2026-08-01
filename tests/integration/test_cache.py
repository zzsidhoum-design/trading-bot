"""Integration tests for the Redis Cache/Lock adapters and the event outbox."""

from __future__ import annotations

import pytest
from redis.asyncio import Redis
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker

from qtrader.config.settings import Settings
from qtrader.domain.events import PriceUpdated
from qtrader.domain.value_objects import Interval
from qtrader.infrastructure.cache import RedisCache, RedisLock
from qtrader.infrastructure.database.models import EventRecordModel
from qtrader.infrastructure.database.repositories import SQLAlchemyEventRepository
from qtrader.infrastructure.database.session import build_engine, build_session_factory

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def session_factory() -> async_sessionmaker:
    settings = Settings(_env_file=None)
    engine = build_engine(settings)
    return build_session_factory(engine)


@pytest.fixture(scope="module")
def redis_client() -> Redis:
    client = Redis.from_url(Settings(_env_file=None).redis_url, decode_responses=False)
    return client


@pytest.mark.asyncio
async def test_cache_set_get_delete(redis_client: Redis) -> None:
    cache = RedisCache(redis_client)
    key = "integration:cache"
    await cache.delete(key)
    assert await cache.get(key) is None

    await cache.set(key, "v1", ttl_seconds=60)
    assert await cache.get(key) == "v1"

    await cache.set(key, "v2")
    assert await cache.get(key) == "v2"

    await cache.delete(key)
    assert await cache.get(key) is None


@pytest.mark.asyncio
async def test_cache_ttl_expiry(redis_client: Redis) -> None:
    cache = RedisCache(redis_client)
    key = "integration:cache:ttl"
    await cache.set(key, "x", ttl_seconds=1)
    assert await cache.get(key) == "x"
    await redis_client.execute_command("PEXPIRE", key, 100)
    import asyncio

    await asyncio.sleep(0.2)
    assert await cache.get(key) is None


@pytest.mark.asyncio
async def test_cache_zrevrange(redis_client: Redis) -> None:
    cache = RedisCache(redis_client)
    key = "integration:cache:rank"
    await redis_client.delete(key)
    await cache.zadd(key, {"AAPL": 1.5, "MSFT": 3.0, "TSLA": 2.0})
    top = await cache.zrevrange(key, 0, 2)
    assert [name for name, _ in top] == ["MSFT", "TSLA", "AAPL"]
    assert [score for _, score in top] == [3.0, 2.0, 1.5]


@pytest.mark.asyncio
async def test_lock_acquire_and_release(redis_client: Redis) -> None:
    lock = RedisLock(redis_client)
    name = "integration:lock"
    assert await lock.acquire(name, ttl_seconds=30) is True
    assert await lock.acquire(name, ttl_seconds=30) is False
    await lock.release(name)
    assert await lock.acquire(name, ttl_seconds=30) is True
    await lock.release(name)


@pytest.mark.asyncio
async def test_outbox_roundtrip(session_factory: async_sessionmaker) -> None:
    repo = SQLAlchemyEventRepository(session_factory)

    async with session_factory() as session:
        await session.execute(delete(EventRecordModel))
        await session.commit()

    event = PriceUpdated(
        symbol="AAPL",
        interval=Interval.M5,
        ts="2026-08-01T12:00:00Z",
        open="179.5",
        high="181.0",
        low="179.0",
        close="180.5",
        volume="1250000",
    )
    await repo.record(event)

    events = await repo.list_after(None, None, 10)
    assert len(events) == 1
    assert isinstance(events[0], PriceUpdated)
    assert events[0].event_uuid == event.event_uuid

    filtered = await repo.list_after(None, "PriceUpdated", 10)
    assert len(filtered) == 1
    assert filtered[0].symbol == "AAPL"
