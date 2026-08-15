"""Shared fakes for the Phase 6 AI layer tests (not collected by pytest)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from qtrader.application.ai.models import SentimentResult
from qtrader.domain.entities import NewsItem, Prediction, Signal
from qtrader.domain.events import DomainEvent
from qtrader.domain.ports import (
    EventBus,
    NewsProvider,
    NewsRepository,
    PredictionRepository,
    SignalRepository,
)
from qtrader.domain.value_objects import Interval, PriceBar, SignalType


class FakeEventBus(EventBus):
    def __init__(self) -> None:
        self.published: list[DomainEvent] = []

    async def publish(self, event: DomainEvent) -> None:
        self.published.append(event)

    def subscribe(self, event_type: type[DomainEvent], handler: object) -> None:
        pass

    async def close(self) -> None:
        pass


def make_signal(
    symbol: str,
    agent: str,
    score: float,
    *,
    signal_type: SignalType | None = None,
    created_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> Signal:
    return Signal(
        symbol=symbol,
        agent=agent,
        signal_type=signal_type or _signal_type(score),
        score=Decimal(str(score)),
        created_at=created_at or datetime.now(UTC),
        metadata=metadata or {},
    )


def _signal_type(score: float) -> SignalType:
    if score >= 0.5:
        return SignalType.BUY
    if score <= -0.5:
        return SignalType.SELL
    return SignalType.HOLD


class FakeSignalRepository(SignalRepository):
    def __init__(self) -> None:
        self.rows: list[Signal] = []

    async def save(self, signal: Signal) -> Signal:
        self.rows.append(signal)
        return signal

    async def latest_for_symbol(
        self, symbol: str, agent: str | None = None
    ) -> list[Signal]:
        rows = [r for r in self.rows if r.symbol == symbol]
        if agent is not None:
            rows = [r for r in rows if r.agent == agent]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows


class FakePredictionRepository(PredictionRepository):
    def __init__(self) -> None:
        self.rows: list[Prediction] = []

    async def save(self, prediction: Prediction) -> Prediction:
        self.rows.append(prediction)
        return prediction

    async def latest_for_symbol(
        self, symbol: str, limit: int = 20
    ) -> list[Prediction]:
        rows = [r for r in self.rows if r.symbol == symbol]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[:limit]


class FakeNewsProvider(NewsProvider):
    def __init__(self, items: list[NewsItem] | None = None) -> None:
        self.items = items or []
        self.calls: list[tuple[str | None, datetime, int]] = []

    async def fetch_news(
        self, symbol: str | None, since: datetime, limit: int
    ) -> list[NewsItem]:
        self.calls.append((symbol, since, limit))
        return [i for i in self.items if i.published_at >= since][:limit]


class FakeNewsRepository(NewsRepository):
    def __init__(self) -> None:
        self.items: list[NewsItem] = []

    async def upsert(self, items: list[NewsItem]) -> int:
        by_url = {i.url: i for i in self.items}
        inserted = 0
        for item in items:
            if item.url not in by_url:
                inserted += 1
            by_url[item.url] = item
        self.items = list(by_url.values())
        return inserted

    async def recent(
        self, symbol: str | None, since: datetime, limit: int
    ) -> list[NewsItem]:
        return [i for i in self.items if i.published_at >= since][:limit]


class StubSentimentModel:
    """Deterministic sentiment model for pipeline tests."""

    def __init__(self, result: SentimentResult | None = None) -> None:
        self._result = result
        self.calls: list[tuple[str, str | None]] = []

    def analyze(
        self, text: str, *, symbol: str | None = None
    ) -> SentimentResult:
        self.calls.append((text, symbol))
        if self._result is not None:
            return self._result
        return SentimentResult(
            sentiment=0.5,
            confidence=0.7,
            relevance=0.9,
            model="stub",
        )


def make_price_bars(
    symbol: str,
    closes: list[float],
    *,
    interval: Interval = Interval.D1,
    start: datetime | None = None,
    volume: int = 1_000_000,
) -> list[PriceBar]:
    """Build bars with open=close=previous close (deterministic flat bars)."""
    start = start or datetime(2024, 1, 1, tzinfo=UTC)
    bars: list[PriceBar] = []
    previous = closes[0]
    for index, close in enumerate(closes):
        ts = start + timedelta(days=index)
        open_price = previous if index > 0 else close
        bars.append(
            PriceBar(
                symbol=symbol,
                interval=interval,
                ts=ts,
                open=Decimal(str(open_price)),
                high=Decimal(str(max(open_price, close))),
                low=Decimal(str(min(open_price, close))),
                close=Decimal(str(close)),
                volume=volume,
            )
        )
        previous = close
    return bars


def rising_closes(count: int = 260, *, start: float = 100.0, step: float = 0.4) -> list[float]:
    return [start + step * i for i in range(count)]


def falling_closes(count: int = 260, *, start: float = 200.0, step: float = 0.4) -> list[float]:
    return [start - step * i for i in range(count)]


def sideways_closes(count: int = 260, *, start: float = 100.0) -> list[float]:
    """A perfectly flat series (close == SMA) — the engine calls this SIDEWAYS."""
    return [start] * count
