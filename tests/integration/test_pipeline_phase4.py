"""Integration test for the Phase 4 pipeline: Data → Scanner → Prediction → Chief.

Runs only when ``QTRADER_RUN_INTEGRATION=1`` with Postgres + Redis up.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from qtrader.application.agents.chief import ChiefAgent
from qtrader.application.agents.data import DataAgent
from qtrader.application.agents.prediction import PredictionAgent
from qtrader.application.agents.scanner import SCAN_ZSET_PREFIX, MarketScanner
from qtrader.application.agents.technical import TechnicalAgent
from qtrader.application.services.bar_cleaner import BarCleaner
from qtrader.application.services.decision_strategy import EnsembleDecisionStrategy
from qtrader.application.services.feature_store import FeatureStore
from qtrader.application.services.indicators import IndicatorEngine
from qtrader.application.services.model_trainer import ModelTrainer
from qtrader.config.settings import Settings
from qtrader.domain.entities import Signal, Stock
from qtrader.domain.events import BackfillCompleted, ScanCompleted
from qtrader.domain.ports import MarketDataProvider
from qtrader.domain.value_objects import Interval, PriceBar, SignalType
from qtrader.infrastructure.cache import RedisCache
from qtrader.infrastructure.database.models import (
    DecisionLogModel,
    EventRecordModel,
    FundamentalModel,
    IndicatorModel,
    ModelRegistryModel,
    NewsModel,
    PredictionModel,
    PriceModel,
    SignalModel,
    StockModel,
)
from qtrader.infrastructure.database.repositories import (
    SQLAlchemyDecisionRepository,
    SQLAlchemyEventRepository,
    SQLAlchemyIndicatorRepository,
    SQLAlchemyModelRepository,
    SQLAlchemyPredictionRepository,
    SQLAlchemyPriceRepository,
    SQLAlchemySignalRepository,
    SQLAlchemyStockRepository,
)
from qtrader.infrastructure.database.session import build_engine, build_session_factory
from qtrader.infrastructure.eventbus import InProcessEventBus

pytestmark = pytest.mark.integration

START = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
END = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


class FakeYahoo(MarketDataProvider):
    """Deterministic rising bar generator — no network."""

    async def fetch_bars(self, symbol, interval, start, end) -> list[PriceBar]:
        bars = []
        base = Decimal("100")
        step = Decimal("0.3")
        for i in range(300):
            close = base + step * i
            ts = end - timedelta(minutes=5 * (299 - i))
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
    engine = build_engine(Settings(_env_file=None))
    return build_session_factory(engine)


@pytest.fixture(scope="module")
def redis_client() -> Redis:
    return Redis.from_url(Settings(_env_file=None).redis_url, decode_responses=False)


@pytest.mark.asyncio
async def test_pipeline_prediction_and_decision(
    session_factory: async_sessionmaker, redis_client: Redis
) -> None:
    stock_repo = SQLAlchemyStockRepository(session_factory)
    price_repo = SQLAlchemyPriceRepository(session_factory)
    event_repo = SQLAlchemyEventRepository(session_factory)
    signal_repo = SQLAlchemySignalRepository(session_factory)
    indicator_repo = SQLAlchemyIndicatorRepository(session_factory)
    prediction_repo = SQLAlchemyPredictionRepository(session_factory)
    decision_repo = SQLAlchemyDecisionRepository(session_factory)
    model_repo = SQLAlchemyModelRepository(session_factory)
    cache = RedisCache(redis_client)
    bus = InProcessEventBus(event_repo)

    await stock_repo.upsert(Stock(symbol="TSTE", exchange="XNAS", name="Pipeline E"))
    async with session_factory() as session:
        rows = await session.scalars(
            select(StockModel).where(StockModel.symbol == "TSTE")
        )
        stock_ids = [r.id for r in rows]
        await session.execute(delete(PriceModel).where(PriceModel.stock_id.in_(stock_ids)))
        await session.execute(
            delete(IndicatorModel).where(IndicatorModel.stock_id.in_(stock_ids))
        )
        await session.execute(delete(SignalModel).where(SignalModel.stock_id.in_(stock_ids)))
        await session.execute(delete(NewsModel).where(NewsModel.stock_id.in_(stock_ids)))
        await session.execute(
            delete(FundamentalModel).where(FundamentalModel.stock_id.in_(stock_ids))
        )
        await session.execute(
            delete(PredictionModel).where(PredictionModel.stock_id.in_(stock_ids))
        )
        await session.execute(
            delete(DecisionLogModel).where(DecisionLogModel.stock_id.in_(stock_ids))
        )
        await session.execute(delete(EventRecordModel))
        await session.execute(delete(ModelRegistryModel))
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
    feature_store = FeatureStore(price_repo, indicator_repo, signal_repo)
    prediction = PredictionAgent(
        features=feature_store,
        models=model_repo,
        predictions=prediction_repo,
        bus=bus,
    )
    strategy = EnsembleDecisionStrategy()
    chief = ChiefAgent(
        signals=signal_repo,
        predictions=prediction_repo,
        decisions=decision_repo,
        bus=bus,
        strategy=strategy,
    )

    bus.subscribe(BackfillCompleted, scanner.on_event)
    bus.subscribe(ScanCompleted, technical.on_event)
    bus.subscribe(ScanCompleted, prediction.on_event)
    bus.subscribe(ScanCompleted, chief.on_event)

    await data_agent.backfill("TSTE", Interval.M5, START, END)

    predictions = await prediction_repo.latest_for_symbol("TSTE", limit=5)
    assert predictions, "prediction agent should persist rows"
    assert all(p.symbol == "TSTE" for p in predictions)
    assert all(p.prob_up is not None for p in predictions)

    # seed remaining evidence so the chief sees full coverage and BUYs
    for agent, score in (("news", 0.6), ("fundamental", 0.5)):
        await signal_repo.save(
            Signal(
                symbol="TSTE",
                agent=agent,
                signal_type=SignalType.BUY,
                score=Decimal(str(score)),
            )
        )
    record = await chief.decide_symbol("TSTE")
    assert record is not None
    assert record.symbol == "TSTE"
    assert record.decision_uuid

    decisions = await decision_repo.latest_for_symbol("TSTE", limit=5)
    assert decisions, "chief agent should persist decision rows"
    assert all(d.symbol == "TSTE" for d in decisions)
    assert all(d.decision_uuid for d in decisions)

    events = await event_repo.list_after(None, None, 100)
    types = {e.type_name for e in events}
    required = {"PredictionGenerated", "DecisionMade"}
    assert required <= types

    # trainer fits against the persisted bars and registers a version
    trainer = ModelTrainer(price_repo, model_repo, model_name="momentum")
    result = await trainer.train(
        ["TSTE"],
        Interval.M5,
        horizon_bars=12,
        lookback_bars=120,
        min_samples=50,
        promote_threshold=0.9,
    )
    assert result is not None
    active = await model_repo.load_active("momentum")
    assert active is not None
    assert active.version == result.version

    # clean up so other pipeline tests stay isolated (scanner scans all stocks)
    async with session_factory() as session:
        rows = await session.scalars(
            select(StockModel).where(StockModel.symbol == "TSTE")
        )
        stock_ids = [r.id for r in rows]
        await session.execute(delete(PriceModel).where(PriceModel.stock_id.in_(stock_ids)))
        await session.execute(
            delete(IndicatorModel).where(IndicatorModel.stock_id.in_(stock_ids))
        )
        await session.execute(delete(SignalModel).where(SignalModel.stock_id.in_(stock_ids)))
        await session.execute(delete(NewsModel).where(NewsModel.stock_id.in_(stock_ids)))
        await session.execute(
            delete(FundamentalModel).where(FundamentalModel.stock_id.in_(stock_ids))
        )
        await session.execute(
            delete(PredictionModel).where(PredictionModel.stock_id.in_(stock_ids))
        )
        await session.execute(
            delete(DecisionLogModel).where(DecisionLogModel.stock_id.in_(stock_ids))
        )
        await session.execute(delete(EventRecordModel))
        await session.execute(delete(ModelRegistryModel))
        await session.execute(delete(StockModel).where(StockModel.symbol == "TSTE"))
        await session.commit()
    await redis_client.zrem(f"{SCAN_ZSET_PREFIX}:overall", "TSTE")

    await bus.close()
