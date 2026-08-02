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
)
from qtrader.domain.value_objects import Interval, Money, PriceBar, TradingMode
from qtrader.interfaces.api.app import create_app
from qtrader.interfaces.api.dependencies import get_container

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

    async def save(self, portfolio) -> Portfolio:
        return portfolio


class FakeEventRepository(EventRepository):
    def __init__(self) -> None:
        self._events: list[PriceUpdated] = []

    async def record(self, event) -> None:
        self._events.append(event)  # type: ignore[arg-type]

    async def list_after(self, event_uuid, event_type, limit) -> list:
        return list(self._events)[:limit]


class FakeContainer:
    def __init__(self) -> None:
        self._settings = Settings(_env_file=None, api_key=API_KEY)
        self._services: dict[type, object] = {
            Settings: self._settings,
            StockRepository: FakeStockRepository(),
            PriceRepository: FakePriceRepository(),
            PortfolioRepository: FakePortfolioRepository(),
            EventRepository: FakeEventRepository(),
        }

    def resolve(self, service_type: type) -> object:
        return self._services[service_type]

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
def client(app: FastAPI):
    from httpx import ASGITransport

    return httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    )


async def _get(client: httpx.AsyncClient, path: str) -> httpx.Response:
    return await client.get(path, headers={"X-API-Key": API_KEY})


@pytest.mark.asyncio
async def test_health_requires_api_key(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_health_reports_ok(client: httpx.AsyncClient) -> None:
    resp = await _get(client, "/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["cache"] == "ok"


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
