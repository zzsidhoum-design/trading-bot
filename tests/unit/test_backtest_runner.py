"""Unit tests for the Phase 6 backtest engine (broker, signals, replay loop)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from qtrader.application.services.backtest import (
    BacktestBroker,
    BacktestOrder,
    BacktestParams,
    BacktestRunner,
    _OpenPosition,
)
from qtrader.application.services.indicators import IndicatorEngine
from qtrader.application.services.risk_calculator import RiskCalculator, RiskPolicy
from qtrader.domain.value_objects import TradeSide, TradingMode
from tests.unit.fakes_phase6 import (
    FakeBacktestRepository,
    FakePerformanceRepository,
    FakePriceRepository,
    FakeSystemLogRepository,
    bar,
)


def _trend_bars(symbol: str, days: int = 120, start_price: float = 100.0) -> list:
    """Deterministic series: flat -> dip -> rally -> decline (crosses EMAs)."""
    start = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
    bars: list = []
    price = float(start_price)
    for i in range(days):
        if i < 40:
            step = 0.0
        elif i < 44:
            step = -2.0
        elif i < 74:
            step = 1.0
        else:
            step = -1.5
        price += step
        ts = start + timedelta(days=i)
        bars.append(
            bar(
                symbol,
                ts,
                open=f"{price:.2f}",
                high=f"{price + 1.5:.2f}",
                low=f"{price - 1.5:.2f}",
                close=f"{price:.2f}",
                volume="1000000",
            )
        )
    return bars


def _runner(
    bars: dict[str, list],
    commission_bps: float = 1.0,
    slippage_bps: float = 0.0,
    logs: FakeSystemLogRepository | None = None,
) -> tuple[BacktestRunner, FakeBacktestRepository, FakePerformanceRepository]:
    backtests = FakeBacktestRepository()
    performance = FakePerformanceRepository()
    runner = BacktestRunner(
        prices=FakePriceRepository(bars),
        backtests=backtests,
        performance=performance,
        risk_calculator=RiskCalculator(RiskPolicy(risk_per_trade_pct=0.01)),
        indicator_engine=IndicatorEngine(),
        logs=logs,
    )
    return runner, backtests, performance


def test_broker_fills_at_open_with_slippage_and_commission() -> None:
    broker = BacktestBroker(commission_bps=10.0, slippage_bps=5.0)
    broker.queue(
        BacktestOrder(
            symbol="X", side=TradeSide.BUY, quantity=100, signal_ts=datetime.now(UTC)
        )
    )
    fill_bar = bar(
        "X",
        datetime(2026, 1, 2, tzinfo=UTC),
        open="100",
        high="105",
        low="99",
        close="104",
    )
    fills = broker.fills_at(fill_bar)
    assert len(fills) == 1
    fill = fills[0]
    assert fill.price == Decimal("100.05")  # 100 * (1 + 5bp)
    assert fill.commission == Decimal("10.00")  # notional 10005 * 10bp
    assert broker.pending == []


def test_broker_defers_orders_for_other_symbols() -> None:
    broker = BacktestBroker()
    broker.queue(
        BacktestOrder(
            symbol="Y", side=TradeSide.BUY, quantity=1, signal_ts=datetime.now(UTC)
        )
    )
    fill_bar = bar(
        "X",
        datetime(2026, 1, 2, tzinfo=UTC),
        open="100",
        high="100",
        low="100",
        close="100",
    )
    assert broker.fills_at(fill_bar) == []
    assert len(broker.pending) == 1


def test_broker_sell_fills_below_open() -> None:
    broker = BacktestBroker(slippage_bps=5.0)
    broker.queue(
        BacktestOrder(
            symbol="X", side=TradeSide.SELL, quantity=10, signal_ts=datetime.now(UTC)
        )
    )
    fill_bar = bar(
        "X",
        datetime(2026, 1, 2, tzinfo=UTC),
        open="100",
        high="100",
        low="100",
        close="100",
    )
    fill = broker.fills_at(fill_bar)[0]
    assert fill.price == Decimal("99.95")


def test_intrabar_exit_prefers_stop_on_ambiguity() -> None:
    pos = _OpenPosition(
        symbol="X",
        quantity=10,
        entry_price=Decimal("100"),
        stop_loss=Decimal("97"),
        take_profit=Decimal("106"),
        entry_ts=datetime(2026, 1, 1, tzinfo=UTC),
        fees=Decimal(0),
    )
    both = bar(
        "X",
        datetime(2026, 1, 2, tzinfo=UTC),
        open="100",
        high="108",
        low="96",
        close="100",
    )
    price, outcome = BacktestRunner._intrabar_exit(pos, both)
    assert (price, outcome) == (Decimal("97"), "stop")

    hit_tp = bar(
        "X",
        datetime(2026, 1, 3, tzinfo=UTC),
        open="100",
        high="107",
        low="99",
        close="100",
    )
    price, outcome = BacktestRunner._intrabar_exit(pos, hit_tp)
    assert (price, outcome) == (Decimal("106"), "take_profit")

    calm = bar("X", datetime(2026, 1, 4, tzinfo=UTC), open="100", high="102", low="99", close="100")
    assert BacktestRunner._intrabar_exit(pos, calm) == (None, "")


@pytest.mark.asyncio
async def test_full_replay_produces_trades_and_persists() -> None:
    symbol = "SIG"
    logs = FakeSystemLogRepository()
    runner, backtests, performance = _runner(
        {symbol: _trend_bars(symbol)}, commission_bps=1.0, logs=logs
    )
    result = await runner.run(
        name="unit-run",
        symbols=[symbol],
        start=date(2026, 1, 1),
        end=date(2026, 4, 30),
        initial_capital=Decimal("100000"),
        params=BacktestParams(commission_bps=1.0),
    )
    assert result.run.status == "completed"
    assert result.run.run_id is not None
    assert result.run.final_capital is not None
    assert result.summary.trades_count == len(result.trades)
    assert len(result.trades) >= 1
    assert len(result.equity_curve) > 0
    assert result.summary.final_equity is not None

    persisted = backtests.runs[-1]
    assert persisted.status == "completed"
    assert persisted.final_capital is not None
    assert persisted.metrics is not None
    assert persisted.metrics.trades_count >= 1

    # Performance row upserted with a stable key.
    assert len(performance.summaries) == 1
    assert performance.summaries[0].mode is TradingMode.BACKTEST

    # System logs recorded start + completion.
    assert any("run started" in e.message for e in logs.entries)
    assert any("run completed" in e.message for e in logs.entries)

    # No look-ahead: first trade cannot be at the very first bar.
    first_trade = result.trades[0]
    assert first_trade.entry_time > datetime(2026, 1, 1, tzinfo=UTC)


def _simulate(bars: dict[str, list]) -> tuple[list, BacktestRunner]:
    """Run the synchronous simulation core directly and return its trades."""
    from qtrader.domain.entities import BacktestRun
    from qtrader.domain.value_objects import Money

    runner = _runner(bars)[0]
    run = BacktestRun(
        name="sim",
        universe=list(bars),
        start=date(2026, 1, 1),
        end=date(2026, 4, 30),
        initial_capital=Money(Decimal("100000")),
    )
    result = runner._simulate(run, bars, Decimal("100000"), BacktestParams(commission_bps=1.0))
    return result.trades, runner


@pytest.mark.asyncio
async def test_replay_is_deterministic() -> None:
    symbol = "DET"
    bars = {symbol: _trend_bars(symbol)}
    first, _ = _simulate(bars)
    second, _ = _simulate(bars)
    assert [(t.symbol, t.entry_price, t.exit_price, t.outcome) for t in first] == [
        (t.symbol, t.entry_price, t.exit_price, t.outcome) for t in second
    ]


@pytest.mark.asyncio
async def test_empty_history_still_completes() -> None:
    runner, backtests, performance = _runner({"GHOST": []})
    result = await runner.run(
        name="empty",
        symbols=["GHOST"],
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
        initial_capital=Decimal("100000"),
    )
    assert result.run.status == "completed"
    assert result.summary.trades_count == 0
    assert result.run.final_capital is not None
    assert result.run.final_capital.amount == Decimal("100000")


@pytest.mark.asyncio
async def test_run_failure_marks_run_failed_and_relogs() -> None:
    class _ExplodingPrices(FakePriceRepository):
        async def history(self, symbol, interval, start=None, end=None, limit=500):
            raise RuntimeError("upstream down")

    logs = FakeSystemLogRepository()
    runner, backtests, _ = _runner({"AAPL": []}, logs=logs)
    runner._prices = _ExplodingPrices({})

    with pytest.raises(RuntimeError, match="upstream down"):
        await runner.run(
            name="boom",
            symbols=["AAPL"],
            start=date(2026, 1, 1),
            end=date(2026, 6, 1),
            initial_capital=Decimal("100000"),
        )

    assert backtests.runs[-1].status == "failed"
    assert any(e.level == "ERROR" and e.message == "run failed" for e in logs.entries)
