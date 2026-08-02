"""DashboardService — read-side aggregation for the Phase 7 dashboard.

Thin aggregation over repositories; routes stay thin and never touch ORM
models. Equity is derived from closed trades (cumulative P/L + initial
capital) plus a mark-to-market point for open positions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from qtrader.application.services.performance_metrics import PerformanceMetrics
from qtrader.application.services.portfolio_service import PortfolioService
from qtrader.domain.entities import Position, Trade
from qtrader.domain.ports import (
    Cache,
    DashboardQueries,
    PortfolioRepository,
    PriceRepository,
    RiskRepository,
    StockRepository,
)
from qtrader.domain.value_objects import Interval, TradeSide, TradingMode

_SCAN_PREFIX = "scan:top"


@dataclass(frozen=True, slots=True)
class EquityPoint:
    ts: datetime
    equity: Decimal


@dataclass(frozen=True, slots=True)
class PositionQuote:
    position: Position
    current_price: Decimal | None
    unrealized_pnl: Decimal | None


@dataclass(frozen=True, slots=True)
class AllocationSlice:
    symbol: str
    sector: str | None
    market_value: Decimal
    weight_pct: float


@dataclass(frozen=True, slots=True)
class DashboardSummary:
    cash: Decimal
    equity: Decimal
    open_positions: int
    unrealized_pnl: Decimal
    exposure_pct: float
    total_trades: int


class DashboardService:
    def __init__(
        self,
        queries: DashboardQueries,
        portfolios: PortfolioRepository,
        prices: PriceRepository,
        risks: RiskRepository,
        cache: Cache,
        stocks: StockRepository,
        portfolio_service: PortfolioService | None = None,
    ) -> None:
        self._queries = queries
        self._portfolios = portfolios
        self._prices = prices
        self._risks = risks
        self._cache = cache
        self._stocks = stocks
        self._portfolio_service = portfolio_service

    async def _resolve_portfolio_id(self, portfolio_id: int | None) -> int:
        if portfolio_id is not None:
            return portfolio_id
        if self._portfolio_service is not None:
            portfolio = await self._portfolio_service.default_portfolio()
            if portfolio.portfolio_id is not None:
                return portfolio.portfolio_id
        return 1

    async def summary(self, portfolio_id: int | None = None) -> DashboardSummary | None:
        portfolio_id = await self._resolve_portfolio_id(portfolio_id)
        portfolio = await self._portfolios.get(portfolio_id)
        if portfolio is None:
            return None
        positions = await self._queries.positions(portfolio_id)
        trades = await self._queries.trades(portfolio_id, limit=1000)
        open_positions = [p for p in positions if p.status.value == "OPEN"]
        unrealized = Decimal(0)
        notional = Decimal(0)
        for position in open_positions:
            bar = await self._prices.latest(position.symbol or "", Interval.D1)
            price = bar.close if bar else position.avg_entry_price.amount
            market_value = Decimal(position.quantity) * price
            notional += market_value
            unrealized += market_value - (
                Decimal(position.quantity) * position.avg_entry_price.amount
            )
        cash = portfolio.current_cash.amount
        equity = cash + notional
        return DashboardSummary(
            cash=cash,
            equity=equity,
            open_positions=len(open_positions),
            unrealized_pnl=unrealized,
            exposure_pct=float(notional / equity) if equity else 0.0,
            total_trades=len(trades),
        )

    async def equity_curve(
        self, portfolio_id: int | None = None, limit: int = 200
    ) -> list[EquityPoint]:
        portfolio_id = await self._resolve_portfolio_id(portfolio_id)
        portfolio = await self._portfolios.get(portfolio_id)
        if portfolio is None:
            return []
        initial = portfolio.initial_capital.amount
        trades = await self._queries.trades(portfolio_id, limit=1000)
        closed = [
            t
            for t in trades
            if t.exit_time is not None
            and t.pnl is not None
            and (t.outcome or "closed") != "open"
        ]
        closed.sort(key=lambda t: t.exit_time)
        events: list[tuple[datetime, Decimal]] = []
        equity = initial
        for trade in closed:
            equity += trade.pnl or Decimal(0)
            events.append((trade.exit_time, equity))
        notional = Decimal(0)
        for position in await self._queries.positions(portfolio_id):
            if position.status.value != "OPEN" or not position.symbol:
                continue
            bar = await self._prices.latest(position.symbol, Interval.D1)
            price = bar.close if bar else position.avg_entry_price.amount
            notional += Decimal(position.quantity) * price
            events.append((position.opened_at, equity))
        events.append((datetime.now(UTC), portfolio.current_cash.amount + notional))
        events.sort(key=lambda event: event[0])
        points: list[EquityPoint] = []
        for ts, value in events:
            if points and points[-1].ts == ts:
                points[-1] = EquityPoint(ts=ts, equity=value)
            else:
                points.append(EquityPoint(ts=ts, equity=value))
        return points[-limit:]

    async def positions(self, portfolio_id: int | None = None) -> list[PositionQuote]:
        portfolio_id = await self._resolve_portfolio_id(portfolio_id)
        quotes: list[PositionQuote] = []
        for position in await self._queries.positions(portfolio_id):
            bar = await self._prices.latest(position.symbol or "", Interval.D1)
            current = bar.close if bar else None
            unrealized = None
            if current is not None and position.status.value == "OPEN":
                unrealized = (current - position.avg_entry_price.amount) * Decimal(
                    position.quantity
                )
            quotes.append(PositionQuote(position, current, unrealized))
        return quotes

    async def allocation(self, portfolio_id: int | None = None) -> list[AllocationSlice]:
        portfolio_id = await self._resolve_portfolio_id(portfolio_id)
        portfolio = await self._portfolios.get(portfolio_id)
        if portfolio is None:
            return []
        equity = portfolio.current_cash.amount
        symbols = {p.symbol for p in await self._queries.positions(portfolio_id) if p.symbol}
        sectors = {
            s.symbol: s.sector for s in await self._stocks.list_active() if s.symbol in symbols
        }
        slices: list[AllocationSlice] = []
        for position in await self._queries.positions(portfolio_id):
            if position.status.value != "OPEN" or not position.symbol:
                continue
            bar = await self._prices.latest(position.symbol, Interval.D1)
            price = bar.close if bar else position.avg_entry_price.amount
            market_value = Decimal(position.quantity) * price
            equity += market_value
            slices.append(
                AllocationSlice(
                    symbol=position.symbol,
                    sector=sectors.get(position.symbol),
                    market_value=market_value,
                    weight_pct=0.0,
                )
            )
        weighted: list[AllocationSlice] = []
        for slice_ in slices:
            weighted.append(
                AllocationSlice(
                    symbol=slice_.symbol,
                    sector=slice_.sector,
                    market_value=slice_.market_value,
                    weight_pct=float(slice_.market_value / equity) if equity else 0.0,
                )
            )
        return weighted

    async def top_stocks(self, metric: str = "overall", limit: int = 20) -> list[tuple[str, float]]:
        key = f"{_SCAN_PREFIX}:{metric}" if metric != "overall" else f"{_SCAN_PREFIX}:overall"
        return await self._cache.zrevrange(key, 0, max(limit - 1, 0))

    async def trades(
        self, portfolio_id: int | None = None, since: datetime | None = None, limit: int = 100
    ) -> list[Trade]:
        portfolio_id = await self._resolve_portfolio_id(portfolio_id)
        portfolio = await self._portfolios.get(portfolio_id)
        mode = portfolio.mode if portfolio is not None else TradingMode.PAPER
        records = list(await self._queries.trades(portfolio_id, since, limit))
        for position in await self._queries.positions(portfolio_id):
            if position.status.value != "OPEN" or not position.symbol:
                continue
            if since is not None and position.opened_at < since:
                continue
            bar = await self._prices.latest(position.symbol, Interval.D1)
            current = bar.close if bar else position.avg_entry_price.amount
            entry = position.avg_entry_price.amount
            quantity = Decimal(position.quantity)
            records.append(
                Trade(
                    portfolio_id=portfolio_id,
                    stock_id=position.stock_id,
                    symbol=position.symbol,
                    strategy="portfolio",
                    side=TradeSide.BUY,
                    quantity=quantity,
                    entry_price=entry,
                    exit_price=current,
                    pnl=(current - entry) * quantity,
                    pnl_pct=((current - entry) / entry) if entry else Decimal(0),
                    fees=Decimal(0),
                    entry_time=position.opened_at,
                    exit_time=position.opened_at,
                    outcome="open",
                    mode=mode,
                    position_id=position.position_id,
                )
            )
        records.sort(key=lambda t: t.entry_time, reverse=True)
        return records[:limit]

    async def risk(self, limit: int = 50) -> list[Any]:
        return await self._risks.recent(limit)

    async def agents(self, limit: int = 50) -> list[Any]:
        return await self._queries.agent_metrics(limit=limit)

    async def logs(
        self, level: str | None = None, component: str | None = None, limit: int = 50
    ) -> list[Any]:
        return await self._queries.logs(level, component, limit)

    async def performance(
        self, strategy: str | None = None, mode: TradingMode | None = None, limit: int = 50
    ) -> list[Any]:
        stored = await self._queries.performance(strategy, mode, limit)
        if mode is not None and mode is not TradingMode.BACKTEST:
            live = await self._live_performance(strategy, mode)
            return live + stored
        if mode is None:
            live = await self._live_performance(strategy, None)
            return (live + stored) if live else stored
        return stored

    async def _live_performance(
        self, strategy: str | None, mode: TradingMode | None, limit: int = 50
    ) -> list[Any]:
        """Compute current (trade-based) performance from the live portfolio.

        Groups closed trades by (strategy, mode) and derives a summary from the
        cumulative P/L series plus a final mark-to-market point so the dashboard
        reflects actual account activity instead of stale backtest rows only.
        """
        portfolio_id = await self._resolve_portfolio_id(None)
        portfolio = await self._portfolios.get(portfolio_id)
        if portfolio is None:
            return []
        closed = [
            t
            for t in await self._queries.trades(portfolio_id, limit=1000)
            if t.pnl is not None and (t.outcome or "closed") != "open"
        ]
        if mode is not None:
            closed = [t for t in closed if t.mode == mode]
        if strategy is not None:
            closed = [t for t in closed if t.strategy == strategy]
        if not closed:
            return []
        groups: dict[tuple[str, TradingMode], list[Trade]] = {}
        for trade in closed:
            groups.setdefault((trade.strategy, trade.mode), []).append(trade)
        summaries: list[Any] = []
        for (group_strategy, group_mode), group in sorted(
            groups.items(),
            key=lambda item: min(t.entry_time for t in item[1]),
            reverse=True,
        ):
            group = sorted(group, key=lambda t: t.entry_time)
            series: list[tuple[datetime, Decimal]] = [
                (group[0].entry_time, portfolio.initial_capital.amount)
            ]
            equity = portfolio.initial_capital.amount
            for trade in group:
                equity += trade.pnl or Decimal(0)
                series.append((trade.exit_time or trade.entry_time, equity))
            notional = Decimal(0)
            for position in await self._queries.positions(portfolio_id):
                if position.status.value != "OPEN" or not position.symbol:
                    continue
                bar = await self._prices.latest(position.symbol, Interval.D1)
                price = bar.close if bar else position.avg_entry_price.amount
                notional += Decimal(position.quantity) * price
            series.append((datetime.now(UTC), portfolio.current_cash.amount + notional))
            summaries.append(
                PerformanceMetrics.from_series(
                    strategy=group_strategy,
                    mode=group_mode,
                    period_start=min(t.entry_time.date() for t in group),
                    period_end=max(t.exit_time or t.entry_time for t in group).date(),
                    equity_curve=series,
                    trade_pnl_pcts=[t.pnl_pct or Decimal(0) for t in group],
                    interval=Interval.D1,
                )
            )
        return summaries[:limit]

    async def models(self) -> list[Any]:
        return await self._queries.models()
