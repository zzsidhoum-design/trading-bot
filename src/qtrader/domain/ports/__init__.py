"""Ports â€” the interfaces the domain/application depend on (DIP).

Infrastructure adapters implement these. Nothing outside this package may be
imported here; these are pure ABC/Protocol definitions.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from qtrader.domain.events import DomainEvent
from qtrader.domain.value_objects import PriceBar

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
    """Transaction boundary. Commit on success, rollback on error."""

    @abstractmethod
    async def commit(self) -> None: ...

    @abstractmethod
    async def rollback(self) -> None: ...


class StockRepository(ABC):
    @abstractmethod
    async def upsert(self, stock: Any) -> Any: ...

    @abstractmethod
    async def get_by_symbol(self, symbol: str, exchange: str | None = None) -> Any | None: ...

    @abstractmethod
    async def list_active(self) -> list[Any]: ...

    @abstractmethod
    async def search(
        self, query: str | None, sector: str | None, limit: int, offset: int
    ) -> list[Any]: ...


class PriceRepository(ABC):
    @abstractmethod
    async def upsert_bars(self, bars: list[PriceBar]) -> int: ...

    @abstractmethod
    async def latest(self, symbol: str, interval: Any) -> PriceBar | None: ...

    @abstractmethod
    async def history(
        self,
        symbol: str,
        interval: Any,
        start: Any | None = None,
        end: Any | None = None,
        limit: int = 500,
    ) -> list[PriceBar]: ...


class PortfolioRepository(ABC):
    @abstractmethod
    async def create(self, portfolio: Any) -> Any: ...

    @abstractmethod
    async def get(self, portfolio_id: int) -> Any | None: ...


class PositionRepository(ABC):
    @abstractmethod
    async def open_positions(self, portfolio_id: int) -> list[Any]: ...

    @abstractmethod
    async def save(self, position: Any) -> Any: ...


class OrderRepository(ABC):
    @abstractmethod
    async def create(self, order: Any) -> Any: ...

    @abstractmethod
    async def save(self, order: Any) -> Any: ...

    @abstractmethod
    async def get_by_idempotency_key(self, key: str) -> Any | None: ...

    @abstractmethod
    async def list_by_portfolio(
        self, portfolio_id: int, status: Any | None = None, limit: int = 100
    ) -> list[Any]: ...


class SignalRepository(ABC):
    @abstractmethod
    async def save(self, signal: Any) -> Any: ...

    @abstractmethod
    async def latest_for_symbol(self, symbol: str, agent: str | None = None) -> list[Any]: ...


class EventRepository(ABC):
    """Outbox / audit journal of every domain event."""

    @abstractmethod
    async def record(self, event: DomainEvent) -> None: ...

    @abstractmethod
    async def list_after(
        self, event_uuid: str | None, event_type: str | None, limit: int
    ) -> list[DomainEvent]: ...


# --------------------------------------------------------------------------- #
# External adapters (defined now to fix contracts; implemented in later phases)
# --------------------------------------------------------------------------- #


class MarketDataProvider(ABC):
    """Source of bars/quotes. Implemented by Yahoo/Polygon/static feeds."""

    @abstractmethod
    async def fetch_bars(
        self, symbol: str, interval: Any, start: Any, end: Any
    ) -> list[PriceBar]: ...

    @abstractmethod
    async def fetch_quote(self, symbol: str) -> PriceBar: ...


class BrokerGateway(ABC):
    """Execution contract shared by Paper/Alpaca/IBKR/Backtest brokers."""

    @abstractmethod
    async def submit_order(self, order: Any) -> str: ...

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> None: ...

    @abstractmethod
    async def modify_brackets(self, position_id: str, stop_loss: Any, take_profit: Any) -> None: ...

    @abstractmethod
    async def get_order_status(self, broker_order_id: str) -> Any: ...


class NewsProvider(ABC):
    @abstractmethod
    async def fetch_news(self, symbol: str | None, since: Any, limit: int) -> list[Any]: ...


class LLMClient(ABC):
    """Provider-agnostic LLM call (OpenAI/Anthropic/local)."""

    @abstractmethod
    async def complete_json(self, system_prompt: str, user_prompt: str, schema: type[T]) -> T: ...


class ModelRepository(ABC):
    @abstractmethod
    async def load_active(self, name: str) -> Any | None: ...

    @abstractmethod
    async def register(
        self, name: str, version: int, artifact_path: str, metrics: dict[str, Any]
    ) -> None: ...
