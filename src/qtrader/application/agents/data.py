"""Data Agent — reliable, clean market data in the DB (docs/02-agents.md §1).

Ingests bars from a MarketDataProvider, cleans them, persists via the
PriceRepository and publishes ``PriceUpdated`` / ``BackfillCompleted``. The
latest quote is written through to the cache so downstream agents never wait
on the DB for a "current price".
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, ClassVar

from qtrader.application.agents.base import AgentBase, AgentContext
from qtrader.application.services.bar_cleaner import BarCleaner
from qtrader.application.services.bar_validator import BarValidator
from qtrader.domain.events import BackfillCompleted, DomainEvent, PriceUpdated
from qtrader.domain.ports import Cache, EventBus, MarketDataProvider, PriceRepository
from qtrader.domain.value_objects import Interval, PriceBar


class DataAgent(AgentBase):
    name: ClassVar[str] = "data"
    consumes: ClassVar[tuple[type[DomainEvent], ...]] = ()
    produces: ClassVar[tuple[type[DomainEvent], ...]] = (PriceUpdated, BackfillCompleted)

    def __init__(
        self,
        provider: MarketDataProvider,
        prices: PriceRepository,
        cache: Cache,
        bus: EventBus,
        cleaner: BarCleaner,
        *,
        validator: BarValidator | None = None,
        quote_cache_ttl_seconds: int = 300,
    ) -> None:
        self._provider = provider
        self._prices = prices
        self._cache = cache
        self._bus = bus
        self._cleaner = cleaner
        self._validator = validator
        self._quote_ttl = quote_cache_ttl_seconds

    async def backfill(
        self, symbol: str, interval: Interval, start: datetime, end: datetime
    ) -> int:
        """Fetch + clean + validate + persist a historical range; publish completion."""
        end = end.astimezone(UTC)
        start = start.astimezone(UTC)
        try:
            raw = await self._provider.fetch_bars(symbol, interval, start, end)
        except RuntimeError as exc:
            self._logger.error(
                "data.backfill.provider_failed",
                symbol=symbol,
                interval=interval,
                reason=str(exc),
            )
            return 0
        if not raw:
            self._logger.warning(
                "data.backfill.empty_window", symbol=symbol, interval=interval
            )
            return 0
        report = self._cleaner.clean(raw, now=end, reject_stale=False)
        kept = report.kept
        rejected = 0
        reasons: dict[str, int] = dict(report.reasons)
        gaps: list[tuple[str, str, str, int]] = []
        if self._validator is not None:
            validation = self._validator.validate(kept)
            kept = validation.kept
            rejected = validation.rejected
            for reason, count in validation.reasons.items():
                reasons[reason] = reasons.get(reason, 0) + count
            gaps = validation.gaps
        inserted = await self._prices.upsert_bars(kept)
        self._logger.info(
            "data.backfill",
            symbol=symbol,
            interval=interval,
            fetched=len(raw),
            kept=len(report.kept),
            validated=len(kept),
            inserted=inserted,
            dropped=report.dropped,
            rejected=rejected,
            reasons=reasons,
            gaps=gaps,
        )
        if kept:
            await self._cache_quote(kept[-1])
            await self._bus.publish(
                BackfillCompleted(
                    symbol=symbol,
                    interval=interval,
                    start=start.isoformat(),
                    end=end.isoformat(),
                )
            )
        return inserted

    async def refresh(self, symbol: str) -> PriceBar | None:
        """Fetch the latest quote, validate, persist and publish ``PriceUpdated``."""
        try:
            quote = await self._provider.fetch_quote(symbol)
        except RuntimeError as exc:
            self._logger.warning("data.refresh.no_quote", symbol=symbol, error=str(exc))
            return None
        if quote is None:
            self._logger.warning("data.refresh.empty_quote", symbol=symbol)
            return None
        report = self._cleaner.clean([quote], reject_stale=True)
        bar = report.kept[0] if report.kept else None
        if bar is not None and self._validator is not None:
            validation = self._validator.validate([bar])
            bar = validation.kept[0] if validation.kept else None
        if bar is None:
            self._logger.warning(
                "data.refresh.dropped",
                symbol=symbol,
                ts=quote.ts.isoformat(),
                reasons=report.reasons,
            )
            return None
        inserted = await self._prices.upsert_bars([bar])
        await self._cache_quote(bar)
        if inserted:
            await self._bus.publish(
                PriceUpdated(
                    symbol=bar.symbol,
                    interval=bar.interval,
                    ts=bar.ts.isoformat(),
                    open=str(bar.open),
                    high=str(bar.high),
                    low=str(bar.low),
                    close=str(bar.close),
                    volume=str(bar.volume),
                )
            )
        return bar

    async def _cache_quote(self, bar: PriceBar) -> None:
        payload: dict[str, Any] = {
            "symbol": bar.symbol,
            "interval": bar.interval,
            "ts": bar.ts.isoformat(),
            "open": str(bar.open),
            "high": str(bar.high),
            "low": str(bar.low),
            "close": str(bar.close),
            "volume": str(bar.volume),
        }
        await self._cache.set(
            f"quote:{bar.symbol}", json.dumps(payload), ttl_seconds=self._quote_ttl
        )

    async def run(self, ctx: AgentContext) -> None:
        if ctx.start is not None and ctx.end is not None:
            await self.backfill(ctx.symbol, ctx.interval, ctx.start, ctx.end)
        else:
            await self.refresh(ctx.symbol)
