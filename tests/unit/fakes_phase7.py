"""Shared fakes for Phase 7 tests (not collected by pytest)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from qtrader.application.services.dashboard_service import DashboardService
from qtrader.domain.entities import (
    AgentMetric,
    BacktestRun,
    IndicatorSnapshot,
    Order,
    PerformanceSummary,
    Portfolio,
    Position,
    Stock,
    SystemLog,
    Trade,
)
from qtrader.domain.ports import (
    BacktestRepository,
    BrokerGateway,
    Cache,
    DashboardQueries,
    EventBus,
    IndicatorRepository,
    ModelRepository,
    NewsRepository,
    OrderRepository,
    PerformanceRepository,
    PortfolioRepository,
    PositionRepository,
    PredictionRepository,
    PriceRepository,
    RiskRepository,
    SignalRepository,
    StockRepository,
    TradeRepository,
)
from qtrader.domain.value_objects import (
    Interval,
    Money,
    OrderFill,
    OrderStatus,
    PriceBar,
    TradingMode,
)


def money(value: str) -> Money:
    return Money(Decimal(value))


def bar(
    symbol: str,
    ts: datetime,
    open: str,
    high: str,
    low: str,
    close: str,
    volume: str = "1000000",
    interval: object = None,
) -> PriceBar:
    return PriceBar(
        symbol=symbol,
        interval=interval if interval is not None else Interval.D1,
        ts=ts,
        open=Decimal(open),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
    )


class FakeDashboardQueries(DashboardQueries):
    def __init__(
        self,
        positions: list[Position] | None = None,
        trades: list[Trade] | None = None,
        logs: list[SystemLog] | None = None,
        metrics: list[AgentMetric] | None = None,
        performance: list[PerformanceSummary] | None = None,
        models: list | None = None,
    ) -> None:
        self.positions_list = positions or []
        self.trades_list = trades or []
        self.logs_list = logs or []
        self.metrics_list = metrics or []
        self.performance_list = performance or []
        self.models_list = models or []

    async def positions(self, portfolio_id: int) -> list[Position]:
        return self.positions_list

    async def trades(
        self, portfolio_id: int, since=None, limit: int = 100
    ) -> list[Trade]:
        return self.trades_list[:limit]

    async def logs(self, level=None, component=None, limit: int = 50) -> list[SystemLog]:
        entries = self.logs_list
        if level is not None:
            entries = [e for e in entries if e.level == level]
        if component is not None:
            entries = [e for e in entries if e.component == component]
        return entries[:limit]

    async def agent_metrics(self, agent_name=None, limit: int = 50) -> list[AgentMetric]:
        return self.metrics_list[:limit]

    async def performance(self, strategy=None, mode=None, limit: int = 50) -> list:
        return self.performance_list[:limit]

    async def models(self) -> list:
        return self.models_list


class FakePortfolioRepository(PortfolioRepository):
    def __init__(self, portfolio=None) -> None:
        self._portfolio = portfolio or None

    async def create(self, portfolio):
        return portfolio

    async def get(self, portfolio_id: int) -> Portfolio | None:
        return self._portfolio

    async def first(self) -> Portfolio | None:
        return self._portfolio

    async def save(self, portfolio):
        return portfolio


class FakePriceRepository(PriceRepository):
    def __init__(self, latest_bar=None) -> None:
        self._latest = latest_bar

    async def upsert_bars(self, bars: list[PriceBar]) -> int:
        return len(bars)

    async def latest(self, symbol: str, interval) -> PriceBar | None:
        return self._latest if self._latest is None or self._latest.symbol == symbol else None

    async def history(self, symbol, interval, start=None, end=None, limit=500) -> list[PriceBar]:
        return [self._latest] if self._latest is not None else []


class FakeStockRepository(StockRepository):
    def __init__(self, stocks: list[Stock] | None = None) -> None:
        self._stocks: dict[str, Stock] = {s.symbol: s for s in (stocks or [])}
        self._next_id = max([s.stock_id or 0 for s in self._stocks.values()], default=0) + 1
        self.upserted: list[Stock] = []

    async def upsert(self, stock: Stock) -> Stock:
        if stock.stock_id is None:
            stock = replace(stock, stock_id=self._next_id)
            self._next_id += 1
        self._stocks[stock.symbol] = stock
        self.upserted.append(stock)
        return stock

    async def get_by_symbol(self, symbol: str, exchange: str | None = None) -> Stock | None:
        return self._stocks.get(symbol)

    async def list_active(self) -> list[Stock]:
        return list(self._stocks.values())

    async def search(self, query, sector, limit, offset) -> list[Stock]:
        return list(self._stocks.values())[:limit]


class FakeRiskRepository(RiskRepository):
    def __init__(self, assessments=None) -> None:
        self.assessments = assessments or []

    async def record(self, assessment):
        return assessment

    async def recent(self, limit: int = 50) -> list:
        return self.assessments[:limit]


class FakeCache(Cache):
    def __init__(self) -> None:
        self.zsets: dict[str, list[tuple[str, float]]] = {}
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        self.values[key] = value

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)

    async def zadd(self, key: str, mapping: dict[str, float]) -> None:
        entries = dict(self.zsets.get(key, []))
        entries.update(mapping)
        self.zsets[key] = sorted(entries.items(), key=lambda kv: kv[1], reverse=True)

    async def zrevrange(self, key: str, start: int, end: int) -> list[tuple[str, float]]:
        return self.zsets.get(key, [])[start : end + 1]


class FakeIndicatorRepository(IndicatorRepository):
    def __init__(self, snapshot: IndicatorSnapshot | None = None) -> None:
        self._snapshot = snapshot

    async def save_snapshot(self, snapshot) -> None:
        return None

    async def latest(self, symbol: str, interval) -> IndicatorSnapshot | None:
        return self._snapshot


class FakeOrderRepository(OrderRepository):
    def __init__(self) -> None:
        self.orders: list[Order] = []
        self._next = 1

    async def create(self, order: Order) -> Order:
        created = replace(order, order_id=self._next)
        self._next += 1
        self.orders.append(created)
        return created

    async def save(self, order: Order) -> Order:
        self.orders = [o if o.order_id != order.order_id else order for o in self.orders]
        return order

    async def get_by_idempotency_key(self, key: str) -> Order | None:
        return next((o for o in self.orders if o.idempotency_key == key), None)

    async def list_by_portfolio(
        self, portfolio_id: int, status=None, limit: int = 100
    ) -> list[Order]:
        orders = [o for o in self.orders if o.portfolio_id == portfolio_id]
        if status is not None:
            orders = [o for o in orders if o.status == status]
        return orders[:limit]


class FakePositionRepository(PositionRepository):
    def __init__(self, positions: list[Position] | None = None) -> None:
        self._positions = positions or []
        self.saved: list[Position] = []

    async def open_positions(self, portfolio_id: int) -> list[Position]:
        return [p for p in self._positions if p.status.value == "OPEN"]

    async def save(self, position: Position) -> Position:
        self.saved.append(position)
        return position


class FakeTradeRepository(TradeRepository):
    def __init__(self) -> None:
        self.trades: list[Trade] = []

    async def record(self, trade: Trade) -> Trade:
        self.trades.append(trade)
        return trade


class FakeEventBus(EventBus):
    def __init__(self) -> None:
        self.published: list[object] = []

    def subscribe(self, event_type, handler) -> None:
        return None

    async def publish(self, event) -> None:
        self.published.append(event)

    async def close(self) -> None:
        return None


class FakeBrokerGateway(BrokerGateway):
    def __init__(self, filled: bool = True) -> None:
        self.submitted: list[Order] = []
        self._filled = filled

    async def submit_order(self, order: Order) -> str:
        self.submitted.append(order)
        return f"brk-{order.order_id}"

    async def cancel_order(self, broker_order_id: str) -> None:
        return None

    async def modify_brackets(self, position_id: str, stop_loss, take_profit) -> None:
        return None

    async def get_order_status(self, broker_order_id: str) -> OrderFill:
        if self._filled:
            return OrderFill(
                broker_order_id=broker_order_id,
                filled_qty=1,
                avg_fill_price=Decimal("100"),
                commission=Decimal("0"),
                status=OrderStatus.FILLED,
            )
        return OrderFill(
            broker_order_id=broker_order_id,
            filled_qty=0,
            avg_fill_price=Decimal("0"),
            commission=Decimal("0"),
            status=OrderStatus.REJECTED,
        )


class FakeSignalRepository(SignalRepository):
    def __init__(self) -> None:
        self.saved: list = []
        self.latest: list = []

    async def save(self, signal):
        self.saved.append(signal)
        return signal

    async def latest_for_symbol(self, symbol: str, agent: str | None = None) -> list:
        return [s for s in self.latest if s.symbol == symbol]


class FakeNewsRepository(NewsRepository):
    def __init__(self) -> None:
        self.items: list = []

    async def upsert(self, items: list) -> int:
        self.items.extend(items)
        return len(items)

    async def recent(self, symbol: str | None, since, limit: int) -> list:
        return self.items[:limit]


class FakePredictionRepository(PredictionRepository):
    def __init__(self) -> None:
        self.predictions: list = []

    async def save(self, prediction):
        self.predictions.append(prediction)
        return prediction

    async def latest_for_symbol(self, symbol: str, limit: int = 20) -> list:
        return self.predictions[:limit]


class FakeModelRepository(ModelRepository):
    def __init__(self, models: list | None = None) -> None:
        self.models = models or []
        self.promoted: list[tuple[str, int]] = []

    async def load_active(self, name: str):
        return next((m for m in self.models if m.name == name and m.is_active), None)

    async def promote(self, name: str, version: int) -> None:
        self.promoted.append((name, version))

    async def create_version(self, name, hyperparams, training_window, offline_metrics):
        raise NotImplementedError


class FakeBacktestRepository(BacktestRepository):
    def __init__(self, runs: list[BacktestRun] | None = None) -> None:
        self.runs = runs or []

    async def create(self, run):
        return run

    async def save(self, run):
        return run

    async def get(self, run_id: int):
        return next((r for r in self.runs if r.run_id == run_id), None)

    async def latest(self, name=None, limit=20):
        return self.runs[:limit]


class FakeBacktestRunner:
    def __init__(self, result=None) -> None:
        self.result = result

    async def run(self, **kwargs):
        from qtrader.application.services.backtest import BacktestParams, BacktestResult
        from qtrader.domain.entities import PerformanceSummary

        params: BacktestParams = kwargs["params"]
        run = BacktestRun(
            name=kwargs.get("name", "manual"),
            universe=kwargs.get("symbols", []),
            start=kwargs["start"],
            end=kwargs["end"],
            initial_capital=money(str(kwargs.get("initial_capital", 100000))),
            interval=params.interval,
            strategy=params.strategy,
            commission_bps=Decimal(str(params.commission_bps)),
            slippage_bps=Decimal(str(params.slippage_bps)),
            run_id=1,
            status="completed",
            created_at=datetime(2026, 8, 1, tzinfo=UTC),
            final_capital=money("100000"),
            metrics=PerformanceSummary(
                strategy=params.strategy,
                mode=TradingMode.BACKTEST,
                period_start=kwargs["start"],
                period_end=kwargs["end"],
                trades_count=0,
            ),
        )
        return BacktestResult(
            run=run,
            summary=run.metrics,
            equity_curve=[],
            trades=[],
        )


class FakePerformanceRepository(PerformanceRepository):
    def __init__(self, summaries=None) -> None:
        self.summaries = summaries or []

    async def upsert(self, summary):
        self.summaries.append(summary)
        return summary

    async def latest_for_strategy(self, strategy: str, mode: TradingMode):
        for s in reversed(self.summaries):
            if s.strategy == strategy and s.mode is mode:
                return s
        return None


def make_dashboard_service(
    *,
    positions: list[Position] | None = None,
    trades: list[Trade] | None = None,
    portfolio=None,
    portfolio_repo: FakePortfolioRepository | None = None,
    latest_bar=None,
    zsets: dict[str, list[tuple[str, float]]] | None = None,
    stocks: list[Stock] | None = None,
) -> DashboardService:
    if portfolio is None and portfolio_repo is None:
        portfolio = _default_portfolio()
    cache = FakeCache()
    cache.zsets = zsets or {}
    return DashboardService(
        queries=FakeDashboardQueries(
            positions=positions,
            trades=trades,
            metrics=[
                AgentMetric(
                    agent_name="technical",
                    metric_name="rsi",
                    value=Decimal("55"),
                    computed_at=datetime(2026, 8, 1, tzinfo=UTC),
                )
            ],
        ),
        portfolios=portfolio_repo
        if portfolio_repo is not None
        else FakePortfolioRepository(portfolio),
        prices=FakePriceRepository(latest_bar),
        risks=FakeRiskRepository(),
        cache=cache,
        stocks=FakeStockRepository(stocks or []),
    )


def _default_portfolio():
    return Portfolio(
        name="default",
        currency="USD",
        initial_capital=money("100000"),
        current_cash=money("65000"),
        mode=TradingMode.BACKTEST,
        portfolio_id=1,
    )


def make_position(
    *,
    symbol: str = "AAPL",
    quantity: int = 10,
    avg: str = "100",
    status: str = "OPEN",
    position_id: int | None = 1,
) -> Position:
    from qtrader.domain.value_objects import PositionStatus

    return Position(
        portfolio_id=1,
        stock_id=1,
        quantity=quantity,
        avg_entry_price=money(avg),
        symbol=symbol,
        status=PositionStatus(status),
        position_id=position_id,
    )
