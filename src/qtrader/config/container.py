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

from qtrader.application.agents.chief import ChiefAgent
from qtrader.application.agents.data import DataAgent
from qtrader.application.agents.fundamental import FundamentalAgent
from qtrader.application.agents.news import NewsAgent
from qtrader.application.agents.prediction import PredictionAgent
from qtrader.application.agents.scanner import MarketScanner
from qtrader.application.agents.technical import TechnicalAgent
from qtrader.application.services.bar_cleaner import BarCleaner
from qtrader.application.services.decision_strategy import EnsembleDecisionStrategy
from qtrader.application.services.feature_store import FeatureStore
from qtrader.application.services.indicators import IndicatorEngine
from qtrader.application.services.model_trainer import ModelTrainer
from qtrader.config.settings import Settings
from qtrader.domain.events import BackfillCompleted, ScanCompleted
from qtrader.domain.ports import (
    Cache,
    DecisionRepository,
    DecisionStrategy,
    EventBus,
    EventRepository,
    FundamentalProvider,
    FundamentalRepository,
    IndicatorRepository,
    LLMClient,
    Lock,
    MarketDataProvider,
    ModelRepository,
    NewsProvider,
    NewsRepository,
    PortfolioRepository,
    PredictionRepository,
    PriceRepository,
    SignalRepository,
    StockRepository,
)
from qtrader.infrastructure.cache import RedisCache, RedisLock
from qtrader.infrastructure.data_providers.fundamental import StubFundamentalProvider
from qtrader.infrastructure.data_providers.yahoo import YahooFinanceProvider
from qtrader.infrastructure.database.repositories import (
    SQLAlchemyDecisionRepository,
    SQLAlchemyEventRepository,
    SQLAlchemyFundamentalRepository,
    SQLAlchemyIndicatorRepository,
    SQLAlchemyModelRepository,
    SQLAlchemyNewsRepository,
    SQLAlchemyPortfolioRepository,
    SQLAlchemyPredictionRepository,
    SQLAlchemyPriceRepository,
    SQLAlchemySignalRepository,
    SQLAlchemyStockRepository,
)
from qtrader.infrastructure.database.session import build_engine, build_session_factory
from qtrader.infrastructure.eventbus import InProcessEventBus
from qtrader.infrastructure.llm.adapters import KeywordLLMClient, OpenAILLMClient
from qtrader.infrastructure.news.feed import RSSNewsProvider

T = TypeVar("T")


class Container:
    """Thin wrapper over punq that registers the production graph."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()
        self._container = punq.Container()
        self._engine: AsyncEngine | None = None
        self._redis_client: Redis | None = None
        self._provider: YahooFinanceProvider | None = None
        self._news_provider: RSSNewsProvider | None = None
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
        c.register(SignalRepository, instance=SQLAlchemySignalRepository(session_factory))
        c.register(IndicatorRepository, instance=SQLAlchemyIndicatorRepository(session_factory))
        c.register(NewsRepository, instance=SQLAlchemyNewsRepository(session_factory))
        c.register(FundamentalRepository, instance=SQLAlchemyFundamentalRepository(session_factory))
        c.register(PredictionRepository, instance=SQLAlchemyPredictionRepository(session_factory))
        c.register(DecisionRepository, instance=SQLAlchemyDecisionRepository(session_factory))
        c.register(ModelRepository, instance=SQLAlchemyModelRepository(session_factory))

        c.register(FundamentalProvider, instance=StubFundamentalProvider())

        llm: LLMClient
        if self._settings.openai_api_key:
            llm = OpenAILLMClient(api_key=self._settings.openai_api_key)
        else:
            llm = KeywordLLMClient()
        c.register(LLMClient, instance=llm)
        self._news_provider = RSSNewsProvider()
        c.register(NewsProvider, instance=self._news_provider)

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

        technical = TechnicalAgent(
            prices=c.resolve(PriceRepository),
            indicators=c.resolve(IndicatorRepository),
            signals=c.resolve(SignalRepository),
            bus=bus,
            engine=IndicatorEngine(),
            interval=self._settings.scan_interval,
            history_limit=self._settings.technical_history_bars,
            min_bars=self._settings.technical_min_bars,
        )
        c.register(TechnicalAgent, instance=technical)

        news = NewsAgent(
            provider=c.resolve(NewsProvider),
            news_repo=c.resolve(NewsRepository),
            signals=c.resolve(SignalRepository),
            bus=bus,
            llm=c.resolve(LLMClient),
            lookback_hours=self._settings.news_lookback_hours,
            per_symbol_limit=self._settings.news_per_symbol_limit,
        )
        c.register(NewsAgent, instance=news)

        fundamental = FundamentalAgent(
            provider=c.resolve(FundamentalProvider),
            fundamentals=c.resolve(FundamentalRepository),
            signals=c.resolve(SignalRepository),
            bus=bus,
            max_age_days=self._settings.fundamental_max_age_days,
        )
        c.register(FundamentalAgent, instance=fundamental)

        feature_store = FeatureStore(
            prices=c.resolve(PriceRepository),
            indicators=c.resolve(IndicatorRepository),
            signals=c.resolve(SignalRepository),
        )
        c.register(FeatureStore, instance=feature_store)

        strategy = EnsembleDecisionStrategy(
            weights=self._settings.decision_weights_dict,
            buy_threshold=self._settings.decision_buy_threshold,
            sell_threshold=self._settings.decision_sell_threshold,
            conflict_threshold=self._settings.decision_conflict_threshold,
            min_coverage=self._settings.decision_min_coverage,
        )
        c.register(DecisionStrategy, instance=strategy)

        prediction = PredictionAgent(
            features=feature_store,
            models=c.resolve(ModelRepository),
            predictions=c.resolve(PredictionRepository),
            bus=bus,
            model_name=self._settings.prediction_model_name,
            horizon=self._settings.prediction_horizon,
            interval=self._settings.scan_interval,
            lookback_bars=self._settings.prediction_lookback_bars,
            min_bars=self._settings.prediction_min_bars,
        )
        c.register(PredictionAgent, instance=prediction)

        chief = ChiefAgent(
            signals=c.resolve(SignalRepository),
            predictions=c.resolve(PredictionRepository),
            decisions=c.resolve(DecisionRepository),
            bus=bus,
            strategy=strategy,
        )
        c.register(ChiefAgent, instance=chief)

        trainer = ModelTrainer(
            prices=c.resolve(PriceRepository),
            model_repo=c.resolve(ModelRepository),
            model_name=self._settings.prediction_model_name,
        )
        c.register(ModelTrainer, instance=trainer)

        bus.subscribe(ScanCompleted, technical.on_event)
        bus.subscribe(ScanCompleted, news.on_event)
        bus.subscribe(ScanCompleted, fundamental.on_event)
        bus.subscribe(ScanCompleted, prediction.on_event)
        bus.subscribe(ScanCompleted, chief.on_event)

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
        if self._news_provider is not None:
            await self._news_provider.close()
        if self._redis_client is not None:
            await self._redis_client.aclose()
        if self._engine is not None:
            await self._engine.dispose()


@lru_cache
def get_container() -> Container:
    return Container()
