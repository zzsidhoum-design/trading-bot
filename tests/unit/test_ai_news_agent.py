"""Phase 6 — News Agent sentiment-model seam (FinBERT/lexicon plug-in)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from qtrader.application.agents.news import NewsAgent
from qtrader.application.ai.models import SentimentResult
from qtrader.domain.entities import NewsItem
from tests.unit.fakes_ai import (
    FakeEventBus,
    FakeNewsProvider,
    FakeNewsRepository,
    FakeSignalRepository,
    StubSentimentModel,
)

NOW = datetime.now(UTC)


def _item(title: str, url: str) -> NewsItem:
    return NewsItem(
        symbol="AAPL",
        source="test",
        title=title,
        url=url,
        published_at=NOW - timedelta(hours=1),
        content=f"{title} details",
    )


def _agent(
    items: list[NewsItem],
    model: StubSentimentModel | None,
) -> tuple[NewsAgent, FakeNewsRepository, FakeSignalRepository, FakeEventBus]:
    news_repo = FakeNewsRepository()
    signals = FakeSignalRepository()
    bus = FakeEventBus()
    agent = NewsAgent(
        FakeNewsProvider(items),
        news_repo,
        signals,
        bus,
        sentiment_model=model,
        lookback_hours=24,
        per_symbol_limit=20,
    )
    return agent, news_repo, signals, bus


@pytest.mark.asyncio
async def test_sentiment_model_drives_news_score() -> None:
    model = StubSentimentModel(
        SentimentResult(
            sentiment=0.9,
            confidence=0.9,
            relevance=1.0,
            model="stub",
        )
    )
    agent, news_repo, signals, bus = _agent(
        [_item("AAPL reports record earnings", "https://x.com/1")], model
    )
    score = await agent.analyze_symbol("AAPL")
    assert score == pytest.approx(0.9)
    stored = news_repo.items[0]
    assert stored.sentiment_score == Decimal("0.9")
    assert stored.analysis_confidence == Decimal("0.9")
    assert stored.metadata.get("analysis_schema") == "stub"
    assert len(signals.rows) == 1
    assert signals.rows[0].score == Decimal("0.9")
    assert len(bus.published) == 1
    assert model.calls and model.calls[0][1] == "AAPL"


@pytest.mark.asyncio
async def test_sentiment_model_error_skips_item() -> None:
    model = StubSentimentModel(
        SentimentResult(
            sentiment=0.0,
            confidence=0.0,
            relevance=0.0,
            model="stub",
            error=True,
            error_message="transformers missing",
        )
    )
    agent, news_repo, signals, bus = _agent(
        [_item("anything", "https://x.com/2")], model
    )
    score = await agent.analyze_symbol("AAPL")
    assert score == 0.0
    assert signals.rows == []
    assert bus.published == []


@pytest.mark.asyncio
async def test_sentiment_model_without_llm_still_analyzes() -> None:
    model = StubSentimentModel(
        SentimentResult(
            sentiment=-0.5,
            confidence=0.7,
            relevance=1.0,
            model="stub",
        )
    )
    agent, _, signals, _ = _agent([_item("AAPL misses guidance", "https://x.com/3")], model)
    score = await agent.analyze_symbol("AAPL")
    assert score < 0
    assert len(signals.rows) == 1


@pytest.mark.asyncio
async def test_sentiment_model_ignored_when_none() -> None:
    agent, news_repo, signals, bus = _agent(
        [_item("plain headline", "https://x.com/4")], None
    )
    score = await agent.analyze_symbol("AAPL")
    assert score == 0.0
    assert news_repo.items[0].sentiment_score is None
    assert signals.rows == []
    assert bus.published == []
