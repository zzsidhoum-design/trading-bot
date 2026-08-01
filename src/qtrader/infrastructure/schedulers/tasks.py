"""Scheduled worker tasks (arq).

Each task receives ``ctx`` with ``redis`` (arq's connection) and any kwargs
from the cron entry. Tasks must be idempotent — arq guarantees at-least-once.
"""

from __future__ import annotations

from typing import Any

from arq import cron
from arq.connections import RedisSettings

from qtrader.config.settings import Settings


async def heartbeat(ctx: dict[str, Any]) -> str:
    """Prove the worker is alive: round-trip through the shared cache/DB."""
    from redis.asyncio import Redis

    from qtrader.config.container import get_container
    from qtrader.infrastructure.cache import RedisCache

    container = get_container()
    cache = RedisCache(container.resolve(Redis))
    await cache.set("worker:heartbeat", "1", ttl_seconds=300)
    db_ok = await container.database_healthy()
    return f"heartbeat db={db_ok}"


async def backfill(ctx: dict[str, Any], symbol: str | None = None) -> str:
    """Placeholder for the Data Agent backfill job (Phase 2)."""
    return f"backfill requested for {symbol or 'all'} (not yet implemented)"


async def scan_cycle(ctx: dict[str, Any]) -> str:
    """Placeholder for the Market Scanner cycle (Phase 2)."""
    return "scan_cycle not yet implemented"


class WorkerSettings:
    functions = [heartbeat, backfill, scan_cycle]

    cron_jobs = [
        cron(heartbeat, name="heartbeat", second=0),
        cron(scan_cycle, name="scan_cycle", minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
    ]

    redis_settings = RedisSettings.from_dsn(Settings().redis_url)

    max_tries = 3
    job_timeout = 60
