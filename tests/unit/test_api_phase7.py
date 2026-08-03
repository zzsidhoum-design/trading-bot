"""Unit tests for Phase 7 API routes (fake container, no infra)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI

from qtrader.application.agents.execution import ExecutionAgent
from qtrader.application.services.backtest import BacktestRunner
from qtrader.application.services.dashboard_service import DashboardService
from qtrader.application.services.portfolio_service import PortfolioService
from qtrader.application.services.risk_calculator import RiskCalculator, RiskPolicy
from qtrader.application.use_cases.manual_order import ManualOrder
from qtrader.config.settings import Settings
from qtrader.domain.entities import BacktestRun, Portfolio, RegisteredModel, Stock, Trade
from qtrader.domain.ports import EventRepository
from qtrader.domain.value_objects import Interval, Money, TradeSide, TradingMode
from qtrader.interfaces.api.app import create_app
from qtrader.interfaces.api.dependencies import get_container
from tests.unit.fakes_phase7 import (
    FakeBacktestRepository,
    FakeBacktestRunner,
    FakeBrokerGateway,
    FakeCache,
    FakeDashboardQueries,
    FakeEventBus,
    FakeIndicatorRepository,
    FakeModelRepository,
    FakeNewsRepository,
    FakeOrderRepository,
    FakePortfolioRepository,
    FakePositionRepository,
    FakePredictionRepository,
    FakePriceRepository,
    FakeRiskRepository,
    FakeSignalRepository,
    FakeStockRepository,
    FakeTradeRepository,
    bar,
    make_position,
    money,
)

API_KEY = "test-key"


class FakeEventRepository(EventRepository):
    def __init__(self) -> None:
        self.events: list = []

    async def record(self, event) -> None:
        self.events.append(event)

    async def list_after(self, event_uuid, event_type, limit) -> list:
        return list(self.events)[:limit]

    async def count_by_type(self, limit=1000) -> dict[str, int]:
        return {e.type_name: self.events.count(e) for e in set(self.events)}


class FakeContainer:
    def __init__(self) -> None:
        self._settings = Settings(_env_file=None, api_key=API_KEY, qtrader_mode=TradingMode.PAPER)
        self.portfolio = Portfolio(
            name="default",
            currency="USD",
            initial_capital=money("100000"),
            current_cash=money("65000"),
            mode=TradingMode.BACKTEST,
            portfolio_id=1,
        )
        self.portfolios = FakePortfolioRepository(self.portfolio)
        self.stocks = FakeStockRepository(
            [Stock(symbol="AAPL", exchange="XNAS", name="Apple", stock_id=1)]
        )
        self.prices = FakePriceRepository(
            bar("AAPL", datetime(2026, 8, 1, tzinfo=UTC), "109", "111", "108", "110")
        )
        self.positions = FakePositionRepository(
            [make_position(symbol="AAPL", quantity=10, avg="100")]
        )
        self.orders = FakeOrderRepository()
        self.trades = FakeTradeRepository()
        self.bus = FakeEventBus()
        self.broker = FakeBrokerGateway(filled=True)
        self.indicators = FakeIndicatorRepository()
        self.signals = FakeSignalRepository()
        self.news = FakeNewsRepository()
        self.predictions = FakePredictionRepository()
        self.risks = FakeRiskRepository()
        self.models = FakeModelRepository(
            [
                RegisteredModel(
                    name="momentum", version=3, is_active=True, model_id=1, status="active"
                )
            ]
        )
        self.backtest_repo = FakeBacktestRepository()
        self.backtest_runner = FakeBacktestRunner()
        self.events = FakeEventRepository()
        self.cache = FakeCache()
        self.cache.zsets = {"scan:top:overall": [("AAPL", 0.9)]}
        self.queries = FakeDashboardQueries(
            positions=[make_position(symbol="AAPL", quantity=10, avg="100")],
            trades=[
                Trade(
                    portfolio_id=1,
                    stock_id=1,
                    symbol="AAPL",
                    strategy="ensemble",
                    side=TradeSide.SELL,
                    quantity=Decimal("10"),
                    entry_price=Decimal("100"),
                    exit_price=Decimal("110"),
                    pnl=Decimal("100"),
                    pnl_pct=Decimal("0.1"),
                    fees=Decimal("0"),
                    entry_time=datetime(2026, 8, 1, tzinfo=UTC),
                    exit_time=datetime(2026, 8, 1, tzinfo=UTC),
                    outcome="closed",
                    mode=TradingMode.BACKTEST,
                )
            ],
            models=self.models.models,
        )
        self.dashboard = DashboardService(
            queries=self.queries,
            portfolios=self.portfolios,
            prices=self.prices,
            risks=self.risks,
            cache=self.cache,
            stocks=self.stocks,
        )
        execution = ExecutionAgent(
            broker=self.broker,
            portfolio_service=PortfolioService(self.portfolios, default_id=1),
            portfolios=self.portfolios,
            positions=self.positions,
            orders=self.orders,
            stocks=self.stocks,
            trades=self.trades,
            bus=self.bus,
            gate=None,
        )
        self.execution = execution
        self.manual_order = ManualOrder(
            portfolios=PortfolioService(self.portfolios, default_id=1),
            stocks=self.stocks,
            prices=self.prices,
            indicators=self.indicators,
            positions=self.positions,
            orders=self.orders,
            risk_calculator=RiskCalculator(RiskPolicy()),
            execution=execution,
            settings=self._settings,
        )
        self._registry: dict[type, object] = {
            Settings: self._settings,
            DashboardService: self.dashboard,
            ManualOrder: self.manual_order,
            ExecutionAgent: self.execution,
            BacktestRunner: self.backtest_runner,
            PortfolioService: PortfolioService(self.portfolios, default_id=1),
        }

    def resolve(self, service_type: type) -> object:
        if service_type in self._registry:
            return self._registry[service_type]
        for instance in (
            self.stocks,
            self.prices,
            self.portfolios,
            self.positions,
            self.orders,
            self.trades,
            self.bus,
            self.broker,
            self.indicators,
            self.signals,
            self.news,
            self.predictions,
            self.risks,
            self.models,
            self.backtest_repo,
            self.events,
            self.cache,
            self.queries,
        ):
            if isinstance(instance, service_type):
                return instance
        raise KeyError(f"no fake for {service_type}")

    async def database_healthy(self) -> bool:
        return True

    async def cache_healthy(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


@pytest.fixture
def app() -> FastAPI:
    application = create_app()
    application.dependency_overrides[get_container] = lambda: FakeContainer()
    return application


@pytest.fixture
async def client(app: FastAPI):
    from httpx import ASGITransport

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _get(client: httpx.AsyncClient, path: str) -> httpx.Response:
    return await client.get(path, headers={"X-API-Key": API_KEY})


@pytest.mark.asyncio
async def test_dashboard_summary(client: httpx.AsyncClient) -> None:
    resp = await _get(client, "/api/v1/dashboard/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cash"] == "65000.000000"
    assert body["open_positions"] == 1
    assert body["total_trades"] == 1


@pytest.mark.asyncio
async def test_dashboard_equity(client: httpx.AsyncClient) -> None:
    resp = await _get(client, "/api/v1/dashboard/equity")
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["equity"] == "100100.000000"


@pytest.mark.asyncio
async def test_dashboard_positions(client: httpx.AsyncClient) -> None:
    resp = await _get(client, "/api/v1/dashboard/positions")
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["symbol"] == "AAPL"
    assert body[0]["unrealized_pnl"] == "100.000000"


@pytest.mark.asyncio
async def test_dashboard_trades(client: httpx.AsyncClient) -> None:
    resp = await _get(client, "/api/v1/dashboard/trades")
    assert resp.status_code == 200
    assert resp.json()[0]["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_dashboard_top_stocks(client: httpx.AsyncClient) -> None:
    resp = await _get(client, "/api/v1/dashboard/top-stocks")
    assert resp.status_code == 200
    assert resp.json() == [{"symbol": "AAPL", "score": 0.9}]


@pytest.mark.asyncio
async def test_dashboard_agents_and_logs(client: httpx.AsyncClient) -> None:
    agents = await _get(client, "/api/v1/dashboard/agents")
    assert agents.status_code == 200
    logs = await _get(client, "/api/v1/dashboard/logs")
    assert logs.status_code == 200


@pytest.mark.asyncio
async def test_portfolio_orders_list(client: httpx.AsyncClient) -> None:
    resp = await _get(client, "/api/v1/portfolio/orders")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_portfolio_orders_submit(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/portfolio/orders",
        headers={"X-API-Key": API_KEY},
        json={"symbol": "AAPL", "side": "SELL", "quantity": 10, "order_type": "MARKET"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["symbol"] == "AAPL"
    assert body["status"] == "PENDING"


@pytest.mark.asyncio
async def test_portfolio_orders_submit_rejects_duplicate_buy(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post(
        "/api/v1/portfolio/orders",
        headers={"X-API-Key": API_KEY},
        json={"symbol": "AAPL", "side": "BUY", "quantity": 10, "order_type": "MARKET"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert "position already open" in body["detail"]


@pytest.mark.asyncio
async def test_system_mode_toggle(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/system/mode",
        headers={"X-API-Key": API_KEY},
        json={"mode": "backtest"},
    )
    assert resp.status_code == 200
    assert resp.json()["mode"] == "backtest"


@pytest.mark.asyncio
async def test_models_list(client: httpx.AsyncClient) -> None:
    resp = await _get(client, "/api/v1/models")
    assert resp.status_code == 200
    assert resp.json()[0]["name"] == "momentum"


@pytest.mark.asyncio
async def test_agents_list(client: httpx.AsyncClient) -> None:
    resp = await _get(client, "/api/v1/agents")
    assert resp.status_code == 200
    names = [a["name"] for a in resp.json()]
    assert "execution" in names


@pytest.mark.asyncio
async def test_backtest_submit_and_list(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/backtest",
        headers={"X-API-Key": API_KEY},
        json={"name": "t", "symbols": ["AAPL"], "start": "2026-01-01", "end": "2026-06-01"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    listed = await _get(client, "/api/v1/backtest")
    assert listed.status_code == 200


def _backtest_run(run_id: int, name: str = "t") -> BacktestRun:
    return BacktestRun(
        name=name,
        universe=["AAPL"],
        start=date(2026, 1, 1),
        end=date(2026, 6, 1),
        initial_capital=Money("100000"),
        interval=Interval.D1,
        strategy="ensemble",
        commission_bps=Decimal("1"),
        slippage_bps=Decimal("0"),
        run_id=run_id,
        status="completed",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        final_capital=Money("100000"),
    )


def _seeded_app(runs: list[BacktestRun]) -> FastAPI:
    container = FakeContainer()
    container.backtest_repo = FakeBacktestRepository(runs)
    application = create_app()
    application.dependency_overrides[get_container] = lambda: container
    return application


async def _post(
    client: httpx.AsyncClient, path: str, body: dict
) -> httpx.Response:
    return await client.post(path, headers={"X-API-Key": API_KEY}, json=body)


@pytest.mark.asyncio
async def test_backtest_submit_blank_symbols_422() -> None:
    transport = httpx.ASGITransport(app=_seeded_app([]))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        resp = await _post(
            client,
            "/api/v1/backtest",
            {"name": "t", "symbols": ["   "], "start": "2026-01-01", "end": "2026-06-01"},
        )
    assert resp.status_code == 422
    assert resp.json()["error"] == "validation_error"


@pytest.mark.asyncio
async def test_backtest_submit_invalid_interval_422() -> None:
    transport = httpx.ASGITransport(app=_seeded_app([]))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        resp = await _post(
            client,
            "/api/v1/backtest",
            {
                "name": "t",
                "symbols": ["AAPL"],
                "start": "2026-01-01",
                "end": "2026-06-01",
                "interval": "not-an-interval",
            },
        )
    assert resp.status_code == 422
    assert resp.json()["error"] == "validation_error"


@pytest.mark.asyncio
async def test_backtest_get_missing_run_404() -> None:
    transport = httpx.ASGITransport(app=_seeded_app([]))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        resp = await _get(client, "/api/v1/backtest/99")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "not_found"
    assert body["detail"] == "backtest run not found"


@pytest.mark.asyncio
async def test_backtest_compare_two_runs() -> None:
    runs = [_backtest_run(1), _backtest_run(2)]
    transport = httpx.ASGITransport(app=_seeded_app(runs))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        resp = await _post(client, "/api/v1/backtest/1/compare", {"other_run_id": 2})
    assert resp.status_code == 200
    assert [r["run_id"] for r in resp.json()] == [1, 2]


@pytest.mark.asyncio
async def test_backtest_compare_missing_run_404() -> None:
    transport = httpx.ASGITransport(app=_seeded_app([_backtest_run(1)]))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        resp = await _post(client, "/api/v1/backtest/1/compare", {"other_run_id": 99})
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


@pytest.mark.asyncio
async def test_backtest_list_filters_by_name() -> None:
    runs = [_backtest_run(1, name="alpha"), _backtest_run(2, name="beta")]
    transport = httpx.ASGITransport(app=_seeded_app(runs))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        resp = await _get(client, "/api/v1/backtest?name=beta")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["name"] == "beta"


@pytest.mark.asyncio
async def test_stocks_create_and_indicators_404(client: httpx.AsyncClient) -> None:
    created = await client.post(
        "/api/v1/stocks",
        headers={"X-API-Key": API_KEY},
        json={"symbol": "NFLX", "exchange": "XNAS", "name": "Netflix"},
    )
    assert created.status_code == 201
    assert created.json()["symbol"] == "NFLX"
    ind = await _get(client, "/api/v1/stocks/AAPL/indicators")
    assert ind.status_code == 404
