"""Integration test for the Phase 2 pipeline: DataAgent → MarketScanner.

Runs only when ``QTRADER_RUN_INTEGRATION=1`` with Postgres + Redis up
(`docker compose up -d postgres redis`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from redis.asyncio import Redis
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker

from qtrader.application.agents.data import DataAgent
from qtrader.application.agents.scanner import SCAN_ZSET_PREFIX, MarketScanner
from qtrader.application.services.bar_cleaner import BarCleaner
from qtrader.config.settings import Settings
from qtrader.domain.entities import Stock
from qtrader.domain.events import BackfillCompleted, ScanCompleted
from qtrader.domain.ports import MarketDataProvider
from qtrader.domain.value_objects import Interval, PriceBar
from qtrader.infrastructure.cache import RedisCache
from qtrader.infrastructure.database.models import EventRecordModel, PriceModel, StockModel
from qtrader.infrastructure.database.repositories import (
    SQLAlchemyEventRepository,
    SQLAlchemyPriceRepository,
    SQLAlchemyStockRepository,
)
from qtrader.infrastructure.database.session import build_engine, build_session_factory
from qtrader.infrastructure.eventbus import InProcessEventBus

pytestmark = pytest.mark.integration

START = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
END = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


class FakeYahoo(MarketDataProvider):
    """Deterministic bar generator — no network."""

    def __init__(self) -> None:
        self.requests = 0

    async def fetch_bars(self, symbol, interval, start, end) -> list[PriceBar]:
        self.requests += 1
        bars = []
        base = Decimal("100") if symbol == "TSTC" else Decimal("50")
        step = Decimal("0.2")
        for i in range(40):
            close = base + step * i
            ts = end - timedelta(minutes=5 * (39 - i))
            bars.append(
                PriceBar(
                    symbol=symbol,
                    interval=interval,
                    ts=ts,
                    open=close - Decimal("0.1"),
                    high=close + Decimal("1.0"),
                    low=close - Decimal("1.0"),
                    close=close,
                    volume=Decimal("1000000"),
                )
            )
        return bars

    async def fetch_quote(self, symbol: str) -> PriceBar:
        raise RuntimeError("not used in this test")


@pytest.fixture(scope="module")
def session_factory() -> async_sessionmaker:
    settings = Settings(_env_file=None)
    engine = build_engine(settings)
    return build_session_factory(engine)


@pytest.fixture(scope="module")
def redis_client() -> Redis:
    return Redis.from_url(Settings(_env_file=None).redis_url, decode_responses=False)


@pytest.mark.asyncio
async def test_pipeline_backfill_then_scan(
    session_factory: async_sessionmaker, redis_client: Redis
) -> None:
    stock_repo = SQLAlchemyStockRepository(session_factory)
    price_repo = SQLAlchemyPriceRepository(session_factory)
    event_repo = SQLAlchemyEventRepository(session_factory)
    cache = RedisCache(redis_client)
    bus = InProcessEventBus(event_repo)

    # reset state for this pipeline run
    await stock_repo.upsert(Stock(symbol="TSTC", exchange="XNAS", name="Pipeline A"))
    await stock_repo.upsert(Stock(symbol="TSTD", exchange="XNAS", name="Pipeline B"))
    async with session_factory() as session:
        rows = await session.scalars(select_stocks())
        ids = [r.id for r in rows]
        await session.execute(delete(PriceModel).where(PriceModel.stock_id.in_(ids)))
        await session.execute(delete(EventRecordModel))
        await session.commit()
    for key in (f"{SCAN_ZSET_PREFIX}:overall", f"{SCAN_ZSET_PREFIX}:liquidity"):
        await redis_client.delete(key)

    # isolate the scan universe to TSTC/TSTD (the dev DB may hold real symbols)
    from sqlalchemy import select

    async with session_factory() as session:
        all_rows = (await session.scalars(select(StockModel))).all()
        deactivated = [r for r in all_rows if r.symbol not in {"TSTC", "TSTD"}]
        for row in deactivated:
            row.is_active = False
        await session.commit()

    try:
        data_agent = DataAgent(
            FakeYahoo(), price_repo, cache, bus, BarCleaner(), quote_cache_ttl_seconds=300
        )
        scanner = MarketScanner(
            prices=price_repo,
            cache=cache,
            stocks=stock_repo,
            bus=bus,
            interval=Interval.M5,
            top_k=5,
            min_dollar_volume=0.0,
            min_atr_pct=0.0,
        )
        bus.subscribe(BackfillCompleted, scanner.on_event)

        inserted_a = await data_agent.backfill("TSTC", Interval.M5, START, END)
        inserted_b = await data_agent.backfill("TSTD", Interval.M5, START, END)
        assert inserted_a == 40
        assert inserted_b == 40

        history = await price_repo.history("TSTC", Interval.M5)
        assert len(history) == 40

        quote = await cache.get("quote:TSTC")
        assert quote is not None

        # scanner ran via the BackfillCompleted subscription
        top = await cache.zrevrange(f"{SCAN_ZSET_PREFIX}:overall", 0, 4)
        assert {symbol for symbol, _ in top} == {"TSTC", "TSTD"}
        # composite scores are populated (not zeros) and rank TSTD above TSTC:
        # TSTD wins momentum & volatility, loses liquidity.
        assert top[0][0] == "TSTD"
        assert top[0][1] > 0
        assert top[1][1] < 0

        events = await event_repo.list_after(None, "ScanCompleted", 10)
        assert len(events) >= 2
        scan = events[-1]
        assert isinstance(scan, ScanCompleted)
        symbols = {c["symbol"] for c in scan.candidates}
        assert symbols == {"TSTC", "TSTD"}
    finally:
        async with session_factory() as session:
            for row in deactivated:
                row.is_active = True
            await session.commit()
        await bus.close()


def select_stocks():
    from sqlalchemy import select

    return select(StockModel).where(StockModel.symbol.in_(["TSTC", "TSTD"]))
