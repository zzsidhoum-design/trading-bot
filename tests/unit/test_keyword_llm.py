"""Unit tests for the Keyword LLM fallback (schema-validated JSON)."""

from __future__ import annotations

import pytest

from qtrader.application.services.news_analysis import NewsAnalysis
from qtrader.infrastructure.llm.adapters import KeywordLLMClient


@pytest.mark.asyncio
async def test_keyword_llm_positive() -> None:
    client = KeywordLLMClient()
    result = await client.complete_json(
        "system", "AAPL beats earnings, profit surges", NewsAnalysis
    )
    assert isinstance(result, NewsAnalysis)
    assert result.sentiment_score > 0
    assert result.expected_market_impact in {"LOW", "MEDIUM", "HIGH"}
    assert result.impact_direction == 1
    assert 0.0 <= result.confidence <= 1.0


@pytest.mark.asyncio
async def test_keyword_llm_negative() -> None:
    client = KeywordLLMClient()
    result = await client.complete_json("system", "MSFT misses guidance, stock falls", NewsAnalysis)
    assert result.sentiment_score < 0
    assert result.impact_direction == -1
