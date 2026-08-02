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
from qtrader.application.agents.execution import ExecutionAgent
from qtrader.application.agents.fundamental import FundamentalAgent
from qtrader.application.agents.news import NewsAgent
from qtrader.application.agents.portfolio import PortfolioAgent
from qtrader.application.agents.prediction import PredictionAgent
from qtrader.application.agents.risk import RiskAgent
from qtrader.application.agents.scanner import MarketScanner
from qtrader.application.agents.technical import TechnicalAgent
from qtrader.application.services.allocation_policy import EqualWeightAllocation
from qtrader.application.services.backtest import BacktestRunner
from qtrader.application.services.bar_cleaner import BarCleaner
from qtrader.application.services.dashboard_service import DashboardService
from qtrader.application.services.decision_strategy import EnsembleDecisionStrategy
from qtrader.application.services.feature_store import FeatureStore
from qtrader.application.services.indicators import IndicatorEngine
from qtrader.application.services.model_trainer import ModelTrainer
from qtrader.application.services.portfolio_service import PortfolioService
from qtrader.application.services.risk_calculator import RiskCalculator, RiskPolicy
from qtrader.application.services.system_gate import GateThresholds, SystemGate
from qtrader.application.use_cases.manual_order import ManualOrder
from qtrader.config.settings import Settings
from qtrader.domain.events import (
    AllocationProposal,
    BackfillCompleted,
    DecisionMade,
    RiskApproved,
    ScanCompleted,
)
from qtrader.domain.ports import (
    AllocationPolicy,
    BacktestRepository,
    BrokerGateway,
    Cache,
    DashboardQueries,
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
    OrderRepository,
    PerformanceRepository,
    PortfolioRepository,
    PositionRepository,
    PredictionRepository,
    PriceRepository,
    RiskRepository,
    SignalRepository,
    StockRepository,
    SystemLogRepository,
    TradeRepository,
)
from qtrader.domain.value_objects import Money, TradingMode
from qtrader.infrastructure.brokers import AlpacaBroker, PaperBroker
from qtrader.infrastructure.cache import RedisCache, RedisLock
from qtrader.infrastructure.data_providers.fundamental import StubFundamentalProvider
from qtrader.infrastructure.data_providers.yahoo import YahooFinanceProvider
from qtrader.infrastructure.database.repositories import (
    SQLAlchemyBacktestRepository,
    SQLAlchemyDashboardRepository,
    SQLAlchemyDecisionRepository,
    SQLAlchemyEventRepository,
    SQLAlchemyFundamentalRepository,
    SQLAlchemyIndicatorRepository,
    SQLAlchemyModelRepository,
    SQLAlchemyNewsRepository,
    SQLAlchemyOrderRepository,
    SQLAlchemyPerformanceRepository,
    SQLAlchemyPortfolioRepository,
    SQLAlchemyPositionRepository,
    SQLAlchemyPredictionRepository,
    SQLAlchemyPriceRepository,
    SQLAlchemyRiskRepository,
    SQLAlchemySignalRepository,
    SQLAlchemyStockRepository,
    SQLAlchemySystemLogRepository,
    SQLAlchemyTradeRepository,
)
from qtrader.infrastructure.database.session import build_engine, build_session_factory
from qtrader.infrastructure.eventbus import InProcessEventBus
from qtrader.infrastructure.llm.adapters import KeywordLLMClient, OpenAILLMClient
from qtrader.infrastructure.news.feed import RSSNewsProvider
from qtrader.infrastructure.resilience import CircuitBreakerRegistry

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
        self._broker: BrokerGateway | None = None
        self._breakers = CircuitBreakerRegistry()
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
        c.register(PositionRepository, instance=SQLAlchemyPositionRepository(session_factory))
        c.register(OrderRepository, instance=SQLAlchemyOrderRepository(session_factory))
        c.register(RiskRepository, instance=SQLAlchemyRiskRepository(session_factory))
        c.register(TradeRepository, instance=SQLAlchemyTradeRepository(session_factory))
        c.register(PriceRepository, instance=SQLAlchemyPriceRepository(session_factory))
        c.register(SignalRepository, instance=SQLAlchemySignalRepository(session_factory))
        c.register(IndicatorRepository, instance=SQLAlchemyIndicatorRepository(session_factory))
        c.register(NewsRepository, instance=SQLAlchemyNewsRepository(session_factory))
        c.register(FundamentalRepository, instance=SQLAlchemyFundamentalRepository(session_factory))
        c.register(PredictionRepository, instance=SQLAlchemyPredictionRepository(session_factory))
        c.register(DecisionRepository, instance=SQLAlchemyDecisionRepository(session_factory))
        c.register(ModelRepository, instance=SQLAlchemyModelRepository(session_factory))

        c.register(BacktestRepository, instance=SQLAlchemyBacktestRepository(session_factory))
        c.register(PerformanceRepository, instance=SQLAlchemyPerformanceRepository(session_factory))
        c.register(SystemLogRepository, instance=SQLAlchemySystemLogRepository(session_factory))
        c.register(DashboardQueries, instance=SQLAlchemyDashboardRepository(session_factory))

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

        provider = YahooFinanceProvider(
            circuit=self._breakers.get_or_create(
                "yahoo",
                failure_threshold=self._settings.provider_failure_threshold,
                reset_timeout_seconds=self._settings.provider_reset_timeout_seconds,
            )
        )
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

        portfolio_mode = (
            TradingMode.LIVE if self._settings.live_enabled else TradingMode.PAPER
        )
        portfolio_service = PortfolioService(
            c.resolve(PortfolioRepository),
            initial_capital=Money(self._settings.portfolio_initial_capital),
            mode=portfolio_mode,
        )
        c.register(PortfolioService, instance=portfolio_service)

        risk_policy = RiskPolicy(
            risk_per_trade_pct=self._settings.risk_per_trade_pct,
            max_daily_loss_pct=self._settings.max_daily_loss_pct,
            max_portfolio_exposure_pct=self._settings.max_portfolio_exposure_pct,
            max_positions=self._settings.max_positions,
            per_sector_limit_pct=self._settings.per_sector_limit_pct,
            max_position_pct_adv=self._settings.max_position_pct_adv,
            min_cooldown_minutes=self._settings.min_cooldown_minutes,
            max_trades_per_day=self._settings.max_trades_per_day,
            atr_stop_mult=self._settings.atr_stop_mult,
            take_profit_r_mult=self._settings.take_profit_r_mult,
        )
        risk_calculator = RiskCalculator(risk_policy)
        c.register(RiskCalculator, instance=risk_calculator)

        system_gate = SystemGate(
            thresholds=GateThresholds(
                min_trades=self._settings.gate_min_trades,
                min_win_rate=self._settings.gate_min_win_rate,
                min_profit_factor=self._settings.gate_min_profit_factor,
                min_sharpe=self._settings.gate_min_sharpe,
                max_drawdown=self._settings.gate_max_drawdown,
                min_total_return=self._settings.gate_min_total_return,
            ),
            performance=c.resolve(PerformanceRepository),
            logs=c.resolve(SystemLogRepository),
        )
        c.register(SystemGate, instance=system_gate)

        backtest_runner = BacktestRunner(
            prices=c.resolve(PriceRepository),
            backtests=c.resolve(BacktestRepository),
            performance=c.resolve(PerformanceRepository),
            risk_calculator=risk_calculator,
            indicator_engine=IndicatorEngine(),
            logs=c.resolve(SystemLogRepository),
        )
        c.register(BacktestRunner, instance=backtest_runner)

        c.register(
            DashboardService,
            instance=DashboardService(
                queries=c.resolve(DashboardQueries),
                portfolios=c.resolve(PortfolioRepository),
                prices=c.resolve(PriceRepository),
                risks=c.resolve(RiskRepository),
                cache=c.resolve(Cache),
                stocks=c.resolve(StockRepository),
                portfolio_service=portfolio_service,
            ),
        )

        broker: BrokerGateway
        if self._settings.broker_provider == "alpaca":
            broker = AlpacaBroker(
                api_key=self._settings.alpaca_api_key,
                secret=self._settings.alpaca_secret_key,
                live=not self._settings.alpaca_paper,
            )
        else:
            broker = PaperBroker(prices=c.resolve(PriceRepository))
        self._broker = broker
        c.register(BrokerGateway, instance=broker)
        c.register(
            AllocationPolicy,
            instance=EqualWeightAllocation(self._settings.allocation_weight_per_trade),
        )

        risk_agent = RiskAgent(
            calculator=risk_calculator,
            risk_repo=c.resolve(RiskRepository),
            portfolio_service=portfolio_service,
            positions=c.resolve(PositionRepository),
            orders=c.resolve(OrderRepository),
            prices=c.resolve(PriceRepository),
            indicators=c.resolve(IndicatorRepository),
            stocks=c.resolve(StockRepository),
            bus=bus,
        )
        c.register(RiskAgent, instance=risk_agent)

        portfolio_agent = PortfolioAgent(
            policy=c.resolve(AllocationPolicy),
            portfolio_service=portfolio_service,
            positions=c.resolve(PositionRepository),
            bus=bus,
        )
        c.register(PortfolioAgent, instance=portfolio_agent)

        execution_agent = ExecutionAgent(
            broker=broker,
            portfolio_service=portfolio_service,
            portfolios=c.resolve(PortfolioRepository),
            positions=c.resolve(PositionRepository),
            orders=c.resolve(OrderRepository),
            stocks=c.resolve(StockRepository),
            trades=c.resolve(TradeRepository),
            bus=bus,
            gate=system_gate,
            gate_strategy=self._settings.gate_strategy,
        )
        c.register(ExecutionAgent, instance=execution_agent)

        c.register(
            ManualOrder,
            instance=ManualOrder(
                portfolios=portfolio_service,
                stocks=c.resolve(StockRepository),
                prices=c.resolve(PriceRepository),
                indicators=c.resolve(IndicatorRepository),
                positions=c.resolve(PositionRepository),
                orders=c.resolve(OrderRepository),
                risk_calculator=risk_calculator,
                execution=execution_agent,
                settings=self._settings,
            ),
        )

        bus.subscribe(ScanCompleted, technical.on_event)
        bus.subscribe(ScanCompleted, news.on_event)
        bus.subscribe(ScanCompleted, fundamental.on_event)
        bus.subscribe(ScanCompleted, prediction.on_event)
        bus.subscribe(ScanCompleted, chief.on_event)

        bus.subscribe(DecisionMade, risk_agent.on_event)
        bus.subscribe(RiskApproved, portfolio_agent.on_event)
        bus.subscribe(AllocationProposal, execution_agent.on_event)

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

    def circuit_breakers(self) -> list[dict[str, object]]:
        """Snapshot of every registered circuit breaker (for observability)."""
        return self._breakers.snapshots()

    async def aclose(self) -> None:
        """Best-effort release of engine pool, redis and provider connections."""
        if self._provider is not None:
            await self._provider.close()
        if self._news_provider is not None:
            await self._news_provider.close()
        if self._broker is not None:
            await self._broker.close()
        if self._redis_client is not None:
            await self._redis_client.aclose()
        if self._engine is not None:
            await self._engine.dispose()


@lru_cache
def get_container() -> Container:
    return Container()
