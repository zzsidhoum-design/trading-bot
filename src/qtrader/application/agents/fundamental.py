"""Fundamental Analysis Agent — valuation & financial-health score (docs/02-agents.md §5).

Pulls fundamentals per symbol, persists them, computes a normalized composite
score, persists a ``Signal`` row and publishes ``FundamentalSignalGenerated``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import ClassVar

import structlog

from qtrader.application.agents.base import AgentBase, AgentContext
from qtrader.application.services.fundamental_score import score_fundamentals
from qtrader.domain.entities import FundamentalData, Signal
from qtrader.domain.events import DomainEvent, FundamentalSignalGenerated, ScanCompleted
from qtrader.domain.ports import (
    EventBus,
    FundamentalProvider,
    FundamentalRepository,
    SignalRepository,
)

logger = structlog.get_logger(__name__)

SCORE_QUANT = Decimal("0.0001")


def _dec_score(value: float) -> Decimal:
    return Decimal(str(round(value, 4))).quantize(SCORE_QUANT, rounding=ROUND_HALF_UP)


class FundamentalAgent(AgentBase):
    name: ClassVar[str] = "fundamental"
    consumes: ClassVar[tuple[type[DomainEvent], ...]] = (ScanCompleted,)
    produces: ClassVar[tuple[type[DomainEvent], ...]] = (FundamentalSignalGenerated,)

    def __init__(
        self,
        provider: FundamentalProvider,
        fundamentals: FundamentalRepository,
        signals: SignalRepository,
        bus: EventBus,
        *,
        max_age_days: int = 120,
    ) -> None:
        self._provider = provider
        self._fundamentals = fundamentals
        self._signals = signals
        self._bus = bus
        self._max_age_days = max_age_days

    async def analyze_symbol(self, symbol: str) -> FundamentalData | None:
        stored = await self._fundamentals.latest(symbol)
        data: FundamentalData | None
        if stored and stored.report_date:
            age_days = (datetime.now(UTC).date() - stored.report_date).days
            if age_days <= self._max_age_days:
                data = stored
            else:
                data = await self._provider.fetch_fundamentals(symbol)
        else:
            data = await self._provider.fetch_fundamentals(symbol)
        if data is None:
            return None
        await self._fundamentals.upsert(data)

        score, signal_type, sub_scores = score_fundamentals(data)
        signal = Signal(
            symbol=symbol,
            agent=self.name,
            signal_type=signal_type,
            score=_dec_score(score),
            horizon="fundamental",
            metadata={"period": data.period, "sub_scores": sub_scores},
        )
        await self._signals.save(signal)
        logger.info("fundamental.signal", symbol=symbol, signal_type=signal_type, score=score)
        await self._bus.publish(
            FundamentalSignalGenerated(
                symbol=symbol,
                agent=self.name,
                signal_type=signal_type,
                score=score,
                rating=signal_type.value,
                as_of=(
                    data.report_date.isoformat()
                    if data.report_date
                    else datetime.now(UTC).date().isoformat()
                ),
                sub_scores=sub_scores,
            )
        )
        return data

    async def analyze_candidates(self, symbols: list[str]) -> int:
        scored = 0
        for symbol in symbols:
            try:
                if await self.analyze_symbol(symbol) is not None:
                    scored += 1
            except Exception:
                logger.exception("fundamental.analyze_failed", symbol=symbol)
        return scored

    async def on_event(self, event: DomainEvent) -> None:
        if isinstance(event, ScanCompleted):
            await self.analyze_candidates([c["symbol"] for c in event.candidates])

    async def run(self, ctx: AgentContext) -> None:
        await self.analyze_symbol(ctx.symbol)
