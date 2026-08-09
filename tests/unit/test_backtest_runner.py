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
    model: object | None = None,
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
        model=model,
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
    price, outcome = BacktestRunner._intrabar_exit(pos, both, BacktestParams())
    assert (price, outcome) == (Decimal("97"), "stop")

    hit_tp = bar(
        "X",
        datetime(2026, 1, 3, tzinfo=UTC),
        open="100",
        high="107",
        low="99",
        close="100",
    )
    price, outcome = BacktestRunner._intrabar_exit(pos, hit_tp, BacktestParams())
    assert (price, outcome) == (Decimal("106"), "take_profit")

    calm = bar("X", datetime(2026, 1, 4, tzinfo=UTC), open="100", high="102", low="99", close="100")
    assert BacktestRunner._intrabar_exit(pos, calm, BacktestParams()) == (None, "")


def test_intrabar_exit_honors_trailing_stop() -> None:
    pos = _OpenPosition(
        symbol="X",
        quantity=10,
        entry_price=Decimal("100"),
        stop_loss=Decimal("90"),
        take_profit=Decimal("115"),
        entry_ts=datetime(2026, 1, 1, tzinfo=UTC),
        fees=Decimal(0),
        peak=Decimal("105"),
    )
    params = BacktestParams(trailing_stop_pct=0.05)
    dipped = bar(
        "X", datetime(2026, 1, 2, tzinfo=UTC), open="102", high="106", low="99", close="100"
    )
    price, outcome = BacktestRunner._intrabar_exit(pos, dipped, params)
    assert (price, outcome) == (Decimal("99.75"), "trailing")
    calm = bar(
        "X", datetime(2026, 1, 3, tzinfo=UTC), open="102", high="103", low="100.5", close="101"
    )
    assert BacktestRunner._intrabar_exit(pos, calm, params) == (None, "")


def test_open_position_uses_parameterized_bracket() -> None:
    runner = _runner({"X": _trend_bars("X")})[0]
    from qtrader.application.services.backtest import BacktestFill

    fill = BacktestFill(
        symbol="X",
        side=TradeSide.BUY,
        quantity=10,
        price=Decimal("100"),
        commission=Decimal("0.1"),
        ts=datetime(2026, 1, 2, tzinfo=UTC),
    )
    positions: dict[str, _OpenPosition] = {}
    params = BacktestParams(stop_loss_pct=0.02, take_profit_pct=0.04)
    cash = runner._open_position(fill, Decimal("100000"), positions, params, bar_index=5)
    assert cash == Decimal("100000") - Decimal("1000") - Decimal("0.1")
    assert positions["X"].stop_loss == Decimal("98")
    assert positions["X"].take_profit == Decimal("104")
    assert positions["X"].entry_bar_index == 5


def test_queue_buy_sizes_for_bracket_risk() -> None:
    from qtrader.application.services.backtest import BacktestBroker, _Bars, _SimContext
    from qtrader.domain.value_objects import Interval

    bars = _trend_bars("X", days=40)
    runner = _runner({"X": bars})[0]
    broker = BacktestBroker()
    entry_bar = bars[39]
    snapshot = IndicatorEngine().compute(bars, "X", Interval.D1)
    ctx = _SimContext(
        sectors=None,
        trades_today=0,
        daily_pnl_pct=0.0,
        cooldown_remaining_minutes=0.0,
    )
    runner._queue_buy(
        broker,
        entry_bar,
        snapshot,
        BacktestParams(stop_loss_pct=0.03),
        Decimal("100000"),
        {},
        {"X": _Bars()},
        ctx,
    )
    assert len(broker.pending) == 1
    qty = broker.pending[0].quantity
    expected = int((Decimal("100000") * Decimal("0.01")) / (entry_bar.close * Decimal("0.03")))
    assert qty == expected  # ~1/3 of equity, so a stop hit loses ~1% of equity.


