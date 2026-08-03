"""Unit tests for the FastAPI interface (fake repositories, no infra)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI

from qtrader.config.settings import Settings
from qtrader.domain.entities import Portfolio, Stock
from qtrader.domain.events import PriceUpdated
from qtrader.domain.ports import (
    EventRepository,
    PortfolioRepository,
    PriceRepository,
    StockRepository,
    SystemLogRepository,
)
from qtrader.domain.value_objects import Interval, Money, PriceBar, TradingMode
from qtrader.interfaces.api.app import create_app
from qtrader.interfaces.api.dependencies import get_container
from tests.unit.fakes_phase6 import FakeSystemLogRepository

API_KEY = "test-key"


class FakeStockRepository(StockRepository):
    def __init__(self) -> None:
        self._stocks = [
            Stock(symbol="AAPL", exchange="XNAS", name="Apple", sector="TECH", stock_id=1),
            Stock(symbol="MSFT", exchange="XNAS", name="Microsoft", sector="TECH", stock_id=2),
        ]

    async def upsert(self, stock) -> None:
        return None

    async def get_by_symbol(self, symbol: str, exchange: str | None = None) -> Stock | None:
        return next((s for s in self._stocks if s.symbol == symbol), None)

    async def list_active(self) -> list[Stock]:
        return self._stocks

    async def search(self, query, sector, limit, offset) -> list[Stock]:
        matches = []
        for s in self._stocks:
            if query is not None and query.lower() not in s.symbol.lower() and not (
                s.name and query.lower() in s.name.lower()
            ):
                continue
            matches.append(s)
        return matches[:limit]


class FakePriceRepository(PriceRepository):
    def __init__(self) -> None:
        self._bar = PriceBar(
            symbol="AAPL",
            interval=Interval.M5,
            ts=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
            open=Decimal("179.5"),
            high=Decimal("181"),
            low=Decimal("179"),
            close=Decimal("180.5"),
            volume=Decimal("1250000"),
        )

    async def upsert_bars(self, bars) -> int:
        return len(bars)

    async def latest(self, symbol: str, interval) -> PriceBar | None:
        return self._bar if symbol == "AAPL" else None

    async def history(self, symbol, interval, start=None, end=None, limit=500) -> list[PriceBar]:
        return [self._bar] if symbol == "AAPL" else []


class FakePortfolioRepository(PortfolioRepository):
    async def create(self, portfolio) -> Portfolio:
        return portfolio

    async def get(self, portfolio_id: int) -> Portfolio | None:
        return Portfolio(
            name="default",
            initial_capital=Money("100000"),
            current_cash=Money("65000"),
            mode=TradingMode.BACKTEST,
            portfolio_id=1,
        )

    async def first(self) -> Portfolio | None:
        return await self.get(1)

    async def save(self, portfolio) -> Portfolio:
        return portfolio


class FakeEventRepository(EventRepository):
    def __init__(self) -> None:
        self._events: list[PriceUpdated] = []

    async def record(self, event) -> None:
        self._events.append(event)  # type: ignore[arg-type]

    async def list_after(self, event_uuid, event_type, limit) -> list:
        return list(self._events)[:limit]

    async def count_by_type(self, limit=1000) -> dict[str, int]:
        return {e.type_name: self._events.count(e) for e in set(self._events)}


class FakeContainer:
    def __init__(self) -> None:
        from qtrader.application.services.portfolio_service import PortfolioService

        self._settings = Settings(_env_file=None, api_key=API_KEY)
        self._services: dict[type, object] = {
            Settings: self._settings,
            StockRepository: FakeStockRepository(),
            PriceRepository: FakePriceRepository(),
            PortfolioRepository: FakePortfolioRepository(),
            EventRepository: FakeEventRepository(),
            SystemLogRepository: FakeSystemLogRepository(),
            PortfolioService: PortfolioService(FakePortfolioRepository()),
        }

    def resolve(self, service_type: type) -> object:
        return self._services[service_type]

    async def database_healthy(self) -> bool:
        return True

    async def cache_healthy(self) -> bool:
        return True

    async def worker_healthy(self) -> bool:
        return True

    def circuit_breakers(self) -> list[dict[str, object]]:
        return [
            {
                "name": "yahoo",
                "state": "closed",
                "consecutive_failures": 0,
                "reset_timeout_seconds": 30.0,
            }
        ]

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

    async_client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    try:
        yield async_client
    finally:
        await async_client.aclose()


async def _get(client: httpx.AsyncClient, path: str) -> httpx.Response:
    return await client.get(path, headers={"X-API-Key": API_KEY})


@pytest.mark.asyncio
async def test_health_requires_api_key(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"] == "http_error"
    assert body["detail"] == "invalid API key"


@pytest.mark.asyncio
async def test_wrong_api_key_is_rejected(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/health", headers={"X-API-Key": "wrong-key"})
    assert resp.status_code == 401
    assert resp.json()["error"] == "http_error"


@pytest.mark.asyncio
async def test_change_me_default_rejects_even_with_key() -> None:
    container = FakeContainer()
    container._settings = Settings(_env_file=None, api_key="change-me")
    application = create_app()
    application.dependency_overrides[get_container] = lambda: container
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/health", headers={"X-API-Key": "whatever"})
    assert resp.status_code == 401
    assert resp.json()["error"] == "http_error"


@pytest.mark.asyncio
async def test_invalid_query_param_returns_422_envelope(client: httpx.AsyncClient) -> None:
    resp = await _get(client, "/api/v1/stocks/AAPL/history?limit=abc")
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "validation_error"
    assert isinstance(body["detail"], list)


@pytest.mark.asyncio
async def test_invalid_agent_interval_returns_422_envelope(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post(
        "/api/v1/agents/data/run",
        headers={"X-API-Key": API_KEY},
        json={"symbol": "AAPL", "interval": "not-an-interval"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "validation_error"
    assert "not-an-interval" in body["detail"]


@pytest.mark.asyncio
async def test_unhandled_exception_returns_structured_500() -> None:
    """Catch-all handler: structured 500, no internals leaked."""
    from fastapi import FastAPI

    from qtrader.interfaces.api.app import _unhandled_error_response

    app = FastAPI()

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("kaboom")

    app.add_exception_handler(Exception, _unhandled_error_response)

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        resp = await client.get("/boom")

    assert resp.status_code == 500
    assert resp.json() == {"error": "internal_error", "detail": "internal server error"}


@pytest.mark.asyncio
async def test_health_reports_ok(client: httpx.AsyncClient) -> None:
    resp = await _get(client, "/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["cache"] == "ok"
    assert body["worker"] == "ok"


@pytest.mark.asyncio
async def test_search_stocks(client: httpx.AsyncClient) -> None:
    resp = await _get(client, "/api/v1/stocks?q=aapl")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_latest_price(client: httpx.AsyncClient) -> None:
    resp = await _get(client, "/api/v1/stocks/AAPL/price")
    assert resp.status_code == 200
    body = resp.json()
    assert body["close"] == "180.5"
    assert body["interval"] == "5m"


@pytest.mark.asyncio
async def test_latest_price_unknown_symbol_404(client: httpx.AsyncClient) -> None:
    resp = await _get(client, "/api/v1/stocks/ZZZZ/price")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_domain_error_body_has_typed_code(client: httpx.AsyncClient) -> None:
    resp = await _get(client, "/api/v1/stocks/ZZZZ/price")
    body = resp.json()
    assert body["error"] == "no_price_data"
    assert body["detail"] == "no price data for symbol 'ZZZZ'"


@pytest.mark.asyncio
async def test_price_history(client: httpx.AsyncClient) -> None:
    resp = await _get(client, "/api/v1/stocks/AAPL/history?interval=5m")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["high"] == "181"


@pytest.mark.asyncio
async def test_portfolio_summary(client: httpx.AsyncClient) -> None:
    resp = await _get(client, "/api/v1/portfolio")
    assert resp.status_code == 200
    body = resp.json()
    assert body["current_cash"] == "65000.000000"
    assert body["mode"] == "backtest"


@pytest.mark.asyncio
async def test_system_metrics_snapshot(client: httpx.AsyncClient) -> None:
    resp = await _get(client, "/api/v1/system/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "backtest"
    assert body["database"] == "ok"
    assert body["cache"] == "ok"
    assert body["worker"] == "ok"
    assert body["events_by_type"] == {}
    assert body["uptime_seconds"] >= 0
    assert body["circuit_breakers"][0]["name"] == "yahoo"


@pytest.mark.asyncio
async def test_system_logs_lists_recent_entries() -> None:
    from qtrader.domain.entities import SystemLog
    from qtrader.interfaces.api.dependencies import get_system_log_repository

    logs = FakeSystemLogRepository()
    await logs.record(SystemLog(level="INFO", component="backtest", message="completed"))
    await logs.record(SystemLog(level="WARN", component="gate", message="blocked"))

    application = create_app()
    application.dependency_overrides[get_container] = lambda: FakeContainer()
    application.dependency_overrides[get_system_log_repository] = lambda: logs

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/system/logs", headers={"X-API-Key": API_KEY})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert body[0]["level"] == "WARN"
    assert body[0]["message"] == "blocked"
    assert body[0]["component"] == "gate"


@pytest.mark.asyncio
async def test_system_logs_filters_by_level_and_component() -> None:
    from qtrader.domain.entities import SystemLog
    from qtrader.interfaces.api.dependencies import get_system_log_repository

    logs = FakeSystemLogRepository()
    await logs.record(SystemLog(level="INFO", component="backtest", message="completed"))

    application = create_app()
    application.dependency_overrides[get_container] = lambda: FakeContainer()
    application.dependency_overrides[get_system_log_repository] = lambda: logs

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/system/logs?level=warn&component=gate",
            headers={"X-API-Key": API_KEY},
        )

    assert resp.status_code == 200
    assert resp.json() == []
