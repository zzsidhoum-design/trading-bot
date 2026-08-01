"""Integration test for the Phase 3 pipeline: Data → Scanner → analysis agents.

Runs only when ``QTRADER_RUN_INTEGRATION=1`` with Postgres + Redis up.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from qtrader.application.agents.data import DataAgent
from qtrader.application.agents.fundamental import FundamentalAgent
from qtrader.application.agents.news import NewsAgent
from qtrader.application.agents.scanner import SCAN_ZSET_PREFIX, MarketScanner
from qtrader.application.agents.technical import TechnicalAgent
from qtrader.application.services.bar_cleaner import BarCleaner
from qtrader.application.services.indicators import IndicatorEngine
from qtrader.config.settings import Settings
from qtrader.domain.entities import NewsItem, Stock
from qtrader.domain.events import (
    BackfillCompleted,
    ScanCompleted,
)
from qtrader.domain.ports import MarketDataProvider, NewsProvider
from qtrader.domain.value_objects import Interval, PriceBar
from qtrader.infrastructure.cache import RedisCache
from qtrader.infrastructure.data_providers.fundamental import StubFundamentalProvider
from qtrader.infrastructure.database.models import (
    EventRecordModel,
    IndicatorModel,
    NewsModel,
    PriceModel,
    SignalModel,
    StockModel,
)
from qtrader.infrastructure.database.repositories import (
    SQLAlchemyEventRepository,
    SQLAlchemyFundamentalRepository,
    SQLAlchemyIndicatorRepository,
    SQLAlchemyNewsRepository,
    SQLAlchemyPriceRepository,
    SQLAlchemySignalRepository,
    SQLAlchemyStockRepository,
)
from qtrader.infrastructure.database.session import build_engine, build_session_factory
from qtrader.infrastructure.eventbus import InProcessEventBus
from qtrader.infrastructure.llm.adapters import KeywordLLMClient

pytestmark = pytest.mark.integration

START = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
END = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


class FakeYahoo(MarketDataProvider):
    """Deterministic bar generator — no network."""

    async def fetch_bars(self, symbol, interval, start, end) -> list[PriceBar]:
        bars = []
        base = Decimal("100") if symbol == "TSTC" else Decimal("50")
        step = Decimal("0.2")
        for i in range(260):
            close = base + step * i
            ts = end - timedelta(minutes=5 * (259 - i))
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


class FakeNewsProvider(NewsProvider):
    async def fetch_news(self, symbol, since, limit) -> list[NewsItem]:
        return [
            NewsItem(
                symbol=symbol,
                source="test",
                title=f"{symbol} beats earnings, profit surges",
                url=f"https://example.com/{symbol}/1",
                published_at=datetime.now(UTC) - timedelta(hours=1),
                content=f"{symbol} reported strong results.",
            )
        ]


@pytest.fixture(scope="module")
def session_factory() -> async_sessionmaker:
    engine = build_engine(Settings(_env_file=None))
    return build_session_factory(engine)


@pytest.fixture(scope="module")
def redis_client() -> Redis:
    return Redis.from_url(Settings(_env_file=None).redis_url, decode_responses=False)


@pytest.mark.asyncio
async def test_pipeline_analysis_cycle(
    session_factory: async_sessionmaker, redis_client: Redis
) -> None:
    stock_repo = SQLAlchemyStockRepository(session_factory)
    price_repo = SQLAlchemyPriceRepository(session_factory)
    event_repo = SQLAlchemyEventRepository(session_factory)
    signal_repo = SQLAlchemySignalRepository(session_factory)
    indicator_repo = SQLAlchemyIndicatorRepository(session_factory)
    news_repo = SQLAlchemyNewsRepository(session_factory)
    fund_repo = SQLAlchemyFundamentalRepository(session_factory)
    cache = RedisCache(redis_client)
    bus = InProcessEventBus(event_repo)

    # reset state for this pipeline run
    await stock_repo.upsert(Stock(symbol="TSTC", exchange="XNAS", name="Pipeline C"))
    await stock_repo.upsert(Stock(symbol="TSTD", exchange="XNAS", name="Pipeline D"))
    async with session_factory() as session:
        rows = await session.scalars(
            select(StockModel).where(StockModel.symbol.in_(["TSTC", "TSTD"]))
        )
        ids = [r.id for r in rows]
        await session.execute(delete(PriceModel).where(PriceModel.stock_id.in_(ids)))
        await session.execute(delete(IndicatorModel).where(IndicatorModel.stock_id.in_(ids)))
        await session.execute(delete(SignalModel).where(SignalModel.stock_id.in_(ids)))
        await session.execute(delete(NewsModel).where(NewsModel.stock_id.in_(ids)))
        await session.execute(delete(EventRecordModel))
        await session.commit()
    await redis_client.delete(f"{SCAN_ZSET_PREFIX}:overall")

    data_agent = DataAgent(FakeYahoo(), price_repo, cache, bus, BarCleaner())
    scanner = MarketScanner(
        prices=price_repo,
        cache=cache,
        stocks=stock_repo,
        bus=bus,
        top_k=5,
        min_dollar_volume=0.0,
        min_atr_pct=0.0,
    )
    technical = TechnicalAgent(
        price_repo, indicator_repo, signal_repo, bus, engine=IndicatorEngine(), history_limit=260
    )
    news = NewsAgent(
        FakeNewsProvider(), news_repo, signal_repo, bus, llm=KeywordLLMClient()
    )
    fundamental = FundamentalAgent(
        StubFundamentalProvider(), fund_repo, signal_repo, bus
    )
    bus.subscribe(BackfillCompleted, scanner.on_event)
    bus.subscribe(ScanCompleted, technical.on_event)
    bus.subscribe(ScanCompleted, news.on_event)
    bus.subscribe(ScanCompleted, fundamental.on_event)

    await data_agent.backfill("TSTC", Interval.M5, START, END)
    await data_agent.backfill("TSTD", Interval.M5, START, END)

    # scanner + analysis agents ran via bus subscriptions
    snap = await indicator_repo.latest("TSTC", Interval.M5)
    assert snap is not None
    assert snap.rsi is not None and snap.adx is not None

    signals = await signal_repo.latest_for_symbol("TSTC")
    agents = {s.agent for s in signals}
    assert {"technical", "news", "fundamental"} <= agents

    events = await event_repo.list_after(None, None, 100)
    types = {e.type_name for e in events}
    required = {"TechnicalSignalGenerated", "NewsSignalGenerated", "FundamentalSignalGenerated"}
    assert required <= types

    await bus.close()
