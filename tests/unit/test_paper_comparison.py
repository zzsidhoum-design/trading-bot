"""Unit tests for the Phase 7 paper-vs-research comparator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from qtrader.application.paper.comparison import (
    ComparisonInput,
    PaperVsResearchComparator,
)
from qtrader.application.paper.models import (
    PaperOrderRecord,
    PaperOrderStatus,
)
from qtrader.domain.entities import PerformanceSummary, Signal, Trade
from qtrader.domain.value_objects import Interval, SignalType, TradeSide, TradingMode


def _record(
    key: str,
    *,
    strategy: str = "momentum",
    status: PaperOrderStatus = PaperOrderStatus.FILLED,
    fill: str = "101.00",
    requested: str = "100.00",
) -> PaperOrderRecord:
    return PaperOrderRecord(
        key=key,
        decision_ref=key,
        asset="AAPL",
        side="BUY",
        quantity=Decimal("10"),
        order_type="MARKET",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        requested_price=Decimal(requested),
        fill_price=Decimal(fill),
        slippage=Decimal(fill) - Decimal(requested),
        status=status,
        strategy=strategy,
        context={"agent": "technical"},
    )


def _trade(pnl: str, *, when: datetime, strategy: str = "momentum") -> Trade:
    return Trade(
        portfolio_id=1,
        stock_id=1,
        symbol="AAPL",
        strategy=strategy,
        side=TradeSide.BUY,
        quantity=Decimal("10"),
        entry_price=Decimal("100"),
        exit_price=Decimal("100"),
        pnl=Decimal(pnl),
        entry_time=when,
        exit_time=when,
    )


def _summary(
    total_return: str, *, drawdown: str = "-0.05", trades: int = 100
) -> PerformanceSummary:
    return PerformanceSummary(
        strategy="momentum",
        mode=TradingMode.BACKTEST,
        period_start=datetime(2026, 1, 1, tzinfo=UTC).date(),
        period_end=datetime(2026, 6, 30, tzinfo=UTC).date(),
        total_return=Decimal(total_return),
        max_drawdown=Decimal(drawdown),
        trades_count=trades,
    )


def _signal(agent: str) -> Signal:
    return Signal(
        symbol="AAPL",
        agent=agent,
        signal_type=SignalType.BUY,
        score=Decimal("0.5"),
        interval=Interval.D1,
    )


def test_comparison_reports_returns_slippage_fill_and_drawdown() -> None:
    records = (_record("a"), _record("b"), _record("c", status=PaperOrderStatus.REJECTED))
    base = datetime(2026, 1, 1, tzinfo=UTC)
    trades = (_trade("500", when=base), _trade("-200", when=base + timedelta(days=1)))
    report = PaperVsResearchComparator().compare(
        ComparisonInput(
            paper_records=records,
            paper_trades=trades,
            research_summary=_summary("0.30"),
            research_signals=(_signal("technical"), _signal("news")),
            research_fill_rate=0.95,
            initial_capital=Decimal("100000"),
        )
    )
    rows = {r.dimension: r for r in report.rows}

    total = rows["total_return"]
    assert total.paper_value is not None
    assert total.paper_value == 0.003
    assert total.research_value == 0.3
    assert total.divergence is not None and total.divergence > 0

    fill = rows["fill_rate"]
    assert fill.paper_value == 2 / 3
    assert fill.research_value == 0.95

    drawdown = rows["max_drawdown"]
    assert drawdown.paper_value is not None and drawdown.paper_value <= 0

    slippage = rows["avg_slippage_bps"]
    assert slippage.paper_value is not None and slippage.paper_value > 0


def test_comparison_reports_trade_frequency_and_agent_signals() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    trades = tuple(
        _trade("100", when=base + timedelta(days=i)) for i in range(30)
    )
    report = PaperVsResearchComparator().compare(
        ComparisonInput(
            paper_records=(_record("a"),),
            paper_trades=trades,
            research_summary=_summary("0.10", trades=250),
            research_signals=(_signal("technical"), _signal("news")),
        )
    )
    rows = {r.dimension: r for r in report.rows}
    frequency = rows["trade_frequency_per_day"]
    assert frequency.paper_value is not None and frequency.paper_value > 1.0
    assert frequency.research_value == 250 / 252

    signals = rows["agent_signal_frequency"]
    assert signals.paper_value == 1
    assert signals.research_value == 2


def test_comparison_strategy_selection_divergence() -> None:
    records = (
        _record("a", strategy="momentum"),
        _record("b", strategy="meanrev"),
    )
    report = PaperVsResearchComparator().compare(
        ComparisonInput(
            paper_records=records,
            paper_trades=(),
            research_summary=_summary("0.10"),
            research_signals=(),
        )
    )
    rows = {r.dimension: r for r in report.rows}
    selection = rows["strategy_selection"]
    assert selection.paper_value == 2
    assert selection.research_value == 1
    assert selection.divergence == 1


def test_comparison_empty_inputs_are_safe() -> None:
    report = PaperVsResearchComparator().compare(ComparisonInput())
    rows = {r.dimension: r for r in report.rows}
    assert rows["total_return"].paper_value is None
    assert rows["fill_rate"].paper_value is None
    assert rows["max_drawdown"].paper_value is None
