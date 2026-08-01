"""Async Redis adapters for the Cache and Lock ports."""

from __future__ import annotations

import json
from typing import cast

from redis.asyncio import Redis

from qtrader.domain.ports import Cache, Lock


class RedisCache(Cache):
    def __init__(self, client: Redis) -> None:
        self._redis = client

    async def get(self, key: str) -> str | None:
        value = await self._redis.get(key)
        return value.decode() if isinstance(value, bytes) else value

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        await self._redis.set(key, value, ex=ttl_seconds)

    async def delete(self, key: str) -> None:
        await self._redis.delete(key)

    async def zadd(self, key: str, mapping: dict[str, float]) -> None:
        await self._redis.zadd(key, mapping)

    async def zrevrange(self, key: str, start: int, end: int) -> list[tuple[str, float]]:
        raw = await self._redis.zrevrange(key, start, end, withscores=True)
        result: list[tuple[str, float]] = []
        for item in raw:
            k, v = cast(tuple[bytes | str, float], item)
            result.append((k.decode() if isinstance(k, bytes) else str(k), float(v)))
        return result

    @staticmethod
    def dumps(value: object) -> str:
        return json.dumps(value, default=str)

    @staticmethod
    def loads(value: str) -> object:
        return json.loads(value)


class RedisLock(Lock):
    def __init__(self, client: Redis) -> None:
        self._redis = client

    async def acquire(self, name: str, ttl_seconds: int = 30) -> bool:
        return bool(await self._redis.set(name, "1", ex=ttl_seconds, nx=True))

    async def release(self, name: str) -> None:
        await self._redis.delete(name)