def test_queue_buy_rejects_when_projected_exposure_over_limit() -> None:
    from qtrader.application.services.backtest import (
        BacktestBroker,
        _Bars,
        _OpenPosition,
        _SimContext,
    )
    from qtrader.domain.value_objects import Interval

    bars = _trend_bars("X", days=40)
    runner = _runner({"X": bars})[0]
    broker = BacktestBroker()
    entry_bar = bars[39]
    snapshot = IndicatorEngine().compute(bars, "X", Interval.D1)
    pos = _OpenPosition(
        symbol="Y",
        quantity=700,
        entry_price=Decimal("100"),
        stop_loss=Decimal("97"),
        take_profit=Decimal("106"),
        entry_ts=entry_bar.ts,
        fees=Decimal(0),
    )
    cursors = {"Y": _Bars()}
    cursors["Y"].last_close = Decimal("100")
    ctx = _SimContext(
        sectors=None,
        trades_today=0,
        daily_pnl_pct=0.0,
        cooldown_remaining_minutes=0.0,
    )
    runner._queue_buy(
        broker,
        entry_bar,
        snapshot,
        BacktestParams(),
        Decimal("30000"),
        {"Y": pos},
        cursors,
        ctx,
    )
    assert broker.pending == []  # 70% exposure + ~33% candidate > 80% cap


def test_queue_buy_rejects_when_trades_per_day_limit_reached() -> None:
    from qtrader.application.services.backtest import BacktestBroker, _Bars, _SimContext
    from qtrader.domain.value_objects import Interval

    bars = _trend_bars("X", days=40)
    runner = _runner({"X": bars})[0]
    broker = BacktestBroker()
    entry_bar = bars[39]
    snapshot = IndicatorEngine().compute(bars, "X", Interval.D1)
    ctx = _SimContext(
        sectors=None,
        trades_today=10,
        daily_pnl_pct=0.0,
        cooldown_remaining_minutes=0.0,
    )
    runner._queue_buy(
        broker,
        entry_bar,
        snapshot,
        BacktestParams(),
        Decimal("100000"),
        {},
        {"X": _Bars()},
        ctx,
    )
    assert broker.pending == []  # default max_trades_per_day is 10


def test_queue_buy_rejects_when_sector_limit_exceeded() -> None:
    from qtrader.application.services.backtest import (
        BacktestBroker,
        _Bars,
        _OpenPosition,
        _SimContext,
    )
    from qtrader.domain.value_objects import Interval

    bars = _trend_bars("X", days=40)
    runner = _runner({"X": bars})[0]
    broker = BacktestBroker()
    entry_bar = bars[39]
    snapshot = IndicatorEngine().compute(bars, "X", Interval.D1)
    pos_y = _OpenPosition(
        symbol="Y",
        quantity=310,
        entry_price=Decimal("100"),
        stop_loss=Decimal("97"),
        take_profit=Decimal("106"),
        entry_ts=entry_bar.ts,
        fees=Decimal(0),
    )
    pos_z = _OpenPosition(
        symbol="Z",
        quantity=100,
        entry_price=Decimal("100"),
        stop_loss=Decimal("97"),
        take_profit=Decimal("106"),
        entry_ts=entry_bar.ts,
        fees=Decimal(0),
    )
    cursors = {"Y": _Bars(), "Z": _Bars()}
    cursors["Y"].last_close = Decimal("100")
    cursors["Z"].last_close = Decimal("100")
    ctx = _SimContext(
        sectors={"Y": "Tech", "Z": "Tech"},
        trades_today=0,
        daily_pnl_pct=0.0,
        cooldown_remaining_minutes=0.0,
    )
    runner._queue_buy(
        broker,
        entry_bar,
        snapshot,
        BacktestParams(),
        Decimal("59000"),
        {"Y": pos_y, "Z": pos_z},
        cursors,
        ctx,
    )
    assert broker.pending == []  # $41k/$100k Tech > 40% per-sector cap


def test_max_hold_bars_exits_at_close() -> None:
    symbol = "HOLD"
    start = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
    flat_bars = []
    for i in range(40):
        flat_bars.append(
            bar(
                symbol,
                start + timedelta(days=i),
                open="100",
                high="100.5",
                low="99.5",
                close="100",
                volume="1000000",
            )
        )
    bars = {symbol: flat_bars}
    model_outputs = {symbol: {flat_bars[33].ts: 0.9}}
    from qtrader.domain.entities import BacktestRun
    from qtrader.domain.value_objects import Money

    runner = _runner(bars, model=object())[0]
    run = BacktestRun(
        name="hold",
        universe=[symbol],
        start=date(2026, 1, 1),
        end=date(2026, 4, 30),
        initial_capital=Money(Decimal("100000")),
    )
    result = runner._simulate(
        run,
        bars,
        Decimal("100000"),
        BacktestParams(commission_bps=1.0, max_hold_bars=5),
        model_outputs=model_outputs,
    )
    timed = [t for t in result.trades if t.outcome == "time"]
    assert timed, [t.outcome for t in result.trades]
    assert timed[0].exit_time > timed[0].entry_time


