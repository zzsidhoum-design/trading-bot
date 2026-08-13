"""Phase 3 — automated strategy validation and edge detection tests."""

from __future__ import annotations

import json
import random
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest

from qtrader.application.research.strategy import (
    Condition,
    EntryRule,
    ExitRule,
    InMemoryStrategyRegistry,
    Operator,
    SearchLimits,
    StrategySpec,
    StrategyStatus,
)
from qtrader.application.research.strategy.engine import MetricGate, ResearchRequest
from qtrader.application.research.validation import (
    BenchmarkInputs,
    BenchmarkSeriesResult,
    CostLevelResult,
    DataWindow,
    FinalStatus,
    InitialCandidateFilter,
    InitialFilterLimits,
    InMemoryValidationRepository,
    MultipleTestingReport,
    ParameterRobustnessChecker,
    ParameterRobustnessLimits,
    RandomBaselineResult,
    RankingWeights,
    StageResult,
    StrategyRanker,
    StrategyValidationEngine,
    TimeframeResult,
    ValidationPlan,
    ValidationRecord,
    ValidationStage,
    ValidationWalkForwardValidator,
    WalkForwardResult,
    build_benchmark_report,
    buy_and_hold_curve,
    compute_edge_stats,
    cost_sensitivity_report,
    cross_asset_report,
    decode_record,
    deflated_sharpe,
    encode_record,
    expected_max_sharpe,
    multi_timeframe_report,
    multiple_testing_report,
    parameter_variants,
    random_permutation_result,
    regime_report_from_buckets,
    slice_bars,
    slice_bars_by_symbol,
    sma200_filter_spec,
    split_windows,
)
from qtrader.application.services.risk_calculator import RiskCalculator, RiskPolicy
from qtrader.domain.entities import PerformanceSummary
from qtrader.domain.ports import PriceRepository
from qtrader.domain.value_objects import Interval, PriceBar, TradingMode
from tests.unit.fakes_phase7 import FakePerformanceRepository

_UTC = UTC
_EPS = Decimal("0.01")


# --------------------------------------------------------------------------- #
# Shared fixtures
# --------------------------------------------------------------------------- #


def _make_bars(symbol: str, start: date, n: int, seed: int, drift: float) -> list[PriceBar]:
    rng = random.Random(seed)
    price = 100.0
    bars: list[PriceBar] = []
    ts = datetime.combine(start, time(9, 30), tzinfo=_UTC)
    for _ in range(n):
        ret = drift + rng.gauss(0.0, 0.012)
        open_ = price
        close = price * (1 + ret)
        high = max(open_, close) * (1 + abs(rng.gauss(0.0, 0.004)))
        low = min(open_, close) * (1 - abs(rng.gauss(0.0, 0.004)))
        volume = 1_000_000 * (0.8 + rng.random() * 0.4)
        bars.append(
            PriceBar(
                symbol=symbol,
                interval=Interval.D1,
                ts=ts,
                open=_round(open_),
                high=_round(high),
                low=_round(low),
                close=_round(close),
                volume=_round(volume),
            )
        )
        price = close
        ts += timedelta(days=1)
    return bars


def _round(value: float) -> Decimal:
    return Decimal(str(round(value, 2)))


