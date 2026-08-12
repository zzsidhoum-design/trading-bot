"""Unit tests for the multi-timeframe research engine (Phase 3).

Covers the pure function layer (quality, resampling, signals, alignment,
combination, simulation, metrics, regimes, cost sensitivity, walk-forward,
parameter sensitivity, ranking) plus the I/O engine with fake repositories.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pandas as pd

from qtrader.application.services.multitimeframe import (
    CombinationResult,
    MultitimeframeResearchEngine,
    ParamSensitivity,
    Recommendation,
    RegimeSlice,
    ResearchReport,
    ResearchSettings,
    SignalParams,
    SimParams,
    StudyMetrics,
    TimeframeCombo,
    TimeframeStudy,
    WalkForwardSummary,
    aggregate_metrics,
    align_latest,
    analyze_timeframe,
    best_roles,
    combine_signals,
    compute_study_metrics,
    cost_sweep,
    enumerate_combos,
    parameter_sensitivity,
    rank_recommendations,
    regime_labels_for,
    resample_bars,
    signal_series,
    simulate,
    timeframe_quality,
    walk_forward,
)
from qtrader.config.settings import Settings
from qtrader.domain.entities import Stock, TradingStatus, UniverseMembership, UniverseTier
from qtrader.domain.ports import PriceRepository, UniverseRepository
from qtrader.domain.value_objects import Interval, PriceBar
from tests.unit.fakes_phase7 import FakeStockRepository

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _bar(
    symbol: str,
    interval: Interval,
    ts: datetime,
    price: float,
    volume: str = "1000",
) -> PriceBar:
    return PriceBar(
        symbol=symbol,
        interval=interval,
        ts=ts,
        open=Decimal(str(price)),
        high=Decimal(str(price * 1.01)),
        low=Decimal(str(price * 0.99)),
        close=Decimal(str(price)),
        volume=Decimal(volume),
    )


def _m5_bars(symbol: str, n: int = 150, base: float = 100.0, step: float = 0.001) -> list[PriceBar]:
    """Business-day M5 bars on a 5-minute grid (09:30-15:55 UTC), 78/day.

    Starts 2026-01-15 so intraday trade dates fall inside the D1 regime window
    (the volatility axis needs 250 daily bars of history).
    """
    days = list(pd.bdate_range("2026-01-15", periods=(n // 78) + 2))
    bars: list[PriceBar] = []
    for i in range(n):
        day = days[i // 78]
        ts = datetime(day.year, day.month, day.day, 9, 30, tzinfo=UTC) + timedelta(
            minutes=5 * (i % 78)
        )
        bars.append(_bar(symbol, Interval.M5, ts, base * (1.0 + step * i)))
    return bars


def _d1_bars(symbol: str, n: int = 300, base: float = 100.0, step: float = 0.001) -> list[PriceBar]:
    days = list(pd.bdate_range("2025-01-02", periods=n))
    return [
        _bar(
            symbol,
            Interval.D1,
            datetime(day.year, day.month, day.day, tzinfo=UTC),
            base * (1.0 + step * i),
        )
        for i, day in enumerate(days)
    ]


def _h1_from_m5(m5: list[PriceBar]) -> list[PriceBar]:
    return resample_bars(m5, target=Interval.H1, source=Interval.M5)


class FakeUniverseRepository(UniverseRepository):
    def __init__(self, memberships: list[UniverseMembership] | None = None, error=None) -> None:
        self.memberships = memberships or []
        self.error = error

    async def list_memberships(self, status=None) -> list[UniverseMembership]:
        if self.error is not None:
            raise self.error
        return self.memberships

    async def get_membership(self, symbol: str) -> UniverseMembership | None:
        return next((m for m in self.memberships if m.symbol == symbol), None)

    async def upsert_membership(self, membership) -> UniverseMembership:
        return membership

    async def record_symbol_change(self, change):
        return change

    async def list_symbol_changes(self) -> list:
        return []


class FakeMultiPriceRepo(PriceRepository):
    """Serves pre-built bars per (symbol, interval)."""

    def __init__(self, by_symbol: dict[str, dict[Interval, list[PriceBar]]]) -> None:
        self._data = by_symbol
        self.calls: list[tuple[str, Interval]] = []

    async def upsert_bars(self, bars: list[PriceBar]) -> int:
        return len(bars)

    async def latest(self, symbol: str, interval) -> PriceBar | None:
        bars = self._data.get(symbol, {}).get(interval, [])
        return bars[-1] if bars else None

    async def history(self, symbol, interval, start=None, end=None, limit=500) -> list[PriceBar]:
        self.calls.append((symbol, interval))
        bars = self._data.get(symbol, {}).get(interval, [])
        return bars[:limit]


def _price_data(symbol: str = "AAPL") -> dict[str, dict[Interval, list[PriceBar]]]:
    m5 = _m5_bars(symbol)
    return {
        symbol: {
            Interval.D1: _d1_bars(symbol),
            Interval.H1: _h1_from_m5(m5),
            Interval.M5: m5,
        }
    }


def _engine(
    by_symbol=None, settings=None, stocks=None, universe=None
) -> MultitimeframeResearchEngine:
    return MultitimeframeResearchEngine(
        prices=FakeMultiPriceRepo(by_symbol or {}),
        stocks=stocks or FakeStockRepository(),
        universe=universe,
        settings=settings or ResearchSettings(),
    )


def _combo_bars() -> tuple[list[PriceBar], dict[Interval, list[PriceBar]]]:
    m5 = _m5_bars("AAPL", n=500, step=0.0008)
    m30 = resample_bars(m5, target=Interval.M30, source=Interval.M5)
    h1 = _h1_from_m5(m5)
    return m5, {Interval.M5: m5, Interval.M30: m30, Interval.H1: h1}


def _regime(n_trades: int, ret: float, win: float) -> RegimeSlice:
    return RegimeSlice(regime="x", n_trades=n_trades, total_return_pct=ret, win_rate=win)


def _metrics(**overrides) -> StudyMetrics:
    defaults = dict(
        total_return=0.1, expected_value=0.01, sharpe=1.0, sortino=1.0,
        max_drawdown=0.1, profit_factor=2.0, win_rate=0.5, n_trades=10,
        winning_trades=5, avg_holding_bars=3.0, stable_trades=5,
        costs_pct=0.5, gross_profit_pct=10.0, gross_loss_pct=5.0,
        signal_stability=0.5,
        regime={"market:bull": _regime(5, 2.0, 0.6), "vol:low": _regime(5, 1.0, 0.6)},
    )
    defaults.update(overrides)
    return StudyMetrics(**defaults)


def _combination_result(
    combo: TimeframeCombo, oos_sharpe: float, oos_return: float
) -> CombinationResult:
    return CombinationResult(
        combo=combo,
        metrics=_metrics(),
        per_symbol={},
        cost_sensitivity=[],
        walk_forward=WalkForwardSummary(
            n_folds=2, oos_sharpe_mean=oos_sharpe, oos_sharpe_positive_ratio=1.0,
            oos_return_mean=oos_return, oos_return_positive_ratio=1.0, oos_trades=4,
        ),
        param_sensitivity=ParamSensitivity(
            grid_size=3, sharpe_mean=0.5, sharpe_std=0.2,
            sharpe_positive_ratio=0.67, max_drawdown_mean=0.1,
            best_params=SignalParams(), best_sharpe=1.0,
        ),
    )


# --------------------------------------------------------------------------- #
# TimeframeCombo + enumeration
# --------------------------------------------------------------------------- #


def test_combo_key_and_validation() -> None:
    combo = TimeframeCombo(context=Interval.D1, setup=Interval.H1, entry=Interval.M5)
    assert combo.key == "1d->1h->5m"
    combo2 = TimeframeCombo(context=Interval.H4, setup=Interval.H1, entry=Interval.M5)
    assert combo2.key == "4h->1h->5m"


def test_combo_rejects_non_decreasing() -> None:
    try:
        TimeframeCombo(context=Interval.M5, setup=Interval.H1, entry=Interval.D1)
    except ValueError:
        return
    raise AssertionError("expected ValueError for non-decreasing combo")


def test_enumerate_combos_all_triples() -> None:
    combos = enumerate_combos([Interval.D1, Interval.H1, Interval.M5])
    assert [c.key for c in combos] == ["1d->1h->5m"]
    full = enumerate_combos([Interval.D1, Interval.H4, Interval.H1, Interval.M5])
    assert {c.key for c in full} == {"1d->4h->1h", "1d->4h->5m", "1d->1h->5m", "4h->1h->5m"}


# --------------------------------------------------------------------------- #
# Timeframe quality
# --------------------------------------------------------------------------- #


def test_timeframe_quality_empty() -> None:
    q = timeframe_quality([], Interval.D1)
    assert q.ok is False
    assert q.bars == 0
    assert q.coverage_pct == 0.0


def test_timeframe_quality_full_week_d1() -> None:
    bars = _d1_bars("AAPL", n=5)
    q = timeframe_quality(bars, Interval.D1)
    assert q.ok is True
    assert q.coverage_pct == 1.0
    assert q.max_gap_bars == 0.0


def test_timeframe_quality_gap_flagged() -> None:
    bars = [b for i, b in enumerate(_d1_bars("AAPL", n=5)) if i != 2]  # drop the middle bar
    q = timeframe_quality(bars, Interval.D1, min_coverage_pct=0.9)
    assert q.ok is False
    assert q.coverage_pct < 0.9
    assert q.max_gap_bars > 0.0


def test_timeframe_quality_m5_grid() -> None:
    q = timeframe_quality(_m5_bars("AAPL", n=78), Interval.M5)
    assert q.ok is True
    assert q.aligned_pct == 1.0


# --------------------------------------------------------------------------- #
# Resampling
# --------------------------------------------------------------------------- #


def test_resample_bars_h1_to_h4() -> None:
    h1 = [
        _bar("AAPL", Interval.H1, datetime(2026, 1, 2, 8, 0, tzinfo=UTC), 100.0),
        _bar("AAPL", Interval.H1, datetime(2026, 1, 2, 9, 0, tzinfo=UTC), 101.0),
        _bar("AAPL", Interval.H1, datetime(2026, 1, 2, 10, 0, tzinfo=UTC), 102.0),
        _bar("AAPL", Interval.H1, datetime(2026, 1, 2, 11, 0, tzinfo=UTC), 103.0),
    ]
    out = resample_bars(h1, target=Interval.H4, source=Interval.H1)
    assert len(out) == 1
    bar = out[0]
    assert bar.interval is Interval.H4
    assert bar.ts == datetime(2026, 1, 2, 8, 0, tzinfo=UTC)
    assert float(bar.open) == 100.0
    assert float(bar.close) == 103.0
    assert float(bar.high) == round(103.0 * 1.01, 2)  # max of input highs
    assert float(bar.low) == round(100.0 * 0.99, 2)  # min of input lows
    assert float(bar.volume) == 4000.0


def test_resample_rejects_finer_or_equal_target() -> None:
    h1 = _h1_from_m5(_m5_bars("AAPL", n=10))
    for target, source in (
        (Interval.H1, Interval.H1),
        (Interval.M5, Interval.M5),
        (Interval.H1, Interval.H4),
    ):
        try:
            resample_bars(h1, target=target, source=source)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {target} from {source}")


def test_resample_rejects_d1() -> None:
    try:
        resample_bars(_h1_from_m5(_m5_bars("AAPL", n=10)), target=Interval.D1, source=Interval.H1)
    except ValueError:
        return
    raise AssertionError("expected ValueError resampling to D1")


def test_resample_empty() -> None:
    assert resample_bars([], target=Interval.H4, source=Interval.H1) == []


# --------------------------------------------------------------------------- #
# Signal harness
# --------------------------------------------------------------------------- #


def test_signal_series_trend_length_and_warmup() -> None:
    bars = _m5_bars("AAPL", n=60, step=0.002)
    target = signal_series(bars, SignalParams(mode="trend", fast=5, slow=21))
    assert len(target) == len(bars)
    assert all(v in (-1, 0, 1) for v in target)
    assert target[:20] == [0] * 20  # EMA warm-up not tradeable


def test_signal_series_trend_uptrend_is_long() -> None:
    bars = _m5_bars("AAPL", n=60, step=0.002)
    target = signal_series(bars, SignalParams(mode="trend", fast=5, slow=21))
    assert 1 in target and -1 not in target


def test_signal_series_reversion_flat_is_flat() -> None:
    bars = [
        _bar(
            "AAPL",
            Interval.M5,
            datetime(2026, 1, 2, 9, 30, tzinfo=UTC) + timedelta(minutes=5 * i),
            100.0,
        )
        for i in range(30)
    ]
    target = signal_series(bars, SignalParams(mode="reversion"))
    assert target == [0] * len(bars)


# --------------------------------------------------------------------------- #
# Alignment + combination
# --------------------------------------------------------------------------- #


def test_align_latest_carry_forward_no_lookahead() -> None:
    slow = [
        (datetime(2026, 1, 2, 9, 0, tzinfo=UTC), 1),
        (datetime(2026, 1, 2, 10, 0, tzinfo=UTC), 0),
    ]
    entry = [
        _bar("AAPL", Interval.M5, datetime(2026, 1, 2, 9, 5, tzinfo=UTC), 100.0),
        _bar("AAPL", Interval.M5, datetime(2026, 1, 2, 9, 10, tzinfo=UTC), 100.0),
        _bar("AAPL", Interval.M5, datetime(2026, 1, 2, 10, 5, tzinfo=UTC), 100.0),
        _bar("AAPL", Interval.M5, datetime(2026, 1, 2, 10, 10, tzinfo=UTC), 100.0),
    ]
    assert align_latest(slow, entry) == [1, 1, 0, 0]


def test_align_latest_missing_history_is_flat() -> None:
    slow = [(datetime(2026, 1, 2, 10, 0, tzinfo=UTC), 1)]
    entry = [_bar("AAPL", Interval.M5, datetime(2026, 1, 2, 9, 5, tzinfo=UTC), 100.0)]
    assert align_latest(slow, entry) == [0]


def test_combine_signals_modes() -> None:
    entry = [1, 1, -1, 0]
    setup = [1, -1, -1, 0]
    context = [1, 1, -1, 0]
    # all: setup -1 opposes entry +1 at index 1 -> flat there.
    assert combine_signals(entry, setup, context, mode="all") == [1, 0, -1, 0]
    # majority: sums [3, 1, -3, 0] -> long/long/short/flat.
    assert combine_signals(entry, setup, context, mode="majority") == [1, 1, -1, 0]
    assert combine_signals(entry, setup, context, mode="entry") == entry


# --------------------------------------------------------------------------- #
# Simulator + metrics
# --------------------------------------------------------------------------- #


def test_simulate_single_trip_cost_model() -> None:
    bars = _m5_bars("AAPL", n=10, step=0.005)
    target = [0, 1, 1, 1, 1, 1, 1, 1, 1, 1]
    result = simulate(bars, target, params=SimParams(commission_bps=10.0, slippage_bps=50.0))
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_price > float(bars[2].open)  # slippage added at next-bar-open entry
    assert trade.exit_price < float(bars[-1].close)  # slippage deducted at liquidation
    assert trade.holding_bars >= 1
    assert result.n_flips == 1  # one entry; the final bar is a forced liquidation
    assert len(result.equity) >= len(bars)  # one extra mark at forced liquidation
    assert result.total_return > 0.0  # strong uptrend net of costs


def test_simulate_flat_target_no_trades() -> None:
    bars = _m5_bars("AAPL", n=10)
    result = simulate(bars, [0] * len(bars), params=SimParams())
    assert result.trades == []
    assert result.total_return == 0.0


def test_simulate_max_hold_time_stop() -> None:
    bars = _m5_bars("AAPL", n=10)
    target = [0, 1, 1, 1, 0, 0, 0, 0, 0, 0]
    result = simulate(bars, target, params=SimParams(max_hold_bars=2))
    assert len(result.trades) == 1
    assert result.trades[0].holding_bars == 2


def test_simulate_short_history() -> None:
    bars = _m5_bars("AAPL", n=1)
    result = simulate(bars, [0], params=SimParams())
    assert result.trades == []
    assert result.total_return == 0.0


def test_compute_study_metrics_counts() -> None:
    bars = _m5_bars("AAPL", n=10, step=0.005)
    target = [0, 1, 1, 1, 1, 1, 1, 1, 1, 1]
    metrics = compute_study_metrics(simulate(bars, target, params=SimParams()))
    assert metrics.n_trades == 1
    assert metrics.winning_trades == 1
    assert metrics.win_rate == 1.0
    assert metrics.expected_value == metrics.total_return  # single fully-invested trade
    assert metrics.avg_holding_bars > 0.0


def test_compute_study_metrics_no_trades() -> None:
    bars = _m5_bars("AAPL", n=10)
    metrics = compute_study_metrics(simulate(bars, [0] * 10, params=SimParams()))
    assert metrics.n_trades == 0
    assert metrics.signal_stability == 0.0


# --------------------------------------------------------------------------- #
# Regimes
# --------------------------------------------------------------------------- #


def test_regime_labels_for_daily_series() -> None:
    labels = regime_labels_for(_d1_bars("AAPL", n=300))
    assert len(labels) > 0
    assert all(label.market in ("bull", "bear", "sideways") for label in labels)
    assert all(label.volatility in ("low", "high") for label in labels)
    dates = [label.date for label in labels]
    assert dates == sorted(dates)


def test_regime_attribution_populates_slices() -> None:
    d1 = _d1_bars("AAPL", n=300)
    m5 = _m5_bars("AAPL", n=150)
    regimes = regime_labels_for(d1)
    target = signal_series(m5, SignalParams())
    result = simulate(m5, target, params=SimParams())
    metrics = compute_study_metrics(result, regime=regimes)
    assert metrics.n_trades > 0
    assert metrics.regime  # at least one bucket attributed


# --------------------------------------------------------------------------- #
# Cost sensitivity + aggregation
# --------------------------------------------------------------------------- #


def test_cost_sweep_monotonic_in_commission() -> None:
    bars = _m5_bars("AAPL", n=60, step=0.001)
    target = signal_series(bars, SignalParams(mode="trend", fast=5, slow=21))
    points = cost_sweep(bars, target, params=SimParams())
    assert len(points) > 0
    baseline_slip = points[0].slippage_bps
    by_comm = sorted(
        {
            p.commission_bps: p.total_return
            for p in points
            if p.slippage_bps == baseline_slip
        }.items()
    )
    values = [v for _, v in by_comm]
    assert values == sorted(values, reverse=True)  # non-increasing with cost


def test_aggregate_metrics_pools_trades() -> None:
    a = _metrics(
        total_return=1.0, expected_value=0.1, sharpe=1.0, win_rate=0.5, n_trades=10,
        winning_trades=5, avg_holding_bars=3.0, stable_trades=5, costs_pct=1.0,
        gross_profit_pct=20.0, gross_loss_pct=10.0, signal_stability=0.5, regime={},
    )
    b = _metrics(
        total_return=2.0, expected_value=0.3, sharpe=0.5, win_rate=1.0, n_trades=10,
        winning_trades=10, avg_holding_bars=4.0, stable_trades=8, costs_pct=2.0,
        gross_profit_pct=30.0, gross_loss_pct=10.0, signal_stability=0.8, regime={},
    )
    agg = aggregate_metrics({"a": a, "b": b})
    assert agg.n_trades == 20
    assert agg.winning_trades == 15
    assert agg.win_rate == 0.75
    assert agg.total_return == 1.5
    assert agg.avg_holding_bars == 3.5


def test_aggregate_metrics_empty() -> None:
    agg = aggregate_metrics({})
    assert agg.n_trades == 0


# --------------------------------------------------------------------------- #
# Single-timeframe analysis
# --------------------------------------------------------------------------- #


def test_analyze_timeframe_shape() -> None:
    bars = _m5_bars("AAPL", n=150, step=0.001)
    study = analyze_timeframe(
        bars,
        Interval.M5,
        params=SignalParams(),
        sim_params=SimParams(),
    )
    assert isinstance(study, TimeframeStudy)
    assert study.interval is Interval.M5
    assert study.n_trades >= 0
    assert len(study.cost_sensitivity) > 0
    assert study.noise_mean_abs_ret >= 0.0


# --------------------------------------------------------------------------- #
# Walk-forward + parameter sensitivity
# --------------------------------------------------------------------------- #


def test_walk_forward_produces_folds() -> None:
    entry, by_interval = _combo_bars()
    combo = TimeframeCombo(context=Interval.H1, setup=Interval.M30, entry=Interval.M5)
    grid = (
        SignalParams(mode="trend", fast=5, slow=21),
        SignalParams(mode="trend", fast=9, slow=21),
        SignalParams(mode="trend", fast=15, slow=50),
    )
    summary = walk_forward(
        entry,
        by_interval,
        combo,
        grid,
        sim_params=SimParams(),
        n_folds=3,
        min_train_bars=50,
        mode="all",
    )
    assert summary.n_folds >= 1
    assert len(summary.folds) == summary.n_folds
    assert all(f.chosen_params in grid for f in summary.folds)
    assert 0.0 <= summary.oos_sharpe_positive_ratio <= 1.0
    assert summary.oos_trades >= 0


def test_walk_forward_insufficient_data() -> None:
    m5 = _m5_bars("AAPL", n=30)
    h1 = _h1_from_m5(m5)
    m30 = resample_bars(m5, target=Interval.M30, source=Interval.M5)
    combo = TimeframeCombo(context=Interval.H1, setup=Interval.M30, entry=Interval.M5)
    summary = walk_forward(
        m5,
        {Interval.M5: m5, Interval.M30: m30, Interval.H1: h1},
        combo,
        (SignalParams(),),
        sim_params=SimParams(),
        n_folds=3,
        min_train_bars=50,
    )
    assert summary.n_folds == 0
    assert summary.oos_trades == 0


def test_parameter_sensitivity_grid() -> None:
    entry, by_interval = _combo_bars()
    combo = TimeframeCombo(context=Interval.H1, setup=Interval.M30, entry=Interval.M5)
    grid = (
        SignalParams(mode="trend", fast=5, slow=21),
        SignalParams(mode="trend", fast=9, slow=21),
        SignalParams(mode="trend", fast=15, slow=50),
    )
    ps = parameter_sensitivity(entry, by_interval, combo, grid, sim_params=SimParams(), mode="all")
    assert ps.grid_size == 3
    assert ps.best_params in grid
    assert 0.0 <= ps.sharpe_positive_ratio <= 1.0


# --------------------------------------------------------------------------- #
# Ranking + best roles
# --------------------------------------------------------------------------- #


def test_rank_recommendations_oos_first() -> None:
    good = _combination_result(TimeframeCombo(Interval.D1, Interval.H1, Interval.M5), 2.0, 5.0)
    bad = _combination_result(TimeframeCombo(Interval.H4, Interval.H1, Interval.M15), 0.1, 0.2)
    recs: list[Recommendation] = rank_recommendations([bad, good])
    assert recs[0].combo.key == "1d->1h->5m"
    assert recs[0].score > recs[1].score
    assert recs[0].robustness == "HIGH"


def test_rank_recommendations_low_when_oos_negative() -> None:
    bad = _combination_result(TimeframeCombo(Interval.H4, Interval.H1, Interval.M15), -1.0, -2.0)
    recs = rank_recommendations([bad])
    assert recs[0].robustness == "LOW"


def test_best_roles_weighted() -> None:
    good = _combination_result(TimeframeCombo(Interval.D1, Interval.H1, Interval.M5), 2.0, 5.0)
    recs = rank_recommendations([good])
    assert best_roles(recs) == (Interval.D1, Interval.H1, Interval.M5)


def test_best_roles_empty_falls_back() -> None:
    assert best_roles([]) == (Interval.D1, Interval.H1, Interval.M5)


# --------------------------------------------------------------------------- #
# Engine integration
# --------------------------------------------------------------------------- #


async def test_engine_no_symbols_returns_early_report() -> None:
    engine = _engine(stocks=FakeStockRepository(stocks=[]))
    report = await engine.run()
    assert isinstance(report, ResearchReport)
    assert report.symbols == ()
    assert "no symbols resolved" in report.limitations
    assert report.timeframe_studies == []
    assert report.best_context is Interval.D1


async def test_engine_full_report_with_derived_h4() -> None:
    settings = ResearchSettings(
        intervals=(Interval.D1, Interval.H4, Interval.H1, Interval.M5),
        lookback_days=365,
        n_folds=3,
        min_train_bars=30,
        min_coverage_pct=0.5,
        combination_mode="all",
        max_symbols=10,
    )
    engine = _engine(
        by_symbol=_price_data("AAPL"),
        settings=settings,
        stocks=FakeStockRepository(
            [Stock(symbol="AAPL", exchange="XNAS", name="Apple", is_active=True)]
        ),
    )
    report = await engine.run()
    assert report.symbols == ("AAPL",)
    assert Interval.H4 in {s.interval for s in report.timeframe_studies}
    assert len(report.combinations) == 4
    assert len(report.recommendations) == len(report.combinations)
    assert report.best_context in {Interval.D1, Interval.H4}
    assert report.best_setup in {Interval.H4, Interval.H1}
    assert report.best_entry in {Interval.H1, Interval.M5}


async def test_engine_resolves_symbols_from_universe() -> None:
    membership = UniverseMembership(
        symbol="MSFT",
        status=TradingStatus.ACTIVE,
        tier=UniverseTier.C,
        added_at=date(2025, 1, 1),
        removed_at=None,
    )
    engine = _engine(
        by_symbol=_price_data("MSFT"),
        universe=FakeUniverseRepository([membership]),
        stocks=FakeStockRepository(
            [Stock(symbol="IGNORED", exchange="XNAS", name="x", is_active=True)]
        ),
    )
    report = await engine.run()
    assert report.symbols == ("MSFT",)


async def test_engine_falls_back_to_stocks_when_universe_fails() -> None:
    engine = _engine(
        by_symbol=_price_data("AAPL"),
        universe=FakeUniverseRepository(error=RuntimeError("boom")),
        stocks=FakeStockRepository(
            [Stock(symbol="AAPL", exchange="XNAS", name="Apple", is_active=True)]
        ),
    )
    report = await engine.run()
    assert report.symbols == ("AAPL",)


async def test_engine_limitations_flag_missing_interval() -> None:
    data = _price_data("AAPL")
    data["AAPL"] = {Interval.D1: data["AAPL"][Interval.D1]}  # drop intraday
    settings = ResearchSettings(
        intervals=(Interval.D1, Interval.H1, Interval.M5),
        min_coverage_pct=0.5,
        max_symbols=10,
    )
    engine = _engine(
        by_symbol=data,
        settings=settings,
        stocks=FakeStockRepository(
            [Stock(symbol="AAPL", exchange="XNAS", name="Apple", is_active=True)]
        ),
    )
    report = await engine.run()
    assert any("5m" in limitation for limitation in report.limitations)


async def test_engine_respects_symbol_override_and_max_symbols() -> None:
    data = {}
    for symbol in ("AAPL", "MSFT", "TSLA"):
        data.update(_price_data(symbol))
    settings = ResearchSettings(
        intervals=(Interval.D1, Interval.H1, Interval.M5),
        max_symbols=2,
        min_coverage_pct=0.5,
    )
    engine = _engine(
        by_symbol=data,
        settings=settings,
        stocks=FakeStockRepository(
            [
                Stock(symbol=s, exchange="XNAS", name=s, is_active=True)
                for s in ("AAPL", "MSFT", "TSLA")
            ]
        ),
    )
    report = await engine.run(symbols=["TSLA", "AAPL", "MSFT"])
    assert len(report.symbols) == 2  # capped by max_symbols


# --------------------------------------------------------------------------- #
# Settings mixin
# --------------------------------------------------------------------------- #


def test_research_settings_mixin() -> None:
    s = Settings(
        _env_file=None,
        research_lookback_days=100,
        research_signal_fast=5,
        research_n_folds=6,
        research_intervals="1d,1h,5m",
    )
    rs = s.research_settings
    assert rs.lookback_days == 100
    assert rs.signal.fast == 5
    assert rs.n_folds == 6
    assert tuple(iv.value for iv in rs.intervals) == ("1d", "1h", "5m")
    assert rs.sim.commission_bps == s.research_commission_bps
