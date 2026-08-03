"""News Agent — turn unstructured text into scored signals (docs/02-agents.md §4).

Fetches candidate headlines for each scan candidate, persists the raw rows,
optionally runs each item through an ``LLMClient`` (schema-validated JSON),
aggregates a per-symbol recency/confidence-weighted sentiment score, persists a
``Signal`` row and publishes ``NewsSignalGenerated``. LLM failures are logged
and skipped — the pipeline never crashes on a bad article.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import ClassVar

from qtrader.application.agents.base import AgentBase, AgentContext
from qtrader.application.services.news_analysis import NewsAnalysis
from qtrader.domain.entities import NewsItem, Signal
from qtrader.domain.events import DomainEvent, NewsSignalGenerated, ScanCompleted
from qtrader.domain.ports import EventBus, LLMClient, NewsProvider, NewsRepository, SignalRepository
from qtrader.domain.value_objects import MarketImpact, SignalType

SCORE_QUANT = Decimal("0.0001")


def _dec_score(value: float) -> Decimal:
    return Decimal(str(round(value, 4))).quantize(SCORE_QUANT, rounding=ROUND_HALF_UP)


def _signal_type(score: float) -> SignalType:
    if score >= 0.5:
        return SignalType.BUY
    if score <= -0.5:
        return SignalType.SELL
    if abs(score) < 0.15:
        return SignalType.NEUTRAL
    return SignalType.HOLD


def _aggregate(items: list[NewsItem], now: datetime | None = None) -> float:
    """Recency- and confidence-weighted average sentiment in [-1, 1]."""
    now = now or datetime.now(UTC)
    weighted = 0.0
    weight_sum = 0.0
    for item in items:
        if item.sentiment_score is None:
            continue
        age_hours = max((now - item.published_at).total_seconds() / 3600.0, 0.0)
        recency = 1.0 / (1.0 + age_hours)
        confidence = float(item.analysis_confidence) if item.analysis_confidence else 0.5
        weight = recency * confidence
        weighted += float(item.sentiment_score) * weight
        weight_sum += weight
    return weighted / weight_sum if weight_sum else 0.0


class NewsAgent(AgentBase):
    name: ClassVar[str] = "news"
    consumes: ClassVar[tuple[type[DomainEvent], ...]] = (ScanCompleted,)
    produces: ClassVar[tuple[type[DomainEvent], ...]] = (NewsSignalGenerated,)

    def __init__(
        self,
        provider: NewsProvider,
        news_repo: NewsRepository,
        signals: SignalRepository,
        bus: EventBus,
        llm: LLMClient | None = None,
        *,
        lookback_hours: int = 24,
        per_symbol_limit: int = 20,
    ) -> None:
        self._provider = provider
        self._news_repo = news_repo
        self._signals = signals
        self._bus = bus
        self._llm = llm
        self._lookback_hours = lookback_hours
        self._limit = per_symbol_limit

    async def analyze_symbol(self, symbol: str) -> float:
        since = datetime.now(UTC) - timedelta(hours=self._lookback_hours)
        try:
            raw = await self._provider.fetch_news(symbol, since, self._limit)
        except RuntimeError as exc:
            self._logger.warning("news.fetch_failed", symbol=symbol, error=str(exc))
            return 0.0
        if not raw:
            return 0.0
        await self._news_repo.upsert(raw)

        analyzed: list[NewsItem] = []
        for item in raw:
            if self._llm is not None:
                result = await self._analyze(item, symbol)
                if result is not None:
                    analyzed.append(result)
            else:
                analyzed.append(item)

        scored = [i for i in analyzed if i.sentiment_score is not None]
        if not scored:
            return 0.0
        await self._news_repo.upsert(scored)
        score = _aggregate(scored)
        signal_type = _signal_type(score)
        signal = Signal(
            symbol=symbol,
            agent=self.name,
            signal_type=signal_type,
            score=_dec_score(score),
            horizon="news_window",
            metadata={
                "items": len(scored),
                "sources": list({i.source for i in scored if i.source}),
                "lookback_hours": self._lookback_hours,
            },
        )
        await self._signals.save(signal)
        self._logger.info("news.signal", symbol=symbol, signal_type=signal_type, score=score)
        await self._bus.publish(
            NewsSignalGenerated(
                symbol=symbol,
                agent=self.name,
                signal_type=signal_type,
                score=score,
                impact=(
                    MarketImpact.HIGH
                    if abs(score) >= 0.6
                    else (MarketImpact.MEDIUM if abs(score) >= 0.3 else MarketImpact.LOW)
                ),
                sources=list({i.source for i in scored if i.source}),
            )
        )
        return score

    async def _analyze(self, item: NewsItem, symbol: str) -> NewsItem | None:
        llm = self._llm
        if llm is None:
            return None
        system_prompt = (
            "You analyze stock market news. Reply with strict JSON matching the schema "
            "NewsAnalysis: sentiment_score ([-1,1]), summary, expected_market_impact "
            "(LOW|MEDIUM|HIGH), impact_direction (-1|0|1), relevant_symbols, categories, "
            "confidence ([0,1])."
        )
        user_prompt = f"Symbol: {symbol}\nTitle: {item.title}\nBody: {(item.content or '')[:1000]}"
        try:
            result: NewsAnalysis = await llm.complete_json(
                system_prompt, user_prompt, NewsAnalysis
            )
        except Exception as exc:
            self._logger.warning("news.llm_failed", url=item.url, error=str(exc))
            return None
        from dataclasses import replace

        return replace(
            item,
            sentiment_score=_dec_score(result.sentiment_score),
            summary=result.summary,
            expected_market_impact=result.expected_market_impact,
            impact_direction=result.impact_direction,
            analysis_confidence=_dec_score(result.confidence),
            categories=result.categories or None,
            analyzed_at=datetime.now(UTC),
            metadata={**item.metadata, "analysis_schema": "NewsAnalysis"},
        )

    async def analyze_candidates(self, symbols: list[str]) -> int:
        scored = 0
        for symbol in symbols:
            try:
                if await self.analyze_symbol(symbol) != 0.0:
                    scored += 1
            except Exception:
                self._logger.exception("news.analyze_failed", symbol=symbol)
        return scored

    async def on_event(self, event: DomainEvent) -> None:
        if isinstance(event, ScanCompleted):
            await self.analyze_candidates([c["symbol"] for c in event.candidates])

    async def run(self, ctx: AgentContext) -> None:
        await self.analyze_symbol(ctx.symbol)
