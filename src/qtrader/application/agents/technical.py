"""Technical Analysis Agent — indicators + preliminary signals (docs/02-agents.md §3).

Consumes scan candidates (cheap filter already applied), computes the full
indicator stack over stored bars, persists the latest ``IndicatorSnapshot``
and a composite ``Signal`` row, then publishes ``TechnicalSignalGenerated``.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import ClassVar

from qtrader.application.agents.base import AgentBase, AgentContext
from qtrader.application.services.indicators import IndicatorEngine, score_technical
from qtrader.domain.entities import IndicatorSnapshot, Signal
from qtrader.domain.events import DomainEvent, ScanCompleted, TechnicalSignalGenerated
from qtrader.domain.ports import EventBus, IndicatorRepository, PriceRepository, SignalRepository
from qtrader.domain.value_objects import Interval

SCORE_QUANT = Decimal("0.0001")


def _dec_score(value: float) -> Decimal:
    return Decimal(str(value)).quantize(SCORE_QUANT, rounding=ROUND_HALF_UP)


class TechnicalAgent(AgentBase):
    name: ClassVar[str] = "technical"
    consumes: ClassVar[tuple[type[DomainEvent], ...]] = (ScanCompleted,)
    produces: ClassVar[tuple[type[DomainEvent], ...]] = (TechnicalSignalGenerated,)

    def __init__(
        self,
        prices: PriceRepository,
        indicators: IndicatorRepository,
        signals: SignalRepository,
        bus: EventBus,
        engine: IndicatorEngine | None = None,
        *,
        interval: Interval = Interval.M5,
        history_limit: int = 260,
        min_bars: int = 60,
    ) -> None:
        self._prices = prices
        self._indicators = indicators
        self._signals = signals
        self._bus = bus
        self._engine = engine or IndicatorEngine()
        self._interval = interval
        self._history_limit = history_limit
        self._min_bars = min_bars

    async def analyze_symbol(
        self, symbol: str, interval: Interval | None = None
    ) -> IndicatorSnapshot | None:
        interval = interval or self._interval
        bars = await self._prices.history(symbol, interval, limit=self._history_limit)
        if len(bars) < self._min_bars:
            self._logger.warning(
                "technical.insufficient_bars", symbol=symbol, interval=interval, bars=len(bars)
            )
            return None

        snapshot = self._engine.compute(bars, symbol, interval)
        await self._indicators.save_snapshot(snapshot)

        score, signal_type, sub_scores = score_technical(snapshot)
        signal = Signal(
            symbol=symbol,
            agent=self.name,
            signal_type=signal_type,
            score=_dec_score(score),
            interval=interval,
            horizon="intraday",
            metadata={"sub_scores": sub_scores},
        )
        await self._signals.save(signal)
        self._logger.info(
            "technical.signal",
            symbol=symbol,
            interval=interval,
            signal_type=signal_type,
            score=score,
        )
        await self._bus.publish(
            TechnicalSignalGenerated(
                symbol=symbol,
                agent=self.name,
                signal_type=signal_type,
                score=score,
                interval=interval,
                sub_scores=sub_scores,
            )
        )
        return snapshot

    async def analyze_candidates(self, symbols: list[str]) -> int:
        return await self.run_batch(symbols, self.analyze_symbol, action="technical.analyze_failed")

    async def on_event(self, event: DomainEvent) -> None:
        if isinstance(event, ScanCompleted):
            symbols = [c["symbol"] for c in event.candidates]
            await self.analyze_candidates(symbols)

    async def run(self, ctx: AgentContext) -> None:
        await self.analyze_symbol(ctx.symbol, ctx.interval)
