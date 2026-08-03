"""Ports — the interfaces the domain/application depend on (DIP).

Infrastructure adapters implement these. Nothing outside this package may be
imported here; these are pure ABC/Protocol definitions.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal
from typing import Any, TypeVar

from qtrader.domain.entities import (
    AgentEvidence,
    AgentMetric,
    BacktestRun,
    DecisionOutcome,
    DecisionRecord,
    FundamentalData,
    IndicatorSnapshot,
    NewsItem,
    Order,
    PerformanceSummary,
    Portfolio,
    Position,
    Prediction,
    RegisteredModel,
    RiskAssessment,
    Signal,
    Stock,
    SystemLog,
    Trade,
)
from qtrader.domain.events import DomainEvent
from qtrader.domain.value_objects import (
    Interval,
    Money,
    OrderFill,
    OrderPlan,
    OrderStatus,
    PriceBar,
    TradingMode,
)

T = TypeVar("T")
EventHandler = Callable[[DomainEvent], Awaitable[None]]


# --------------------------------------------------------------------------- #
# Event bus
# --------------------------------------------------------------------------- #


class EventBus(ABC):
    """Publish/subscribe bus for domain events (agent-to-agent channel)."""

    @abstractmethod
    async def publish(self, event: DomainEvent) -> None: ...

    @abstractmethod
    def subscribe(self, event_type: type[DomainEvent], handler: EventHandler) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...


# --------------------------------------------------------------------------- #
# Cache / coordination
# --------------------------------------------------------------------------- #


class Cache(ABC):
    """Async key-value cache (Redis-backed in production)."""

    @abstractmethod
    async def get(self, key: str) -> str | None: ...

    @abstractmethod
    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None: ...

    @abstractmethod
    async def delete(self, key: str) -> None: ...

    @abstractmethod
    async def zadd(self, key: str, mapping: dict[str, float]) -> None: ...

    @abstractmethod
    async def zrevrange(self, key: str, start: int, end: int) -> list[tuple[str, float]]: ...


class Lock(ABC):
    """Distributed mutex for order execution / anti-double-submit."""

    @abstractmethod
    async def acquire(self, name: str, ttl_seconds: int = 30) -> bool: ...

    @abstractmethod
    async def release(self, name: str) -> None: ...


# --------------------------------------------------------------------------- #
# Unit of work & repositories
# --------------------------------------------------------------------------- #


class UnitOfWork(ABC):
    """Transaction boundary spanning multiple repositories.

    Implementations bind the trading repositories to one shared session:
    ``commit`` makes all writes visible atomically, ``rollback`` discards
    them. Also usable as ``async with`` context manager (commits on clean
    exit, rolls back on error).
    """

    @property
    @abstractmethod
    def stocks(self) -> StockRepository: ...

    @property
    @abstractmethod
    def portfolios(self) -> PortfolioRepository: ...

    @property
    @abstractmethod
    def positions(self) -> PositionRepository: ...

    @property
    @abstractmethod
    def orders(self) -> OrderRepository: ...

    @property
    @abstractmethod
    def trades(self) -> TradeRepository: ...

    @abstractmethod
    async def commit(self) -> None: ...

    @abstractmethod
    async def rollback(self) -> None: ...

    @abstractmethod
    async def __aenter__(self) -> UnitOfWork: ...

    @abstractmethod
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any | None,
    ) -> None: ...


class UnitOfWorkFactory(ABC):
    """Builds a UnitOfWork scoping multiple repository writes to one transaction."""

    @abstractmethod
    def __call__(self) -> UnitOfWork: ...


class StockRepository(ABC):
    @abstractmethod
    async def upsert(self, stock: Stock) -> Stock: ...

    @abstractmethod
    async def get_by_symbol(self, symbol: str, exchange: str | None = None) -> Stock | None: ...

    @abstractmethod
    async def list_active(self) -> list[Stock]: ...

    @abstractmethod
    async def search(
        self, query: str | None, sector: str | None, limit: int, offset: int
    ) -> list[Stock]: ...


class PriceRepository(ABC):
    @abstractmethod
    async def upsert_bars(self, bars: list[PriceBar]) -> int: ...

    @abstractmethod
    async def latest(self, symbol: str, interval: Interval) -> PriceBar | None: ...

    @abstractmethod
    async def history(
        self,
        symbol: str,
        interval: Interval,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> list[PriceBar]: ...


class PortfolioRepository(ABC):
    @abstractmethod
    async def create(self, portfolio: Portfolio) -> Portfolio: ...

    @abstractmethod
    async def get(self, portfolio_id: int) -> Portfolio | None: ...

    @abstractmethod
    async def first(self) -> Portfolio | None:
        """Return any existing portfolio (lowest id), or None."""

    @abstractmethod
    async def save(self, portfolio: Portfolio) -> Portfolio: ...


class PositionRepository(ABC):
    @abstractmethod
    async def open_positions(self, portfolio_id: int) -> list[Position]: ...

    @abstractmethod
    async def save(self, position: Position) -> Position: ...


class OrderRepository(ABC):
    @abstractmethod
    async def create(self, order: Order) -> Order: ...

    @abstractmethod
    async def save(self, order: Order) -> Order: ...

    @abstractmethod
    async def get_by_idempotency_key(self, key: str) -> Order | None: ...

    @abstractmethod
    async def list_by_portfolio(
        self, portfolio_id: int, status: OrderStatus | None = None, limit: int = 100
    ) -> list[Order]: ...


class SignalRepository(ABC):
    @abstractmethod
    async def save(self, signal: Signal) -> Signal: ...

    @abstractmethod
    async def latest_for_symbol(self, symbol: str, agent: str | None = None) -> list[Signal]: ...


class IndicatorRepository(ABC):
    @abstractmethod
    async def save_snapshot(self, snapshot: IndicatorSnapshot) -> None: ...

    @abstractmethod
    async def latest(self, symbol: str, interval: Interval) -> IndicatorSnapshot | None: ...


class NewsRepository(ABC):
    @abstractmethod
    async def upsert(self, items: list[NewsItem]) -> int: ...

    @abstractmethod
    async def recent(
        self, symbol: str | None, since: datetime | None, limit: int
    ) -> list[NewsItem]: ...


class FundamentalRepository(ABC):
    @abstractmethod
    async def upsert(self, data: FundamentalData) -> FundamentalData: ...

    @abstractmethod
    async def latest(self, symbol: str) -> FundamentalData | None: ...


class PredictionRepository(ABC):
    @abstractmethod
    async def save(self, prediction: Prediction) -> Prediction: ...

    @abstractmethod
    async def latest_for_symbol(self, symbol: str, limit: int = 20) -> list[Prediction]: ...


class RiskRepository(ABC):
    """Persisted risk-gate evaluations (``risk_history``)."""

    @abstractmethod
    async def record(self, assessment: RiskAssessment) -> RiskAssessment: ...

    @abstractmethod
    async def recent(self, limit: int = 50) -> list[RiskAssessment]: ...


class TradeRepository(ABC):
    """Closed P/L records (Memory System core)."""

    @abstractmethod
    async def record(self, trade: Trade) -> Trade: ...


class AllocationPolicy(ABC):
    """Capital allocation strategy — turns a risk-approved plan into a size."""

    @abstractmethod
    def quantity_for(self, plan: OrderPlan, cash: Money, open_positions: int) -> Decimal: ...


class DecisionRepository(ABC):
    @abstractmethod
    async def save(self, record: DecisionRecord) -> DecisionRecord: ...

    @abstractmethod
    async def latest_for_symbol(
        self, symbol: str, limit: int = 20
    ) -> list[DecisionRecord]: ...


class ModelRepository(ABC):
    """Versioned ML model registry (active version used for inference)."""

    @abstractmethod
    async def load_active(self, name: str) -> RegisteredModel | None: ...

    @abstractmethod
    async def create_version(
        self,
        name: str,
        hyperparams: dict[str, Any],
        training_window: str | None,
        offline_metrics: dict[str, Any],
    ) -> int: ...

    @abstractmethod
    async def promote(self, name: str, version: int) -> None: ...


class DecisionStrategy(ABC):
    """Pluggable decision engine — fuses evidence streams into a Decision."""

    @abstractmethod
    def decide(self, evidence: list[AgentEvidence]) -> DecisionOutcome: ...


class EventRepository(ABC):
    """Outbox / audit journal of every domain event."""

    @abstractmethod
    async def record(self, event: DomainEvent) -> None: ...

    @abstractmethod
    async def list_after(
        self, event_uuid: str | None, event_type: str | None, limit: int
    ) -> list[DomainEvent]: ...

    @abstractmethod
    async def count_by_type(self, limit: int = 1000) -> dict[str, int]:
        """Counts of the ``limit`` most recent events, grouped by type.

        Cheap alternative to loading rows just to tally them (used by
        ``/system/metrics`` monitoring).
        """


class BacktestRepository(ABC):
    """Persistence for backtest runs (``backtest_runs``)."""

    @abstractmethod
    async def create(self, run: BacktestRun) -> BacktestRun: ...

    @abstractmethod
    async def save(self, run: BacktestRun) -> BacktestRun: ...

    @abstractmethod
    async def get(self, run_id: int) -> BacktestRun | None: ...

    @abstractmethod
    async def latest(self, name: str | None = None, limit: int = 5) -> list[BacktestRun]: ...


class PerformanceRepository(ABC):
    """Aggregate strategy metrics (``strategy_performance``)."""

    @abstractmethod
    async def upsert(self, summary: PerformanceSummary) -> PerformanceSummary: ...

    @abstractmethod
    async def latest_for_strategy(
        self, strategy: str, mode: TradingMode
    ) -> PerformanceSummary | None: ...


class SystemLogRepository(ABC):
    """Audit/journal entries (``system_logs``)."""

    @abstractmethod
    async def record(self, entry: SystemLog) -> SystemLog: ...

    @abstractmethod
    async def recent(
        self, level: str | None = None, component: str | None = None, limit: int = 50
    ) -> list[SystemLog]: ...


# --------------------------------------------------------------------------- #
# External adapters
# --------------------------------------------------------------------------- #


class MarketDataProvider(ABC):
    """Source of bars/quotes. Implemented by Yahoo/Polygon/static feeds."""

    @abstractmethod
    async def fetch_bars(
        self, symbol: str, interval: Interval, start: datetime, end: datetime
    ) -> list[PriceBar]: ...

    @abstractmethod
    async def fetch_quote(self, symbol: str) -> PriceBar: ...


class BrokerGateway(ABC):
    """Execution contract shared by Paper/Alpaca/IBKR/Backtest brokers."""

    @abstractmethod
    async def submit_order(self, order: Order) -> str: ...

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> None: ...

    @abstractmethod
    async def modify_brackets(
        self, position_id: str, stop_loss: Money, take_profit: Money
    ) -> None: ...

    @abstractmethod
    async def get_order_status(self, broker_order_id: str) -> OrderFill: ...

    async def close(self) -> None:  # noqa: B027
        """Release client resources (default: nothing to clean up)."""


class NewsProvider(ABC):
    @abstractmethod
    async def fetch_news(
        self, symbol: str | None, since: datetime, limit: int
    ) -> list[NewsItem]: ...


class FundamentalProvider(ABC):
    """Source of financial statements / valuation metrics."""

    @abstractmethod
    async def fetch_fundamentals(self, symbol: str) -> FundamentalData | None: ...


class LLMClient(ABC):
    """Provider-agnostic LLM call (OpenAI/Anthropic/local)."""

    @abstractmethod
    async def complete_json(self, system_prompt: str, user_prompt: str, schema: type[T]) -> T: ...


class DashboardQueries(ABC):
    """Read-side aggregation for the dashboard (Phase 7).

    Kept separate from the write repositories so dashboard routes never touch
    ORM models directly; a single adapter implements all of these.
    """

    @abstractmethod
    async def positions(self, portfolio_id: int) -> list[Position]: ...

    @abstractmethod
    async def trades(
        self, portfolio_id: int, since: datetime | None = None, limit: int = 100
    ) -> list[Trade]: ...

    @abstractmethod
    async def logs(
        self, level: str | None = None, component: str | None = None, limit: int = 50
    ) -> list[SystemLog]: ...

    @abstractmethod
    async def agent_metrics(
        self, agent_name: str | None = None, limit: int = 50
    ) -> list[AgentMetric]: ...

    @abstractmethod
    async def performance(
        self,
        strategy: str | None = None,
        mode: TradingMode | None = None,
        limit: int = 50,
    ) -> list[PerformanceSummary]: ...

    @abstractmethod
    async def models(self) -> list[RegisteredModel]: ...


class AgentMetricRepository(ABC):
    """Write-side for per-agent dashboard metrics (``agent_metrics``)."""

    @abstractmethod
    async def record(self, metric: AgentMetric) -> AgentMetric: ...
