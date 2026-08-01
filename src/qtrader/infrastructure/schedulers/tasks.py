"""Scheduled worker tasks (arq).

Each task receives ``ctx`` with ``redis`` (arq's connection) and any kwargs
from the cron entry. Tasks must be idempotent — arq guarantees at-least-once.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from arq import cron
from arq.connections import RedisSettings

from qtrader.config.settings import Settings
from qtrader.domain.value_objects import Interval


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


async def backfill(
    ctx: dict[str, Any],
    symbol: str | None = None,
    interval: str | None = None,
    days: int | None = None,
) -> str:
    """Data Agent job: pull clean history for the watchlist (or one symbol)."""
    from qtrader.application.agents.data import DataAgent
    from qtrader.config.container import get_container

    container = get_container()
    settings = container.resolve(Settings)
    agent = container.resolve(DataAgent)
    iv = Interval(interval) if interval else settings.scan_interval
    symbols = [symbol] if symbol else settings.watchlist_symbols
    end = datetime.now(UTC)
    start = end - timedelta(days=days or settings.backfill_days)
    total = 0
    for sym in symbols:
        inserted = await agent.backfill(sym, iv, start, end)
        total += inserted
    return f"backfilled {total} bars for {len(symbols)} symbols ({iv})"


async def scan_cycle(ctx: dict[str, Any]) -> str:
    """Market Scanner cycle: recompute top-K rankings."""
    from qtrader.application.agents.scanner import MarketScanner
    from qtrader.config.container import get_container

    container = get_container()
    scanner = container.resolve(MarketScanner)
    top = await scanner.scan_all()
    return f"scan produced {len(top)} candidates"


async def analyze_cycle(ctx: dict[str, Any], symbols: list[str] | None = None) -> str:
    """Phase 3 analysis cycle: technical, news & fundamental for the candidates."""
    from qtrader.application.agents.fundamental import FundamentalAgent
    from qtrader.application.agents.news import NewsAgent
    from qtrader.application.agents.scanner import MarketScanner
    from qtrader.application.agents.technical import TechnicalAgent
    from qtrader.config.container import get_container

    container = get_container()
    scanner = container.resolve(MarketScanner)
    if symbols is None:
        top = await scanner.scan_all()
        symbols = [c.symbol for c in top]
    technical = await container.resolve(TechnicalAgent).analyze_candidates(symbols)
    news = await container.resolve(NewsAgent).analyze_candidates(symbols)
    fundamental = await container.resolve(FundamentalAgent).analyze_candidates(symbols)
    return (
        f"analyzed {len(symbols)} symbols: "
        f"technical={technical} news={news} fundamental={fundamental}"
    )


class WorkerSettings:
    functions = [heartbeat, backfill, scan_cycle, analyze_cycle]

    cron_jobs = [
        cron(heartbeat, name="heartbeat", second=0),
        cron(scan_cycle, name="scan_cycle", minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
        cron(analyze_cycle, name="analyze_cycle", minute={2, 17, 32, 47}),
    ]

    redis_settings = RedisSettings.from_dsn(Settings().redis_url)

    max_tries = 3
    job_timeout = 60
