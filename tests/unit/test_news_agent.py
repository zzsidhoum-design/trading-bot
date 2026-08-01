"""Unit tests for the News Agent (fake provider/repos/bus + Keyword LLM)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from qtrader.application.agents.news import NewsAgent
from qtrader.domain.entities import NewsItem, Signal
from qtrader.domain.events import DomainEvent, NewsSignalGenerated, ScanCompleted
from qtrader.domain.ports import (
    EventBus,
    LLMClient,
    NewsProvider,
    NewsRepository,
    SignalRepository,
)
from qtrader.infrastructure.llm.adapters import KeywordLLMClient

NOW = datetime.now(UTC)

_UNSET = object()


def _item(title: str, url: str, sentiment: Decimal | None = None) -> NewsItem:
    return NewsItem(
        symbol="AAPL",
        source="test",
        title=title,
        url=url,
        published_at=NOW - timedelta(hours=1),
        content=f"{title} details",
        sentiment_score=sentiment,
    )


class FakeNewsProvider(NewsProvider):
    def __init__(self, items: list[NewsItem]) -> None:
        self._items = items

    async def fetch_news(self, symbol, since, limit) -> list[NewsItem]:
        return [i for i in self._items if i.published_at >= since][:limit]


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

    async def recent(self, symbol, since, limit) -> list[NewsItem]:
        return self.items[:limit]


class FakeSignalRepository(SignalRepository):
    def __init__(self) -> None:
        self.signals: list[Signal] = []

    async def save(self, signal: Signal) -> Signal:
        self.signals.append(signal)
        return signal

    async def latest_for_symbol(self, symbol, agent=None) -> list[Signal]:
        return [
            s
            for s in self.signals
            if s.symbol == symbol and (agent is None or s.agent == agent)
        ]


class FakeEventBus(EventBus):
    def __init__(self) -> None:
        self.published: list[DomainEvent] = []

    async def publish(self, event: DomainEvent) -> None:
        self.published.append(event)

    def subscribe(self, event_type, handler) -> None:
        pass

    async def close(self) -> None:
        pass


def _agent(
    items: list[NewsItem],
    llm: LLMClient | None | object = _UNSET,
    bus: FakeEventBus | None = None,
) -> tuple[NewsAgent, FakeNewsRepository, FakeSignalRepository, FakeEventBus]:
    news_repo = FakeNewsRepository()
    signals = FakeSignalRepository()
    bus = bus or FakeEventBus()
    agent = NewsAgent(
        FakeNewsProvider(items),
        news_repo,
        signals,
        bus,
        llm=KeywordLLMClient() if llm is _UNSET else llm,
        lookback_hours=24,
        per_symbol_limit=20,
    )
    return agent, news_repo, signals, bus


@pytest.mark.asyncio
async def test_analyze_symbol_positive_news() -> None:
    items = [
        _item("AAPL beats earnings, profit surges", "https://x.com/1"),
        _item("AAPL grows revenue, record sales", "https://x.com/2"),
    ]
    agent, news_repo, signals, bus = _agent(items)
    score = await agent.analyze_symbol("AAPL")
    assert score > 0
    assert len(news_repo.items) == 2
    assert news_repo.items[0].sentiment_score is not None
    assert len(signals.signals) == 1
    assert signals.signals[0].agent == "news"
    assert len(bus.published) == 1
    event = bus.published[0]
    assert isinstance(event, NewsSignalGenerated)
    assert event.symbol == "AAPL"
    assert event.impact in {"LOW", "MEDIUM", "HIGH"}


@pytest.mark.asyncio
async def test_analyze_symbol_no_items_publishes_nothing() -> None:
    agent, news_repo, signals, bus = _agent([])
    score = await agent.analyze_symbol("AAPL")
    assert score == 0.0
    assert news_repo.items == []
    assert signals.signals == []
    assert bus.published == []


@pytest.mark.asyncio
async def test_analyze_without_llm_keeps_raw() -> None:
    agent, news_repo, signals, bus = _agent([_item("plain headline", "https://x.com/3")], llm=None)
    score = await agent.analyze_symbol("AAPL")
    assert score == 0.0
    assert len(news_repo.items) == 1
    assert news_repo.items[0].sentiment_score is None
    assert signals.signals == []
    assert bus.published == []


@pytest.mark.asyncio
async def test_scan_completed_triggers_news() -> None:
    agent, news_repo, signals, bus = _agent([_item("AAPL beats earnings", "https://x.com/4")])
    await agent.on_event(
        ScanCompleted(candidates=[{"symbol": "AAPL", "score": 0.9}])
    )
    assert len(signals.signals) == 1
    assert len(bus.published) == 1
