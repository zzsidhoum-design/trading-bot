"""News analysis — strict schema validated for LLM output (docs/02-agents.md §4).

The ``LLMClient.complete_json`` contract returns an instance of ``schema``; the
schema used by the News Agent is ``NewsAnalysis``. Out-of-schema / invalid
values are dropped by the agent (never crash the pipeline).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class NewsAnalysis(BaseModel):
    sentiment_score: float = Field(ge=-1.0, le=1.0)
    summary: str = Field(min_length=1, max_length=500)
    expected_market_impact: str = Field(pattern="^(LOW|MEDIUM|HIGH)$")
    impact_direction: int = Field(ge=-1, le=1)
    relevant_symbols: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list, max_length=10)
    confidence: float = Field(ge=0.0, le=1.0)
