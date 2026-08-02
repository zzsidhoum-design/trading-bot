"""DashboardService — read-side aggregation for the Phase 7 dashboard.

Thin aggregation over repositories; routes stay thin and never touch ORM
models. Equity is derived from closed trades (cumulative P/L + initial
capital) plus a mark-to-market point for open positions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from qtrader.domain.entities import Position, Trade
from qtrader.domain.ports import (
    Cache,
    DashboardQueries,
    PortfolioRepository,
    PriceRepository,
    RiskRepository,
    StockRepository,
)
from qtrader.domain.value_objects import Interval, TradingMode

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
    ) -> None:
        self._queries = queries
        self._portfolios = portfolios
        self._prices = prices
        self._risks = risks
        self._cache = cache
        self._stocks = stocks

    async def summary(self, portfolio_id: int = 1) -> DashboardSummary | None:
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
        self, portfolio_id: int = 1, limit: int = 200
    ) -> list[EquityPoint]:
        portfolio = await self._portfolios.get(portfolio_id)
        if portfolio is None:
            return []
        initial = portfolio.initial_capital.amount
        trades = await self._queries.trades(portfolio_id, limit=1000)
        closed = [t for t in trades if t.exit_time is not None and t.pnl is not None]
        closed.sort(key=lambda t: t.exit_time)
        points: list[EquityPoint] = []
        equity = initial
        for trade in closed:
            equity += trade.pnl or Decimal(0)
            points.append(EquityPoint(ts=trade.exit_time, equity=equity))
        if not points:
            points.append(EquityPoint(ts=datetime.now(), equity=equity))
        return points[-limit:]

    async def positions(self, portfolio_id: int = 1) -> list[PositionQuote]:
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

    async def allocation(self, portfolio_id: int = 1) -> list[AllocationSlice]:
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
        self, portfolio_id: int = 1, since: datetime | None = None, limit: int = 100
    ) -> list[Trade]:
        return await self._queries.trades(portfolio_id, since, limit)

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
        return await self._queries.performance(strategy, mode, limit)

    async def models(self) -> list[Any]:
        return await self._queries.models()
