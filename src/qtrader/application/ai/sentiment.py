"""Financial-text sentiment models + point-in-time news sentiment pipeline.

FinBERT (HuggingFace ``ProsusAI/finbert``) is used strictly as a
*sentiment/feature model* — never as an independent trading decision-maker.
The pipeline is: news -> relevance/entity matching -> sentiment model ->
sentiment + confidence. The real transformer model is loaded lazily and only
when ``transformers`` is installed; the deterministic lexicon model is the
offline fallback. Both must fail safely (an error yields a flagged
``SentimentResult``, never fabricated data). All aggregation is point-in-time:
only items published at or before the decision timestamp are ever used.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from qtrader.application.ai.models import NewsAssessment, SentimentResult
from qtrader.domain.entities import NewsItem
from qtrader.domain.ports import NewsProvider, NewsRepository

# Financial-text lexicon (deterministic fallback calibrated to a -1..1 score).
_POSITIVE = {
    "beat", "beats", "upgrade", "upgraded", "buy", "bullish", "gain", "gains",
    "profit", "profits", "revenue", "growth", "grew", "record", "surge",
    "surges", "rally", "rallies", "outperform", "strong", "raised", "upward",
    "positive", "expansion", "milestone", "win", "wins", "exceeded",
    "outperformed", "accelerating", "rebound", "recovered", "stronger",
    "surpassed", "dividend", "dividends", "buyback", "momentum",
}
_NEGATIVE = {
    "miss", "misses", "downgrade", "downgraded", "sell", "bearish", "loss",
    "losses", "decline", "declines", "fell", "fall", "drop", "drops",
    "weak", "weakened", "cut", "reduced", "downward",
    "negative", "warning", "warned", "lawsuit", "fraud", "investigation",
    "restructuring", "bankruptcy", "default", "layoffs", "underperform",
    "debt", "litigation", "recall", "delayed", "plunge", "plunges", "sells",
    "withdraws", "guidance", "missed", "shortfall", "slowdown",
}


def _tokenize(text: str) -> set[str]:
    return {t.lower().strip(".,!?;:()[]{}\"'") for t in text.replace("-", " ").split()}


class FinancialSentimentModel(Protocol):
    """A financial-text sentiment/feature model (FinBERT or fallback)."""

    def analyze(
        self,
        text: str,
        *,
        symbol: str | None = None,
    ) -> SentimentResult: ...


class LexiconFinancialSentimentModel:
    """Deterministic, dependency-free financial sentiment fallback.

    Score = (pos - neg) / (pos + neg), confidence grows with coverage.
    Relevance rises when the symbol/entity appears in the text.
    """

    def __init__(
        self,
        *,
        positive: set[str] | None = None,
        negative: set[str] | None = None,
    ) -> None:
        self._positive = positive if positive is not None else set(_POSITIVE)
        self._negative = negative if negative is not None else set(_NEGATIVE)

    def analyze(
        self,
        text: str,
        *,
        symbol: str | None = None,
    ) -> SentimentResult:
        tokens = _tokenize(text or "")
        pos = len(tokens & self._positive)
        neg = len(tokens & self._negative)
        total = pos + neg
        if total == 0:
            sentiment = 0.0
            confidence = 0.2
        else:
            sentiment = (pos - neg) / total
            confidence = min(0.9, 0.45 + 0.1 * total)
        relevance = 0.9 if symbol and symbol.lower() in tokens else 0.5
        return SentimentResult(
            sentiment=round(sentiment, 4),
            confidence=round(confidence, 4),
            relevance=round(relevance, 4),
            model="lexicon-financial",
            summary=None,
        )


class FinBERTModel:
    """FinBERT (``ProsusAI/finbert``) sentiment as a feature model.

    The transformer stack is imported lazily; when ``transformers`` is not
    installed (or the model cannot be loaded) every call returns a flagged
    error ``SentimentResult`` instead of crashing or fabricating a score.
    """

    def __init__(
        self,
        *,
        model_name: str = "ProsusAI/finbert",
        max_length: int = 256,
    ) -> None:
        self._model_name = model_name
        self._max_length = max_length
        self._tokenizer: Any = None
        self._model: Any = None
        self._load_error: str | None = None

    def _ensure_loaded(self) -> bool:
        if self._model is not None:
            return True
        if self._load_error is not None:
            return False
        try:
            from transformers import (  # type: ignore
                AutoModelForSequenceClassification,
                AutoTokenizer,
            )

            self._tokenizer = AutoTokenizer.from_pretrained(self._model_name)
            self._model = AutoModelForSequenceClassification.from_pretrained(
                self._model_name
            )
            self._model.eval()
            return True
        except Exception as exc:  # noqa: BLE001 - any loader failure must fail safe
            self._load_error = str(exc)
            return False

    def analyze(
        self,
        text: str,
        *,
        symbol: str | None = None,
    ) -> SentimentResult:
        if not self._ensure_loaded():
            return SentimentResult(
                sentiment=0.0,
                confidence=0.0,
                relevance=0.0,
                model="finbert",
                error=True,
                error_message=f"finbert unavailable: {self._load_error or 'transformers missing'}",
            )
        try:
            import torch  # type: ignore

            tokenizer = self._tokenizer
            model = self._model
            assert tokenizer is not None and model is not None
            inputs = tokenizer(
                (text or "")[: self._max_length * 4],
                return_tensors="pt",
                truncation=True,
                max_length=self._max_length,
            )
            with torch.no_grad():
                logits = model(**inputs).logits
            probs = torch.softmax(logits, dim=-1)[0].tolist()
            # FinBERT label order: 0 positive, 1 negative, 2 neutral.
            sentiment = float(probs[0] - probs[1])
            confidence = float(max(probs[0], probs[1], probs[2]))
        except Exception as exc:  # noqa: BLE001 - any runtime failure fails safe
            return SentimentResult(
                sentiment=0.0,
                confidence=0.0,
                relevance=0.0,
                model="finbert",
                error=True,
                error_message=f"finbert inference failed: {exc}",
            )
        relevance = 0.9 if symbol and symbol.lower() in _tokenize(text or "") else 0.5
        return SentimentResult(
            sentiment=round(sentiment, 4),
            confidence=round(confidence, 4),
            relevance=relevance,
            model="finbert",
        )


def _relevance_ok(item: NewsItem, symbol: str, min_relevance: float) -> bool:
    """Relevance / entity matching: symbol must appear, or be the target asset."""
    haystack = _tokenize(f"{item.title} {item.content or ''}")
    explicit = symbol and symbol.lower() in haystack
    if explicit:
        return True
    if item.symbol and item.symbol.upper() == symbol.upper():
        return True
    return min_relevance <= 0.0


class NewsSentimentPipeline:
    """Point-in-time news -> relevance -> sentiment -> aggregated assessment."""

    def __init__(
        self,
        provider: NewsProvider,
        news_repo: NewsRepository,
        model: FinancialSentimentModel,
        *,
        lookback_hours: int = 24,
        per_symbol_limit: int = 20,
        min_relevance: float = 0.0,
    ) -> None:
        self._provider = provider
        self._news_repo = news_repo
        self._model = model
        self._lookback_hours = lookback_hours
        self._limit = per_symbol_limit
        self._min_relevance = min_relevance

    async def assess(
        self,
        symbol: str,
        *,
        as_of: datetime | None = None,
    ) -> NewsAssessment | None:
        """Assess sentiment using only items published at or before ``as_of``."""
        as_of = as_of or datetime.now(UTC)
        since = as_of - timedelta(hours=self._lookback_hours)
        try:
            raw = await self._provider.fetch_news(symbol, since, self._limit)
        except RuntimeError:
            return None
        if not raw:
            return None

        relevant = [
            item
            for item in raw
            if item.published_at <= as_of and _relevance_ok(item, symbol, self._min_relevance)
        ]
        if not relevant:
            return None

        assessed: list[tuple[float, float, str, float]] = []
        for item in relevant:
            text = f"{item.title}\n{item.content or ''}"
            result = self._model.analyze(text, symbol=symbol)
            if result.error:
                continue
            assessed.append(
                (
                    result.sentiment,
                    result.confidence,
                    item.source or "unknown",
                    result.relevance,
                )
            )
        if not assessed:
            return None

        weighted = 0.0
        weight_sum = 0.0
        conf_sum = 0.0
        sources: set[str] = set()
        max_relevance = 0.0
        for sentiment, confidence, source, relevance in assessed:
            oldest = min(item.published_at for item in relevant)
            age_hours = max((as_of - oldest).total_seconds() / 3600.0, 0.0)
            recency = 1.0 / (1.0 + age_hours)
            weight = recency * confidence
            weighted += sentiment * weight
            weight_sum += weight
            conf_sum += confidence
            sources.add(source)
            max_relevance = max(max_relevance, relevance)
        aggregated = weighted / weight_sum if weight_sum else 0.0

        return NewsAssessment(
            asset=symbol,
            timestamp=as_of,
            sentiment=round(aggregated, 4),
            confidence=round(conf_sum / len(assessed), 4),
            sources=tuple(sorted(sources)),
            relevance=round(max_relevance, 4),
            aggregated_sentiment=round(aggregated, 4),
            items_used=len(assessed),
            model=self._model.__class__.__name__.lower(),
        )


def score_from_assessment(assessment: NewsAssessment | None) -> float:
    """Map an aggregated news assessment to a signed agent score in [-1, 1]."""
    if assessment is None:
        return 0.0
    return assessment.aggregated_sentiment


__all__ = [
    "FinancialSentimentModel",
    "FinBERTModel",
    "LexiconFinancialSentimentModel",
    "NewsSentimentPipeline",
    "score_from_assessment",
]