def test_model_outputs_drive_decisions_without_a_model_instance() -> None:
    # The strategy framework emits prob_up series without a fitted model; the
    # engine must honor model_outputs even when ``model`` is None.
    from qtrader.domain.entities import BacktestRun
    from qtrader.domain.value_objects import Interval, Money

    symbol = "MOD"
    flat_bars = [
        bar(
            symbol,
            datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=i),
            open="100",
            high="100.5",
            low="99.5",
            close="100",
            volume="1000000",
        )
        for i in range(60)
    ]
    bars = {symbol: flat_bars}
    series = {
        symbol: IndicatorEngine().compute_series(flat_bars, symbol, Interval.D1)
    }
    model_outputs = {
        symbol: {
            flat_bars[33].ts: 0.9,
            flat_bars[40].ts: 0.1,
        }
    }
    runner, _, _ = _runner(bars, model=None)
    run = BacktestRun(
        name="probs-only",
        universe=[symbol],
        start=date(2026, 1, 1),
        end=date(2026, 3, 31),
        initial_capital=Money(Decimal("100000")),
    )
    result = runner._simulate(
        run,
        bars,
        Decimal("100000"),
        BacktestParams(commission_bps=1.0),
        model_outputs=model_outputs,
        series=series,
    )
    assert result.summary.trades_count == 1
    assert result.trades[0].entry_time == flat_bars[34].ts  # next-bar open fill
    assert result.trades[0].exit_time >= flat_bars[41].ts


def test_exit_fills_pay_commission() -> None:
    # Bracket/time/end exits previously filled with commission=0, understating
    # costs. A time exit must now pay the same bps rate as the entry.
    from qtrader.domain.entities import BacktestRun
    from qtrader.domain.value_objects import Interval, Money

    symbol = "FEE"
    flat_bars = [
        bar(
            symbol,
            datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=i),
            open="100",
            high="100.5",
            low="99.5",
            close="100",
            volume="1000000",
        )
        for i in range(60)
    ]
    bars = {symbol: flat_bars}
    series = {
        symbol: IndicatorEngine().compute_series(flat_bars, symbol, Interval.D1)
    }
    model_outputs = {symbol: {flat_bars[33].ts: 0.9}}
    runner, _, _ = _runner(bars, commission_bps=10.0, model=None)
    run = BacktestRun(
        name="exit-fees",
        universe=[symbol],
        start=date(2026, 1, 1),
        end=date(2026, 3, 31),
        initial_capital=Money(Decimal("100000")),
    )
    result = runner._simulate(
        run,
        bars,
        Decimal("100000"),
        BacktestParams(commission_bps=10.0, max_hold_bars=3),
        model_outputs=model_outputs,
        series=series,
    )
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.outcome == "time"
    qty = trade.quantity
    # Entry fill at open 100 + time exit at close 100, 10bp each side.
    expected = Decimal("100") * qty * (Decimal("10") / Decimal("10000")) * 2
    assert trade.fees == expected
    assert trade.fees > 0


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


def test_signal_engine_model_path_uses_series_cache_not_compute() -> None:
    """The model branch must consume the precomputed series cache and never
    call IndicatorEngine.compute per bar (that was the O(n^2) hot path)."""
    from qtrader.application.services.backtest import _SignalEngine
    from qtrader.application.services.indicators import IndicatorEngine, IndicatorSnapshot
    from qtrader.application.services.prediction_model import LogisticModel
    from qtrader.domain.value_objects import Decision, Interval

    symbol = "CACHE"
    bars = _trend_bars(symbol, days=80)

    class _ExplodingEngine(IndicatorEngine):
        def compute(self, bars, symbol, interval) -> IndicatorSnapshot:
            raise AssertionError("model path must not recompute when a series cache exists")

    real_engine = IndicatorEngine()
    series = real_engine.compute_series(bars, symbol, Interval.D1)
    last_snapshot = series[len(bars) - 1]
    model = LogisticModel(feature_names=["ret_5"], coef=[0.0], intercept=5.0)
    model_outputs = {
        symbol: {
            bars[i].ts: (0.55 if i % 2 == 1 else 0.5) for i in range(len(bars))
        }
    }
    engine = _SignalEngine(
        _ExplodingEngine(),
        warmup_bars=10,
        model=model,
        model_outputs=model_outputs,
        series={symbol: series},
    )
    decision, snapshot = engine.evaluate(symbol, bars, Interval.D1)
    assert decision is not Decision.HOLD
    assert snapshot is last_snapshot
