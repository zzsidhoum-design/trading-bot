"""Unit tests for DashboardService (Phase 7 read-side aggregation)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from qtrader.domain.entities import Portfolio, Trade
from qtrader.domain.value_objects import Money, TradeSide, TradingMode
from tests.unit.fakes_phase7 import (
    bar,
    make_dashboard_service,
    make_position,
)


def _portfolio(cash: str) -> Portfolio:
    return Portfolio(
        name="default",
        currency="USD",
        initial_capital=Money(Decimal("100000")),
        current_cash=Money(Decimal(cash)),
        mode=TradingMode.BACKTEST,
        portfolio_id=1,
    )


def _trade(pnl: str, exit_time: datetime) -> Trade:
    return Trade(
        portfolio_id=1,
        stock_id=1,
        symbol="AAPL",
        strategy="ensemble",
        side=TradeSide.SELL,
        quantity=Decimal("10"),
        entry_price=Decimal("100"),
        exit_price=Decimal(str(Decimal("100") + Decimal(pnl) / 10)),
        pnl=Decimal(pnl),
        pnl_pct=Decimal("0.02"),
        fees=Decimal("0"),
        entry_time=exit_time,
        exit_time=exit_time,
        outcome="closed",
        mode=TradingMode.BACKTEST,
    )


async def test_summary_aggregates_open_positions_and_trades() -> None:
    ts = datetime(2026, 8, 1, tzinfo=UTC)
    service = make_dashboard_service(
        positions=[make_position(symbol="AAPL", quantity=10, avg="100")],
        trades=[_trade("150", ts)],
        latest_bar=bar("AAPL", ts, "109", "111", "108", "110"),
    )
    summary = await service.summary()
    assert summary is not None
    assert summary.cash == Decimal("65000")
    assert summary.unrealized_pnl == Decimal("100")
    assert summary.equity == Decimal("66100")
    assert summary.open_positions == 1
    assert summary.total_trades == 1
    assert round(summary.exposure_pct, 4) == round(1100 / 66100, 4)


async def test_summary_returns_none_when_portfolio_missing() -> None:
    from tests.unit.fakes_phase7 import FakePortfolioRepository

    service = make_dashboard_service(portfolio_repo=FakePortfolioRepository(None))
    assert await service.summary() is None


async def test_equity_curve_accumulates_closed_pnl() -> None:
    t1 = datetime(2026, 8, 1, tzinfo=UTC)
    t2 = datetime(2026, 8, 2, tzinfo=UTC)
    service = make_dashboard_service(
        trades=[_trade("150", t1), _trade("-50", t2)],
        portfolio=_portfolio(cash="100100"),
    )
    points = await service.equity_curve(limit=10)
    assert [p.equity for p in points] == [
        Decimal("100150"),
        Decimal("100100"),
        Decimal("100100"),
    ]


async def test_equity_curve_marks_to_market_open_positions() -> None:
    ts = datetime(2026, 8, 2, tzinfo=UTC)
    service = make_dashboard_service(
        positions=[make_position(symbol="AAPL", quantity=10, avg="100")],
        latest_bar=bar("AAPL", ts, "109", "111", "108", "110"),
        portfolio=_portfolio(cash="65000"),
    )
    points = await service.equity_curve(limit=10)
    assert points[-1].equity == Decimal("66100")


async def test_trades_include_open_positions() -> None:
    ts = datetime(2026, 8, 2, tzinfo=UTC)
    service = make_dashboard_service(
        positions=[make_position(symbol="AAPL", quantity=10, avg="100")],
        trades=[_trade("150", ts)],
        latest_bar=bar("AAPL", ts, "109", "111", "108", "110"),
    )
    records = await service.trades()
    assert len(records) == 2
    open_entry = next(t for t in records if t.outcome == "open")
    assert open_entry.symbol == "AAPL"
    assert open_entry.pnl == Decimal("100")
    assert open_entry.exit_price == Decimal("110")


async def test_performance_includes_live_summary() -> None:
    t1 = datetime(2026, 8, 1, tzinfo=UTC)
    service = make_dashboard_service(
        trades=[_trade("150", t1)],
        positions=[make_position(symbol="AAPL", quantity=10, avg="100")],
        latest_bar=bar("AAPL", t1, "109", "111", "108", "110"),
        portfolio=_portfolio(cash="100100"),
    )
    summaries = await service.performance()
    assert len(summaries) == 1
    assert summaries[0].trades_count == 1
    assert summaries[0].mode == TradingMode.BACKTEST
    assert summaries[0].final_equity == Decimal("101200")


async def test_positions_quote_current_price_and_pnl() -> None:
    ts = datetime(2026, 8, 1, tzinfo=UTC)
    service = make_dashboard_service(
        positions=[make_position(symbol="AAPL", quantity=10, avg="100")],
        latest_bar=bar("AAPL", ts, "109", "111", "108", "110"),
    )
    quotes = await service.positions()
    assert len(quotes) == 1
    assert quotes[0].current_price == Decimal("110")
    assert quotes[0].unrealized_pnl == Decimal("100")


async def test_allocation_computes_weighted_slices() -> None:
    ts = datetime(2026, 8, 1, tzinfo=UTC)
    service = make_dashboard_service(
        positions=[make_position(symbol="AAPL", quantity=10, avg="100")],
        latest_bar=bar("AAPL", ts, "109", "111", "108", "110"),
        stocks=[],
    )
    slices = await service.allocation()
    assert len(slices) == 1
    assert slices[0].symbol == "AAPL"
    assert slices[0].market_value == Decimal("1100")
    assert 0.0 < slices[0].weight_pct < 1.0


async def test_top_stocks_reads_cache_zset() -> None:
    service = make_dashboard_service(
        zsets={"scan:top:overall": [("AAPL", 0.9), ("MSFT", 0.8)]},
    )
    ranking = await service.top_stocks("overall", limit=5)
    assert ranking == [("AAPL", 0.9), ("MSFT", 0.8)]


async def test_top_stocks_uses_per_metric_key() -> None:
    service = make_dashboard_service(
        zsets={"scan:top:volatility": [("MSFT", 0.85)]},
    )
    ranking = await service.top_stocks("volatility", limit=5)
    assert ranking == [("MSFT", 0.85)]
