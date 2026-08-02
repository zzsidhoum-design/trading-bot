"""Unit tests for ManualOrder — the gated manual write path (Phase 7)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from qtrader.application.agents.execution import ExecutionAgent
from qtrader.application.services.portfolio_service import PortfolioService
from qtrader.application.services.risk_calculator import RiskCalculator, RiskPolicy
from qtrader.application.use_cases.manual_order import (
    ManualOrder,
    ManualOrderRequest,
    OrderRejectedError,
)
from qtrader.config.settings import Settings
from qtrader.domain.entities import Portfolio, Stock
from qtrader.domain.value_objects import OrderStatus, TradingMode
from tests.unit.fakes_phase7 import (
    FakeBrokerGateway,
    FakeEventBus,
    FakeIndicatorRepository,
    FakeOrderRepository,
    FakePortfolioRepository,
    FakePositionRepository,
    FakePriceRepository,
    FakeStockRepository,
    FakeTradeRepository,
    bar,
    money,
)


def _settings(mode: TradingMode = TradingMode.PAPER) -> Settings:
    return Settings(
        _env_file=None,
        api_key="test-key",
        qtrader_mode=mode,
        live_enabled=False,
    )


def _build(
    *,
    mode: TradingMode = TradingMode.PAPER,
    permissive: bool = True,
) -> tuple[ManualOrder, FakeOrderRepository, FakeBrokerGateway, FakePortfolioRepository]:
    portfolio = Portfolio(
        name="default",
        currency="USD",
        initial_capital=money("100000"),
        current_cash=money("100000"),
        mode=TradingMode.BACKTEST,
        portfolio_id=1,
    )
    portfolios = FakePortfolioRepository(portfolio)
    stocks = FakeStockRepository(
        [Stock(symbol="AAPL", exchange="XNAS", name="Apple", stock_id=1)]
    )
    prices = FakePriceRepository(
        bar("AAPL", datetime(2026, 8, 1, tzinfo=UTC), "99", "101", "98", "100")
    )
    indicators = FakeIndicatorRepository()
    positions = FakePositionRepository()
    orders = FakeOrderRepository()
    trades = FakeTradeRepository()
    bus = FakeEventBus()
    broker = FakeBrokerGateway(filled=True)
    policy = RiskPolicy() if permissive else RiskPolicy(max_positions=0)
    execution = ExecutionAgent(
        broker=broker,
        portfolio_service=PortfolioService(portfolios, default_id=1),
        portfolios=portfolios,
        positions=positions,
        orders=orders,
        stocks=stocks,
        trades=trades,
        bus=bus,
        gate=None,
    )
    manual = ManualOrder(
        portfolios=PortfolioService(portfolios, default_id=1),
        stocks=stocks,
        prices=prices,
        indicators=indicators,
        positions=positions,
        orders=orders,
        risk_calculator=RiskCalculator(policy),
        execution=execution,
        settings=_settings(mode),
    )
    return manual, orders, broker, portfolios


async def test_submit_buy_creates_and_executes_order() -> None:
    manual, orders, broker, _ = _build()
    order = await manual.submit(
        ManualOrderRequest(symbol="AAPL", side="BUY", quantity=10)
    )
    assert order.order_id is not None
    assert order.side.value == "BUY"
    assert order.mode == TradingMode.PAPER
    assert order.status == OrderStatus.PENDING
    assert order.idempotency_key.startswith("manual:")
    assert len(broker.submitted) == 1
    assert broker.submitted[0].symbol == "AAPL"
    assert orders.orders[0].status == OrderStatus.FILLED


async def test_submit_rejects_when_risk_gate_blocks() -> None:
    manual, orders, broker, _ = _build(permissive=False)
    with pytest.raises(OrderRejectedError):
        await manual.submit(
            ManualOrderRequest(symbol="AAPL", side="BUY", quantity=10)
        )
    assert orders.orders == []
    assert broker.submitted == []


async def test_submit_rejects_unknown_symbol_via_price_missing() -> None:
    manual, _, _, _ = _build()
    with pytest.raises(ValueError):
        await manual.submit(
            ManualOrderRequest(symbol="ZZZZ", side="BUY", quantity=10)
        )


async def test_submit_live_requires_live_enabled() -> None:
    manual, _, _, _ = _build(mode=TradingMode.LIVE)
    with pytest.raises(ValueError):
        await manual.submit(
            ManualOrderRequest(symbol="AAPL", side="BUY", quantity=10)
        )
