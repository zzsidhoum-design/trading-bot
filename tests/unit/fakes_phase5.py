"""Shared fakes for the Phase 5 agent tests (not collected by pytest)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from qtrader.domain.entities import (
    IndicatorSnapshot,
    Order,
    Portfolio,
    Position,
    RiskAssessment,
    Stock,
    Trade,
)
from qtrader.domain.events import DomainEvent
from qtrader.domain.ports import (
    EventBus,
    IndicatorRepository,
    OrderRepository,
    PortfolioRepository,
    PositionRepository,
    PriceRepository,
    RiskRepository,
    StockRepository,
    TradeRepository,
)
from qtrader.domain.value_objects import Money, PriceBar, TradingMode

_NOW = datetime.now(UTC)


class FakeEventBus(EventBus):
    def __init__(self) -> None:
        self.published: list[DomainEvent] = []

    async def publish(self, event: DomainEvent) -> None:
        self.published.append(event)

    def subscribe(self, event_type, handler) -> None:
        pass

    async def close(self) -> None:
        pass


class FakePriceRepository(PriceRepository):
    def __init__(self, close: str | None = "100") -> None:
        self._close = Decimal(close) if close is not None else None

    async def upsert_bars(self, bars) -> int:
        return len(bars)

    async def latest(self, symbol: str, interval) -> PriceBar | None:
        if self._close is None:
            return None
        return PriceBar(
            symbol=symbol,
            interval=interval,
            ts=_NOW,
            open=self._close,
            high=self._close,
            low=self._close,
            close=self._close,
            volume=Decimal("1000"),
        )

    async def history(self, symbol, interval, start=None, end=None, limit=500) -> list[PriceBar]:
        return []


class FakeIndicatorRepository(IndicatorRepository):
    def __init__(self, atr: str = "2") -> None:
        self._atr = Decimal(atr)

    async def save_snapshot(self, snapshot) -> None:
        pass

    async def latest(self, symbol: str, interval) -> IndicatorSnapshot | None:
        if self._atr is None:
            return None
        return IndicatorSnapshot(symbol=symbol, interval=interval, ts=_NOW, atr=self._atr)


class FakeStockRepository(StockRepository):
    def __init__(self, sector: str = "Tech") -> None:
        self._sector = sector

    async def upsert(self, stock: Stock) -> Stock:
        return replace(stock, stock_id=stock.stock_id or 1)

    async def get_by_symbol(self, symbol: str, exchange: str | None = None) -> Stock | None:
        return Stock(symbol=symbol, exchange="TEST", sector=self._sector, stock_id=1)

    async def list_active(self) -> list[Stock]:
        return []

    async def search(self, query, sector, limit, offset) -> list[Stock]:
        return []


class FakePositionRepository(PositionRepository):
    def __init__(self, positions: list[Position] | None = None) -> None:
        self._positions = list(positions or [])

    async def open_positions(self, portfolio_id: int) -> list[Position]:
        return [
            p
            for p in self._positions
            if p.portfolio_id == portfolio_id and p.status.value == "OPEN"
        ]

    async def save(self, position: Position) -> Position:
        if position.position_id is None:
            position = replace(position, position_id=1)
        self._positions = [
            p if p.position_id != position.position_id else position for p in self._positions
        ]
        if position not in self._positions:
            self._positions.append(position)
        return position


class FakeOrderRepository(OrderRepository):
    def __init__(self, orders: list[Order] | None = None) -> None:
        self._orders = list(orders or [])

    async def create(self, order: Order) -> Order:
        order = replace(order, order_id=len(self._orders) + 1)
        self._orders.append(order)
        return order

    async def save(self, order: Order) -> Order:
        self._orders = [o if o.order_id != order.order_id else order for o in self._orders]
        return order

    async def get_by_idempotency_key(self, key: str) -> Order | None:
        for order in self._orders:
            if order.idempotency_key == key:
                return order
        return None

    async def list_by_portfolio(self, portfolio_id, status=None, limit=100) -> list[Order]:
        orders = [
            o
            for o in self._orders
            if o.portfolio_id == portfolio_id
            and (status is None or o.status.value == status)
        ]
        return orders[:limit]


class FakeRiskRepository(RiskRepository):
    def __init__(self) -> None:
        self.assessments: list[RiskAssessment] = []

    async def record(self, assessment: RiskAssessment) -> RiskAssessment:
        assessment = replace(assessment, risk_id=len(self.assessments) + 1)
        self.assessments.append(assessment)
        return assessment

    async def recent(self, limit: int = 50) -> list[RiskAssessment]:
        return self.assessments[-limit:]


class FakeTradeRepository(TradeRepository):
    def __init__(self) -> None:
        self.trades: list[Trade] = []

    async def record(self, trade: Trade) -> Trade:
        trade = replace(trade, trade_id=len(self.trades) + 1)
        self.trades.append(trade)
        return trade


class FakePortfolioRepository(PortfolioRepository):
    def __init__(self, portfolio: Portfolio | None = None) -> None:
        self._portfolio = portfolio

    async def create(self, portfolio: Portfolio) -> Portfolio:
        self._portfolio = replace(portfolio, portfolio_id=1)
        return self._portfolio

    async def get(self, portfolio_id: int) -> Portfolio | None:
        return self._portfolio

    async def first(self) -> Portfolio | None:
        return self._portfolio

    async def save(self, portfolio: Portfolio) -> Portfolio:
        self._portfolio = portfolio
        return portfolio


def default_portfolio(cash: str = "100000") -> Portfolio:
    return Portfolio(
        name="default",
        initial_capital=Money("100000"),
        current_cash=Money(cash),
        mode=TradingMode.PAPER,
        portfolio_id=1,
    )