class _BarsPriceRepository(PriceRepository):
    def __init__(self, bars_by_symbol: dict[str, list[PriceBar]]) -> None:
        self._bars = {s: sorted(b, key=lambda b: b.ts) for s, b in bars_by_symbol.items()}

    async def upsert_bars(self, bars: list[PriceBar]) -> int:
        return len(bars)

    async def latest(self, symbol: str, interval: Interval) -> PriceBar | None:
        bars = self._bars.get(symbol, [])
        return bars[-1] if bars else None

    async def history(
        self,
        symbol: str,
        interval: Interval,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> list[PriceBar]:
        bars = [
            b
            for b in self._bars.get(symbol, [])
            if (start is None or b.ts >= start) and (end is None or b.ts <= end)
        ]
        return bars[:limit]


def _default_spec(**overrides: object) -> StrategySpec:
    spec = StrategySpec(
        id="strat-0001",
        name="auto-0001",
        entry=EntryRule(
            conditions=(Condition(feature="close", op=Operator.GT, ref_feature="ema_21"),)
        ),
        exit=ExitRule(conditions=(Condition(feature="rsi", op=Operator.GT, value=70.0),)),
        features=("close", "ema_21", "rsi"),
        complexity=3,
        description="trend entry, rsi exit",
    )
    return replace(spec, **overrides)


def _summary(
    strategy: str = "strat-0001",
    *,
    trades: int = 40,
    sharpe: float = 1.2,
    total_return: Decimal = Decimal("0.35"),
    profit_factor: Decimal = Decimal("1.4"),
    win_rate: Decimal = Decimal("0.55"),
    max_drawdown: Decimal = Decimal("-0.18"),
    **overrides: object,
) -> PerformanceSummary:
    base = PerformanceSummary(
        strategy=strategy,
        mode=TradingMode.BACKTEST,
        period_start=date(2020, 1, 1),
        period_end=date(2020, 12, 31),
        total_return=total_return,
        cagr=Decimal("0.20"),
        sharpe=Decimal(str(sharpe)),
        profit_factor=profit_factor,
        win_rate=win_rate,
        max_drawdown=max_drawdown,
        expectancy=Decimal("0.005"),
        avg_win=Decimal("0.02"),
        avg_loss=Decimal("-0.01"),
        turnover=Decimal("3.0"),
        total_costs=Decimal("500"),
        trades_count=trades,
        final_equity=Decimal("135000"),
    )
    return replace(base, **overrides)


def _record(strategy_id: str = "strat-0001", **overrides: object) -> ValidationRecord:
    record = ValidationRecord(
        spec=_default_spec(id=strategy_id),
        stage=ValidationStage.VALIDATED,
        final_status=FinalStatus.VALIDATED,
        universe=("AAA",),
        dataset_version="v1",
        windows=("dev", "validation", "oos"),
        dev_result=StageResult(label="dev", summary=_summary(strategy_id)),
        oos_result=StageResult(label="oos", summary=_summary(strategy_id)),
    )
    return replace(record, **overrides)


def _request(bars_by_symbol) -> ResearchRequest:
    start = min(b.ts.date() for bars in bars_by_symbol.values() for b in bars)
    end = max(b.ts.date() for bars in bars_by_symbol.values() for b in bars)
    return ResearchRequest(
        symbols=tuple(bars_by_symbol),
        start=start,
        end=end,
        interval=Interval.D1,
        dataset_version="test-v1",
    )


def _dataset() -> dict[str, list[PriceBar]]:
    return {
        "AAA": _make_bars("AAA", date(2020, 1, 1), 400, seed=1, drift=0.001),
        "BBB": _make_bars("BBB", date(2020, 1, 1), 400, seed=2, drift=-0.0005),
    }


def _engine_dataset() -> dict[str, list[PriceBar]]:
    return {
        "AAA": _make_bars("AAA", date(2020, 1, 1), 320, seed=1, drift=0.001),
    }


def _lenient_plan(**overrides: object) -> ValidationPlan:
    gate = MetricGate(
        min_sharpe=-999.0,
        min_profit_factor=0.0,
        min_win_rate=0.0,
        min_trades=0,
        max_drawdown=-1.0,
    )
    params: dict[str, object] = dict(
        limits=SearchLimits(max_strategies=2, computational_budget=2),
        initial_filter=InitialFilterLimits(
            min_trades=0,
            max_cagr=999.0,
            max_total_return=999.0,
            max_drawdown=-1.0,
            max_turnover=999.0,
            max_complexity=99,
            min_distinct_indicators=1,
            max_instability=999.0,
            jitter_runs=0,
        ),
        dev_gate=gate,
        wf_gate=gate,
        oos_gate=gate,
        warmup_bars=0,
        folds=2,
        lookback_bars=10,
        horizon_bars=5,
        param_robustness_runs=1,
        cost_levels=((0.0, 0.0),),
        random_benchmark_seeds=4,
        benchmark_gate=False,
    )
    params.update(overrides)
    return ValidationPlan(**params)  # type: ignore[arg-type]


def _engine(
    bars_by_symbol, *, plan: ValidationPlan | None = None
) -> tuple[
    StrategyValidationEngine,
    _BarsPriceRepository,
    FakePerformanceRepository,
    InMemoryStrategyRegistry,
    InMemoryValidationRepository,
]:
    prices = _BarsPriceRepository(bars_by_symbol)
    performance = FakePerformanceRepository()
    registry = InMemoryStrategyRegistry()
    repo = InMemoryValidationRepository()
    param_checker = ParameterRobustnessChecker(
        ParameterRobustnessLimits(max_instability=999.0, min_positive_fraction=0.0)
    )
    engine = StrategyValidationEngine(
        prices=prices,
        performance=performance,
        risk_calculator=RiskCalculator(RiskPolicy()),
        plan=plan or _lenient_plan(),
        registry=registry,
        validation_repository=repo,
        param_checker=param_checker,
    )
    return engine, prices, performance, registry, repo


async def _run_validator(validator, bars_by_symbol) -> PerformanceSummary | None:
    start = min(b.ts.date() for bars in bars_by_symbol.values() for b in bars)
    end = max(b.ts.date() for bars in bars_by_symbol.values() for b in bars)
    return await validator.validate(
        symbols=list(bars_by_symbol),
        start=start,
        end=end,
        initial_capital=Decimal("100000"),
        interval=Interval.D1,
        commission_bps=10.0,
        slippage_bps=50.0,
    )


# --------------------------------------------------------------------------- #
# Data splits
# --------------------------------------------------------------------------- #


class TestSplits:
    def test_split_windows_are_contiguous_and_disjoint(self) -> None:
        dev, validation, oos = split_windows(date(2020, 1, 1), date(2020, 12, 31), 0.5, 0.25)
        assert dev.label == "2020-01-01/2020-06-30"
        assert validation.start == date(2020, 7, 1)
        assert validation.end == date(2020, 9, 29)
        assert oos.start == date(2020, 9, 30)
        assert oos.end == date(2020, 12, 31)
        assert dev.end < validation.start <= validation.end < oos.start

    def test_split_windows_require_room_for_oos(self) -> None:
        with pytest.raises(ValueError):
            split_windows(date(2020, 1, 1), date(2020, 12, 31), 0.8, 0.3)

    def test_slice_bars_respects_window(self) -> None:
        bars = _make_bars("AAA", date(2020, 1, 1), 100, seed=1, drift=0.0)
        window = DataWindow(date(2020, 1, 1), date(2020, 1, 31))
        sliced = slice_bars(bars, window)
        assert 20 <= len(sliced) <= 32
        assert all(window.start <= b.ts.date() <= window.end for b in sliced)

    def test_slice_bars_by_symbol(self) -> None:
        bars = _dataset()
        dev, _, _ = split_windows(date(2020, 1, 1), date(2021, 2, 3), 0.5, 0.25)
        sliced = slice_bars_by_symbol(bars, dev)
        assert set(sliced) == set(bars)
        assert all(dev.start <= b.ts.date() <= dev.end for b in sliced["AAA"])


# --------------------------------------------------------------------------- #
# Initial candidate filter
# --------------------------------------------------------------------------- #


class TestInitialFilter:
    def test_passes_healthy_candidate(self) -> None:
        report = InitialCandidateFilter().check(_default_spec(), _summary())
        assert report.passed
        assert report.reasons == {}

    def test_min_trades_rejection(self) -> None:
        limits = InitialFilterLimits(min_trades=100)
        report = InitialCandidateFilter(limits).check(_default_spec(), _summary(trades=40))
        assert not report.passed
        assert "min_trades" in report.reasons

    def test_single_indicator_rejection(self) -> None:
        spec = _default_spec(
            features=("close", "rsi"),
            complexity=2,
            entry=EntryRule(
                conditions=(Condition(feature="close", op=Operator.GT, value=100.0),)
            ),
            exit=ExitRule(conditions=(Condition(feature="rsi", op=Operator.GT, value=70.0),)),
        )
        report = InitialCandidateFilter().check(spec, _summary())
        assert not report.passed
        assert "single_indicator" in report.reasons

    def test_param_instability_rejection(self) -> None:
        limits = InitialFilterLimits(max_instability=0.1)
        jittered = [_summary(sharpe=1.2), _summary(sharpe=0.0), _summary(sharpe=1.5)]
        report = InitialCandidateFilter(limits).check(_default_spec(), _summary(), jittered)
        assert not report.passed
        assert report.instability is not None and report.instability > 0.1
        assert "param_instability" in report.reasons


# --------------------------------------------------------------------------- #
# Statistical edge and multiple-testing correction
# --------------------------------------------------------------------------- #


class TestEdgeStats:
    def test_expected_max_sharpe_grows_with_trials(self) -> None:
        assert expected_max_sharpe(100, 250) > expected_max_sharpe(10, 250)
        assert expected_max_sharpe(10, 250) < expected_max_sharpe(10, 50)

    def test_deflated_sharpe_rewards_higher_observed(self) -> None:
        adjusted_low, prob_low = deflated_sharpe(0.5, 100, 250)
        adjusted_high, prob_high = deflated_sharpe(2.0, 100, 250)
        assert adjusted_high > adjusted_low
        assert prob_high > prob_low

    def test_multiple_testing_risk_bands(self) -> None:
        high = multiple_testing_report(1.0, 100, 250)
        assert high.risk == "high"
        low = multiple_testing_report(5.0, 5, 250)
        assert low.risk == "low"
        assert low.observed_sharpe == 5.0
        assert low.hypotheses_tested == 5

    def test_multiple_testing_missing_sharpe_is_high_risk(self) -> None:
        report = multiple_testing_report(None, 100, 250)
        assert report.risk == "high"
        assert report.deflated_sharpe is None

    def test_compute_edge_stats_uses_trade_distribution(self) -> None:
        pnls = [Decimal("0.01"), Decimal("-0.005"), Decimal("0.02"), Decimal("0.005")]
        stats = compute_edge_stats(_summary(), trade_pnl_pcts=pnls)
        assert stats.win_rate == 0.55
        assert stats.trades == 40
        assert stats.trade_return_mean == pytest.approx(0.0075)
        assert stats.trade_return_std is not None and stats.trade_return_std > 0.0
        assert stats.trade_return_skew is not None
        assert stats.trade_return_kurtosis is not None


# --------------------------------------------------------------------------- #
# Benchmarks
# --------------------------------------------------------------------------- #


class TestBenchmarks:
    def test_sma200_filter_spec(self) -> None:
        spec = sma200_filter_spec("bench-1")
        assert spec.complexity == 2
        assert spec.features == ("close", "sma_200")

    def test_buy_and_hold_curve_appreciates_with_trend(self) -> None:
        curve = buy_and_hold_curve(
            _dataset(), Decimal("100000"), commission_bps=10.0, slippage_bps=50.0
        )
        assert len(curve) > 1
        assert curve[-1][1] > curve[0][1]
        assert curve[-1][1] > Decimal("100000")

    def test_random_permutation_result(self) -> None:
        result = random_permutation_result([0.01, 0.02, 0.005, -0.005, 0.015], seeds=4)
        assert result.trades == 5
        assert result.seeds == 4
        assert result.mean_total_return > 0.0

    def test_build_benchmark_report_value_added(self) -> None:
        def series(name: str, ret: float) -> BenchmarkSeriesResult:
            return BenchmarkSeriesResult(
                name=name, total_return=ret, sharpe=1.0, max_drawdown=-0.1,
                profit_factor=1.5, trades=30,
            )

        inputs = BenchmarkInputs(
            strategy=series("strategy", 0.40),
            buy_and_hold=series("bh", 0.10),
            index=series("index", 0.20),
            sma200=series("sma200", 0.15),
            momentum=series("momentum", 0.30),
            random=RandomBaselineResult(seeds=4, trades=30, mean_total_return=0.05,
                                        p90_total_return=0.12, worst_total_return=-0.02),
        )
        report = build_benchmark_report(inputs)
        assert report.beats_buy_and_hold
        assert report.beats_sma200
        assert report.beats_random_mean
        assert report.value_added

    def test_build_benchmark_report_no_value_added_when_beaten(self) -> None:
        def series(name: str, ret: float) -> BenchmarkSeriesResult:
            return BenchmarkSeriesResult(
                name=name, total_return=ret, sharpe=1.0, max_drawdown=-0.1,
                profit_factor=1.5, trades=30,
            )

        inputs = BenchmarkInputs(
            strategy=series("strategy", 0.05),
            buy_and_hold=series("bh", 0.10),
            index=series("index", 0.20),
            sma200=series("sma200", 0.15),
            momentum=series("momentum", 0.30),
            random=RandomBaselineResult(seeds=4, trades=30, mean_total_return=0.05,
                                        p90_total_return=0.12, worst_total_return=-0.02),
        )
        report = build_benchmark_report(inputs)
        assert not report.value_added


# --------------------------------------------------------------------------- #
# Robustness
# --------------------------------------------------------------------------- #


class TestRobustness:
    def test_parameter_variants_capped_and_distinct(self) -> None:
        variants = parameter_variants(_default_spec(), max_variants=3)
        assert len(variants) == 3
        assert all(v.id == _default_spec().id for v in variants)
        assert any(v != _default_spec() for v in variants)

    def test_parameter_variants_empty_without_numeric_thresholds(self) -> None:
        spec = _default_spec(
            entry=EntryRule(
                conditions=(Condition(feature="close", op=Operator.GT, ref_feature="ema_21"),)
            ),
            exit=ExitRule(
                conditions=(Condition(feature="close", op=Operator.LT, ref_feature="boll_middle"),)
            ),
            features=("close", "ema_21", "boll_middle"),
            complexity=2,
        )
        assert parameter_variants(spec) == []

    def test_parameter_checker_flags_negative_fraction(self) -> None:
        passing = ParameterRobustnessChecker().check(1.0, [0.5, 0.8, 1.1])
        assert passing.passed
        assert passing.positive_fraction == 1.0
        failing = ParameterRobustnessChecker().check(1.0, [-0.5, -1.0])
        assert not failing.passed

    def test_multi_timeframe_report(self) -> None:
        report = multi_timeframe_report(
            [
                TimeframeResult(interval="D1", trades=10, sharpe=1.5, total_return=0.1),
                TimeframeResult(interval="W1", trades=8, sharpe=0.5, total_return=0.05),
            ]
        )
        assert report.best_interval == "D1"
        assert report.positive_fraction == 1.0
        assert report.consistency_sharpe_std is not None

    def test_regime_report_from_buckets(self) -> None:
        report = regime_report_from_buckets(
            {"market:up": [Decimal("0.01"), Decimal("0.02"), Decimal("-0.005")]}
        )
        assert report.best_regime == "market:up"
        assert report.slices[0].trades == 3
        assert report.slices[0].win_rate == pytest.approx(2 / 3, abs=1e-4)

    def test_cross_asset_report_flags_single_symbol(self) -> None:
        report = cross_asset_report(
            [
                ("AAA", "tech", 10, 0.2, 1.5),
                ("BBB", "tech", 8, -0.1, -0.5),
            ]
        )
        assert report.symbols_with_profit == 1
        assert report.symbols_tested == 2
        assert report.single_symbol_dependence
        assert not report.single_sector_dependence

    def test_cost_sensitivity_report(self) -> None:
        levels = [
            CostLevelResult(0.0, 0.0, 5, 0.1, 1.5, 1.2, 0.6),
            CostLevelResult(10.0, 50.0, 5, 0.05, 1.1, 0.9, 0.55),
        ]
        report = cost_sensitivity_report(levels)
        assert report.edge_retained_at_realistic
        assert report.break_even_level == "10/50bps"


# --------------------------------------------------------------------------- #
# Research database
# --------------------------------------------------------------------------- #


class TestValidationRepository:
    def test_register_and_get(self) -> None:
        repo = InMemoryValidationRepository()
        record = _record()
        repo.register(record)
        assert repo.get("strat-0001") is record

    def test_duplicate_register_raises(self) -> None:
        repo = InMemoryValidationRepository()
        repo.register(_record())
        with pytest.raises(KeyError):
            repo.register(_record())

    def test_update_unknown_raises(self) -> None:
        repo = InMemoryValidationRepository()
        with pytest.raises(KeyError):
            repo.update(_record())

    def test_list_all_sorted_by_created_at(self) -> None:
        repo = InMemoryValidationRepository()
        repo.register(_record("strat-0001"))
        repo.register(_record("strat-0002"))
        assert [r.strategy_id for r in repo.list_all()] == ["strat-0001", "strat-0002"]

    def test_record_json_round_trip(self) -> None:
        record = _record(
            dev_result=StageResult(label="dev", summary=_summary("strat-0001")),
            oos_result=StageResult(label="oos", summary=_summary("strat-0001")),
            edge=compute_edge_stats(_summary("strat-0001")),
        )
        payload = encode_record(record)
        assert json.loads(json.dumps(payload)) == payload
        restored = decode_record(payload)
        assert restored.spec.id == "strat-0001"
        assert restored.stage is ValidationStage.VALIDATED
        assert restored.final_status is FinalStatus.VALIDATED
        assert restored.oos_result is not None
        assert restored.oos_result.summary.trades_count == 40
        assert restored.edge is not None
        assert restored.edge.trades == 40

    def test_export_import_round_trip(self) -> None:
        repo = InMemoryValidationRepository()
        repo.register(_record("strat-0001"))
        repo.register(_record("strat-0002"))
        exported = repo.export()
        other = InMemoryValidationRepository()
        assert other.import_(exported) == 2
        assert [r.strategy_id for r in other.list_all()] == ["strat-0001", "strat-0002"]


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #


class TestRanking:
    def test_weights_must_sum_to_one(self) -> None:
        with pytest.raises(ValueError):
            RankingWeights(expectancy=1.0)

    def test_rank_orders_by_composite_score(self) -> None:
        better = _record(
            "strat-0001",
            oos_result=StageResult(label="oos", summary=_summary("strat-0001", sharpe=2.5)),
            wf_result=WalkForwardResult(
                folds=(),
                aggregate=StageResult(label="walk-forward", summary=_summary("strat-0001")),
                stability_mean_sharpe=1.2,
                stability_std_sharpe=0.2,
            ),
            multiple_testing=MultipleTestingReport(
                hypotheses_tested=10,
                n_return_samples=250,
                observed_sharpe=2.5,
                expected_max_sharpe=1.0,
                deflated_sharpe=1.5,
                prob_real=0.99,
                risk="low",
            ),
        )
        worse = _record(
            "strat-0002",
            oos_result=StageResult(label="oos", summary=_summary("strat-0002", sharpe=0.5)),
            wf_result=WalkForwardResult(
                folds=(),
                aggregate=StageResult(label="walk-forward", summary=_summary("strat-0002")),
                stability_mean_sharpe=0.2,
                stability_std_sharpe=0.9,
            ),
            multiple_testing=MultipleTestingReport(
                hypotheses_tested=10,
                n_return_samples=250,
                observed_sharpe=0.5,
                expected_max_sharpe=1.0,
                deflated_sharpe=-0.5,
                prob_real=0.3,
                risk="high",
            ),
        )
        ranked = StrategyRanker().rank([worse, better])
        assert [entry.strategy_id for entry in ranked] == ["strat-0001", "strat-0002"]
        assert ranked[0].rank == 1
        assert ranked[0].score >= ranked[1].score

    def test_rank_empty(self) -> None:
        assert StrategyRanker().rank([]) == []


# --------------------------------------------------------------------------- #
# Walk-forward
# --------------------------------------------------------------------------- #


class TestValidationWalkForward:
    @pytest.mark.asyncio
    async def test_fold_results_recorded(self) -> None:
        bars = _dataset()
        prices = _BarsPriceRepository(bars)
        performance = FakePerformanceRepository()
        validator = ValidationWalkForwardValidator(
            prices=prices,
            performance=performance,
            risk_calculator=RiskCalculator(RiskPolicy()),
            strategy=_default_spec(),
            folds=2,
            lookback_bars=10,
            horizon_bars=5,
            strategy_label="validation-wf",
        )
        summary = await _run_validator(validator, bars)
        assert summary is not None
        folds = validator.fold_results()
        assert len(folds) >= 1
        assert all(fold.window_label.startswith("validation-wf") for fold in folds)
        assert all(fold.fold == index for index, fold in enumerate(folds))


# --------------------------------------------------------------------------- #
# Validation engine
# --------------------------------------------------------------------------- #


class TestValidationEngine:
    def test_plan_rejects_bad_fractions(self) -> None:
        with pytest.raises(ValueError):
            ValidationPlan(dev_fraction=0.8, validation_fraction=0.3)

    @pytest.mark.asyncio
    async def test_full_pipeline_validates_all_with_lenient_plan(self) -> None:
        engine, _, _, registry, repo = _engine(_engine_dataset(), plan=_lenient_plan())
        report = await engine.run(_request(_engine_dataset()))
        assert report.total_generated == 2
        assert report.validated == 2
        records = repo.list_all()
        assert len(records) == 2
        for record in records:
            assert record.stage is ValidationStage.VALIDATED
            assert record.final_status is FinalStatus.VALIDATED
            assert record.dev_result is not None
            assert record.validation_result is not None
            assert record.wf_result is not None
            assert record.oos_result is not None
            assert record.edge is not None
            assert record.multiple_testing is not None
        statuses = {r.status for r in registry.list_all()}
        assert StrategyStatus.VALIDATED in statuses
        assert not statuses - {StrategyStatus.VALIDATED}

    @pytest.mark.asyncio
    async def test_strict_dev_gate_rejects_everything(self) -> None:
        plan = _lenient_plan(
            dev_gate=MetricGate(min_trades=10_000),
        )
        engine, _, _, registry, repo = _engine(_dataset(), plan=plan)
        report = await engine.run(_request(_dataset()))
        assert report.rejected_development == report.total_generated == 2
        assert report.validated == 0
        assert all(r.final_status is FinalStatus.REJECTED for r in repo.list_all())
        assert all(r.status is not StrategyStatus.VALIDATED for r in registry.list_all())

    @pytest.mark.asyncio
    async def test_no_price_history_marks_failed(self) -> None:
        engine, _, _, registry, repo = _engine({})
        request = ResearchRequest(
            symbols=("AAA", "BBB"),
            start=date(2020, 1, 1),
            end=date(2020, 12, 31),
            interval=Interval.D1,
        )
        report = await engine.run(request)
        assert report.failed == report.total_generated == 2
        assert report.validated == 0
        assert all(r.stage is ValidationStage.FAILED for r in repo.list_all())

    @pytest.mark.asyncio
    async def test_report_counts_are_consistent(self) -> None:
        plan = _lenient_plan(
            wf_gate=MetricGate(min_sharpe=999.0),
        )
        engine, _, _, _, _ = _engine(_dataset(), plan=plan)
        report = await engine.run(_request(_dataset()))
        terminal = (
            report.rejected_initial_filter
            + report.rejected_development
            + report.rejected_robustness
            + report.rejected_walk_forward
            + report.rejected_validation
            + report.rejected_oos
            + report.validated
            + report.failed
        )
        assert terminal == report.total_generated
        assert report.reached_oos <= report.total_generated

    @pytest.mark.asyncio
    async def test_benchmark_gate_can_reject_at_oos(self) -> None:
        plan = _lenient_plan(benchmark_gate=True)
        engine, _, _, _, repo = _engine(_engine_dataset(), plan=plan)
        report = await engine.run(_request(_engine_dataset()))
        assert report.validated + report.rejected_oos == report.total_generated
        records = repo.list_all()
        assert all(
            r.final_status in (FinalStatus.VALIDATED, FinalStatus.RESEARCH_FURTHER)
            for r in records
        )


# --------------------------------------------------------------------------- #
# Settings plan
# --------------------------------------------------------------------------- #


class TestSettingsPlan:
    def test_default_plan(self) -> None:
        from qtrader.config.settings import Settings

        plan = Settings().strategy_validation_plan
        assert isinstance(plan, ValidationPlan)
        assert plan.folds == 4
        assert plan.dev_fraction == 0.5
        assert plan.validation_fraction == 0.25
        assert plan.benchmark_gate is True
        assert plan.limits.max_strategies == 60
        assert plan.commission_bps == 10.0
        assert plan.slippage_bps == 50.0
