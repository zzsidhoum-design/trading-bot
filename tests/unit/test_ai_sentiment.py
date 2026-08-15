"""Phase 6 — FinBERT/lexicon sentiment models and the news sentiment pipeline."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from qtrader.application.ai.models import SentimentResult
from qtrader.application.ai.sentiment import (
    FinBERTModel,
    LexiconFinancialSentimentModel,
    NewsSentimentPipeline,
    score_from_assessment,
)
from qtrader.domain.entities import NewsItem
from tests.unit.fakes_ai import FakeNewsProvider, FakeNewsRepository, StubSentimentModel


def _news(title: str, *, hours_ago: float = 1.0, symbol: str = "AAPL") -> NewsItem:
    return NewsItem(
        symbol=symbol,
        source="test",
        title=title,
        url=f"http://x/{title}",
        published_at=datetime.now(UTC) - timedelta(hours=hours_ago),
        content="",
    )


class TestLexiconModel:
    def test_positive_text_scores_positive(self) -> None:
        model = LexiconFinancialSentimentModel()
        result = model.analyze("company beats revenue estimates", symbol="AAPL")
        assert result.sentiment > 0.0
        assert result.error is False

    def test_negative_text_scores_negative(self) -> None:
        model = LexiconFinancialSentimentModel()
        result = model.analyze("stock plunges after fraud lawsuit", symbol="AAPL")
        assert result.sentiment < 0.0

    def test_neutral_text_scores_zero_with_low_confidence(self) -> None:
        model = LexiconFinancialSentimentModel()
        result = model.analyze("the quick brown fox", symbol="AAPL")
        assert result.sentiment == 0.0
        assert result.confidence < 0.5

    def test_symbol_presence_raises_relevance(self) -> None:
        model = LexiconFinancialSentimentModel()
        with_symbol = model.analyze("AAPL beats guidance", symbol="AAPL")
        without = model.analyze("beats guidance", symbol="AAPL")
        assert with_symbol.relevance > without.relevance

    def test_sentiment_bounded(self) -> None:
        model = LexiconFinancialSentimentModel()
        for text in ("buy buy buy", "sell sell sell", "mixed news"):
            result = model.analyze(text)
            assert -1.0 <= result.sentiment <= 1.0


class TestFinBERTModel:
    def test_fails_safe_when_transformers_missing(self) -> None:
        model = FinBERTModel(model_name="nope/does-not-exist")
        result = model.analyze("some text", symbol="AAPL")
        assert result.error is True
        assert result.error_message is not None
        assert result.sentiment == 0.0


class TestNewsSentimentPipeline:
    @pytest.mark.asyncio
    async def test_assess_returns_none_without_news(self) -> None:
        provider = FakeNewsProvider()
        repo = FakeNewsRepository()
        pipeline = NewsSentimentPipeline(
            provider, repo, StubSentimentModel()
        )
        assert await pipeline.assess("AAPL") is None

    @pytest.mark.asyncio
    async def test_assess_aggregates_weighted_sentiment(self) -> None:
        provider = FakeNewsProvider(
            [
                _news("company beats expectations", hours_ago=1.0),
                _news("misses targets badly", hours_ago=10.0),
            ]
        )
        pipeline = NewsSentimentPipeline(provider, FakeNewsRepository(), StubSentimentModel())
        result = await pipeline.assess("AAPL")
        assert result is not None
        assert result.asset == "AAPL"
        assert result.items_used == 2
        assert result.sources == ("test",)

    @pytest.mark.asyncio
    async def test_assess_skips_future_news(self) -> None:
        future = NewsItem(
            symbol="AAPL",
            source="test",
            title="future leak",
            url="http://x/f",
            published_at=datetime.now(UTC) + timedelta(days=2),
        )
        provider = FakeNewsProvider([future])
        pipeline = NewsSentimentPipeline(provider, FakeNewsRepository(), StubSentimentModel())
        as_of = datetime.now(UTC)
        assert await pipeline.assess("AAPL", as_of=as_of) is None

    @pytest.mark.asyncio
    async def test_assess_skips_unrelated_news(self) -> None:
        provider = FakeNewsProvider([_news("apple pie recipe", symbol="PIE")])
        pipeline = NewsSentimentPipeline(
            provider, FakeNewsRepository(), StubSentimentModel(), min_relevance=0.6
        )
        result = await pipeline.assess("AAPL")
        assert result is None

    @pytest.mark.asyncio
    async def test_provider_failure_returns_none(self) -> None:
        class Boom:
            async def fetch_news(self, symbol, since, limit):
                raise RuntimeError("provider down")

        pipeline = NewsSentimentPipeline(
            Boom(), FakeNewsRepository(), StubSentimentModel()  # type: ignore[arg-type]
        )
        assert await pipeline.assess("AAPL") is None

    @pytest.mark.asyncio
    async def test_model_error_is_skipped(self) -> None:
        provider = FakeNewsProvider([_news("headline one")])
        error_model = StubSentimentModel(
            SentimentResult(
                sentiment=0.0,
                confidence=0.0,
                relevance=0.0,
                model="stub",
                error=True,
                error_message="boom",
            )
        )
        pipeline = NewsSentimentPipeline(provider, FakeNewsRepository(), error_model)
        assert await pipeline.assess("AAPL") is None


def test_score_from_assessment_maps_aggregated_sentiment() -> None:
    from qtrader.application.ai.models import NewsAssessment

    assessment = NewsAssessment(
        asset="AAPL",
        timestamp=datetime.now(UTC),
        sentiment=0.4,
        confidence=0.8,
        sources=("a",),
        relevance=0.9,
        aggregated_sentiment=0.4,
        items_used=3,
        model="lexicon-financial",
    )
    assert score_from_assessment(assessment) == 0.4
    assert score_from_assessment(None) == 0.0
