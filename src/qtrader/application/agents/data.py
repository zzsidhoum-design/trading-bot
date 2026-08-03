"""Data Agent â€” reliable, clean market data in the DB (docs/02-agents.md Â§1).

Ingests bars from a MarketDataProvider, cleans them, persists via the
PriceRepository and publishes ``PriceUpdated`` / ``BackfillCompleted``. The
latest quote is written through to the cache so downstream agents never wait
on the DB for a "current price".
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar

from qtrader.application.agents.base import AgentBase, AgentContext
from qtrader.application.services.bar_cleaner import BarCleaner
from qtrader.domain.events import BackfillCompleted, DomainEvent, PriceUpdated
from qtrader.domain.ports import Cache, EventBus, MarketDataProvider, PriceRepository
from qtrader.domain.value_objects import Interval, PriceBar


def _to_str(value: Decimal) -> str:
    return str(value)


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
        quote_cache_ttl_seconds: int = 300,
    ) -> None:
        self._provider = provider
        self._prices = prices
        self._cache = cache
        self._bus = bus
        self._cleaner = cleaner
        self._quote_ttl = quote_cache_ttl_seconds

    async def backfill(
        self, symbol: str, interval: Interval, start: datetime, end: datetime
    ) -> int:
        """Fetch + clean + persist a historical range; publish completion."""
        end = end.astimezone(UTC)
        start = start.astimezone(UTC)
        try:
            raw = await self._provider.fetch_bars(symbol, interval, start, end)
        except RuntimeError as exc:
            self._logger.warning("data.backfill.provider_down", symbol=symbol, error=str(exc))
            return 0
        report = self._cleaner.clean(raw, now=end, reject_stale=False)
        inserted = await self._prices.upsert_bars(report.kept)
        self._logger.info(
            "data.backfill",
            symbol=symbol,
            interval=interval,
            fetched=len(raw),
            kept=len(report.kept),
            inserted=inserted,
            dropped=report.dropped,
            reasons=report.reasons,
        )
        if report.kept:
            await self._cache_quote(report.kept[-1])
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
        """Fetch the latest quote, persist and publish ``PriceUpdated``."""
        try:
            quote = await self._provider.fetch_quote(symbol)
        except RuntimeError as exc:
            self._logger.warning("data.refresh.no_quote", symbol=symbol, error=str(exc))
            return None
        report = self._cleaner.clean([quote], reject_stale=True)
        if not report.kept:
            self._logger.warning(
                "data.refresh.dropped",
                symbol=symbol,
                ts=quote.ts.isoformat(),
                reasons=report.reasons,
            )
            return None
        bar = report.kept[0]
        inserted = await self._prices.upsert_bars([bar])
        await self._cache_quote(bar)
        if inserted:
            await self._bus.publish(
                PriceUpdated(
                    symbol=bar.symbol,
                    interval=bar.interval,
                    ts=bar.ts.isoformat(),
                    open=_to_str(bar.open),
                    high=_to_str(bar.high),
                    low=_to_str(bar.low),
                    close=_to_str(bar.close),
                    volume=_to_str(bar.volume),
                )
            )
        return bar

    async def _cache_quote(self, bar: PriceBar) -> None:
        payload: dict[str, Any] = {
            "symbol": bar.symbol,
            "interval": bar.interval,
            "ts": bar.ts.isoformat(),
            "open": _to_str(bar.open),
            "high": _to_str(bar.high),
            "low": _to_str(bar.low),
            "close": _to_str(bar.close),
            "volume": _to_str(bar.volume),
        }
        await self._cache.set(
            f"quote:{bar.symbol}", json.dumps(payload), ttl_seconds=self._quote_ttl
        )

    async def run(self, ctx: AgentContext) -> None:
        if ctx.start is not None and ctx.end is not None:
            await self.backfill(ctx.symbol, ctx.interval, ctx.start, ctx.end)
        else:
            await self.refresh(ctx.symbol)
