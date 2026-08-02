"""Market Scanner Agent — find the most tradeable candidates (docs/02-agents.md §2).

Scans stored prices for every active symbol, computes a cheap liquidity /
volatility / momentum profile, filters out illiquid or dead names, and keeps
a Redis sorted set of top-K rankings. Only the cheap filter runs here — heavy
analysis happens downstream for the top-K only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import mean, pstdev
from typing import ClassVar

import structlog

from qtrader.application.agents.base import AgentBase, AgentContext
from qtrader.domain.events import BackfillCompleted, DomainEvent, ScanCompleted
from qtrader.domain.ports import Cache, EventBus, PriceRepository, StockRepository
from qtrader.domain.value_objects import Interval, PriceBar

logger = structlog.get_logger(__name__)

SCAN_ZSET_PREFIX = "scan:top"


@dataclass(frozen=True, slots=True)
class ScanCandidate:
    symbol: str
    score: float
    dollar_volume: float
    atr_pct: float
    momentum_pct: float
    range_pct: float


def _znormalize(values: list[float]) -> list[float]:
    n = len(values)
    if n == 0:
        return []
    sigma = pstdev(values)
    if sigma == 0:
        return [0.0] * n
    mu = mean(values)
    return [(v - mu) / sigma for v in values]


class MarketScanner(AgentBase):
    name: ClassVar[str] = "scanner"
    consumes: ClassVar[tuple[type[DomainEvent], ...]] = (BackfillCompleted,)
    produces: ClassVar[tuple[type[DomainEvent], ...]] = (ScanCompleted,)

    def __init__(
        self,
        prices: PriceRepository,
        cache: Cache,
        stocks: StockRepository,
        bus: EventBus,
        *,
        interval: Interval = Interval.M5,
        top_k: int = 20,
        lookback_bars: int = 60,
        momentum_lookback: int = 20,
        min_dollar_volume: float = 500_000.0,
        min_atr_pct: float = 0.3,
    ) -> None:
        self._prices = prices
        self._cache = cache
        self._stocks = stocks
        self._bus = bus
        self._interval = interval
        self._top_k = top_k
        self._lookback = lookback_bars
        self._momentum_lookback = momentum_lookback
        self._min_dollar_volume = min_dollar_volume
        self._min_atr_pct = min_atr_pct

    async def scan_all(self) -> list[ScanCandidate]:
        """Score every active symbol, persist rankings, publish top-K."""
        stocks = await self._stocks.list_active()
        candidates: list[ScanCandidate] = []
        for stock in stocks:
            bars = await self._prices.history(
                stock.symbol, self._interval, limit=self._lookback
            )
            if len(bars) < self._momentum_lookback + 1:
                continue
            cand = self._metrics(bars)
            if cand.dollar_volume < self._min_dollar_volume:
                continue
            if cand.atr_pct < self._min_atr_pct:
                continue
            candidates.append(cand)

        ranked = self._rank(candidates)
        await self._persist_rankings(ranked)

        top = ranked[: self._top_k]
        logger.info(
            "scanner.scan_completed",
            scanned=len(candidates),
            candidates=len(top),
        )
        await self._bus.publish(
            ScanCompleted(candidates=[asdict(c) for c in top])
        )
        return top

    def _metrics(self, bars: list[PriceBar]) -> ScanCandidate:
        closes = [b.close for b in bars]
        last = closes[-1]
        dollar_volume = sum(c * v for c, v in zip(closes, [b.volume for b in bars], strict=True))

        # ATR% (percent) — mean true range over the window.
        trues: list[float] = []
        for i, bar in enumerate(bars):
            if i == 0:
                trues.append(float(bar.high - bar.low))
            else:
                prev_close = float(bars[i - 1].close)
                trues.append(
                    max(
                        float(bar.high - bar.low),
                        abs(float(bar.high) - prev_close),
                        abs(float(bar.low) - prev_close),
                    )
                )
        atr_pct = (mean(trues) / float(last)) * 100 if last else 0.0

        base = closes[-self._momentum_lookback - 1]
        momentum_pct = (float(last) - float(base)) / float(base) * 100 if base else 0.0

        highs = [float(b.high) for b in bars]
        lows = [float(b.low) for b in bars]
        min_low = min(lows)
        range_pct = (max(highs) - min_low) / min_low * 100 if min_low else 0.0

        return ScanCandidate(
            symbol=bars[-1].symbol,
            score=0.0,
            dollar_volume=float(dollar_volume),
            atr_pct=atr_pct,
            momentum_pct=momentum_pct,
            range_pct=range_pct,
        )

    @staticmethod
    def _rank(candidates: list[ScanCandidate]) -> list[ScanCandidate]:
        """Z-score composite: 40% momentum, 30% volatility, 30% liquidity."""
        if not candidates:
            return []
        z_mom = _znormalize([c.momentum_pct for c in candidates])
        z_atr = _znormalize([c.atr_pct for c in candidates])
        z_liq = _znormalize([c.dollar_volume for c in candidates])
        scored: list[ScanCandidate] = []
        for i, c in enumerate(candidates):
            score = 0.4 * z_mom[i] + 0.3 * z_atr[i] + 0.3 * z_liq[i]
            scored.append(
                ScanCandidate(
                    symbol=c.symbol,
                    score=score,
                    dollar_volume=c.dollar_volume,
                    atr_pct=c.atr_pct,
                    momentum_pct=c.momentum_pct,
                    range_pct=c.range_pct,
                )
            )
        scored.sort(key=lambda c: c.score, reverse=True)
        return scored

    async def _persist_rankings(self, candidates: list[ScanCandidate]) -> None:
        for metric, key in (
            ("dollar_volume", "liquidity"),
            ("atr_pct", "volatility"),
            ("momentum_pct", "momentum"),
        ):
            mapping = {c.symbol: getattr(c, metric) for c in candidates}
            if mapping:
                await self._cache.zadd(f"{SCAN_ZSET_PREFIX}:{key}", mapping)
        overall = {c.symbol: c.score for c in candidates}
        if overall:
            await self._cache.zadd(f"{SCAN_ZSET_PREFIX}:overall", overall)

    async def on_event(self, event: DomainEvent) -> None:
        if isinstance(event, BackfillCompleted):
            await self.scan_all()

    async def run(self, ctx: AgentContext) -> None:
        await self.scan_all()
