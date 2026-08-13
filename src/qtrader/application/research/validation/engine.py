"""Phase 3 pipeline — automated strategy validation and edge detection.

The engine runs every generated hypothesis through a strict, stage-gated
pipeline: initial filtering on the development window -> development testing ->
robustness (parameters, timeframes, regimes, cross-asset, costs) -> walk-forward
over dev+validation -> validation confirmation -> untouched OOS testing ->
benchmark/value gate. Every stage outcome is stored in the research database and
only strategies that clear every gate can be ``VALIDATED``. Nothing here trades
live and no backtest is ever presented as a proven future edge.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from qtrader.application.research.strategy.engine import (
    ResearchRequest,
    StrategyWalkForwardValidator,
)
from qtrader.application.research.strategy.evaluator import StrategyEvaluator
from qtrader.application.research.strategy.generator import StrategyGenerator
from qtrader.application.research.strategy.registry import (
    InMemoryStrategyRegistry,
    StrategyRegistry,
    StrategyStatus,
)
from qtrader.application.research.strategy.specs import StrategySpec
from qtrader.application.research.validation.benchmarks import (
    BenchmarkInputs,
    BenchmarkReport,
    BenchmarkSeriesResult,
    build_benchmark_report,
    buy_and_hold_curve,
    random_permutation_result,
    sma200_filter_spec,
)
from qtrader.application.research.validation.edge import (
    compute_edge_stats,
    multiple_testing_report,
)
from qtrader.application.research.validation.filters import (
    InitialCandidateFilter,
)
from qtrader.application.research.validation.ranking import StrategyRanker
from qtrader.application.research.validation.records import (
    AssetSlice,
    CostLevelResult,
    CostSensitivityReport,
    CrossAssetReport,
    EdgeStats,
    FinalStatus,
    FoldResult,
    MultipleTestingReport,
    MultiTimeframeReport,
    ParameterRobustnessReport,
    RegimeReport,
    StageResult,
    TimeframeResult,
    ValidationPlan,
    ValidationRecord,
    ValidationReport,
    ValidationStage,
    WalkForwardResult,
    final_status_for,
)
from qtrader.application.research.validation.repository import (
    InMemoryValidationRepository,
    ValidationRepository,
)
from qtrader.application.research.validation.robustness import (
    ParameterRobustnessChecker,
    cost_sensitivity_report,
    cross_asset_report,
    multi_timeframe_report,
    parameter_variants,
    regime_report_from_buckets,
)
from qtrader.application.research.validation.splits import (
    DataWindow,
    slice_bars_by_symbol,
    split_windows,
)
from qtrader.application.services.backtest import (
    BacktestParams,
    BacktestResult,
    BacktestRunner,
)
from qtrader.application.services.indicators import IndicatorEngine
from qtrader.application.services.multitimeframe import regime_labels_for
from qtrader.application.services.performance_metrics import PerformanceMetrics
from qtrader.application.services.risk_calculator import RiskCalculator
from qtrader.config.logging import get_logger
from qtrader.domain.entities import BacktestRun, PerformanceSummary, SystemLog
from qtrader.domain.ports import (
    BacktestRepository,
    PerformanceRepository,
    PriceRepository,
    SystemLogRepository,
)
from qtrader.domain.value_objects import Interval, Money, PriceBar, TradingMode

logger = get_logger("qtrader.strategy_validation")


class _NoopBacktestRepository(BacktestRepository):
    """Validation backtests are ephemeral; results live in the research DB."""

    async def create(self, run: BacktestRun) -> BacktestRun:
        return run

    async def save(self, run: BacktestRun) -> BacktestRun:
        return run

    async def get(self, run_id: int) -> BacktestRun | None:
        return None

    async def latest(self, name: str | None = None, limit: int = 5) -> list[BacktestRun]:
        return []


class ValidationWalkForwardValidator(StrategyWalkForwardValidator):
    """Walk-forward that records every held-out period, not just the aggregate."""

    def __init__(self, *, window_label: str = "validation-wf", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._window_label = window_label
        self._fold_results: list[FoldResult] = []
        self._fold_counter = 0

    def _simulate_fold(self, *args: Any, **kwargs: Any) -> BacktestResult:
        result = super()._simulate_fold(*args, **kwargs)
        summary = result.summary
        self._fold_results.append(
            FoldResult(
                fold=self._fold_counter,
                window_label=f"{self._window_label}-fold-{self._fold_counter}",
                trades=summary.trades_count or 0,
                sharpe=_opt_float(summary.sharpe),
                total_return=_opt_float(summary.total_return),
                profit_factor=_opt_float(summary.profit_factor),
            )
        )
        self._fold_counter += 1
        return result

    def fold_results(self) -> tuple[FoldResult, ...]:
        return tuple(self._fold_results)

    def clear(self) -> None:
        self._fold_results = []
        self._fold_counter = 0


class StrategyValidationEngine:
    """Orchestrates the full validation pipeline for one research request."""

    def __init__(
        self,
        prices: PriceRepository,
        performance: PerformanceRepository,
        risk_calculator: RiskCalculator,
        indicator_engine: IndicatorEngine | None = None,
        logs: SystemLogRepository | None = None,
        plan: ValidationPlan | None = None,
        registry: StrategyRegistry | None = None,
        validation_repository: ValidationRepository | None = None,
        evaluator: StrategyEvaluator | None = None,
        generator: StrategyGenerator | None = None,
        filter_: InitialCandidateFilter | None = None,
        param_checker: ParameterRobustnessChecker | None = None,
        ranker: StrategyRanker | None = None,
        sectors: dict[str, str] | None = None,
    ) -> None:
        self._prices = prices
        self._performance = performance
        self._risk = risk_calculator
        self._indicators = indicator_engine or IndicatorEngine()
        self._logs = logs
        self._plan = plan or ValidationPlan()
        self._registry = registry or InMemoryStrategyRegistry()
        self._repo = validation_repository or InMemoryValidationRepository()
        self._evaluator = evaluator or StrategyEvaluator(
            warmup_bars=self._plan.warmup_bars
        )
        self._generator = generator or StrategyGenerator()
        self._filter = filter_ or InitialCandidateFilter(self._plan.initial_filter)
        self._param_checker = param_checker or ParameterRobustnessChecker()
        self._ranker = ranker or StrategyRanker()
        self._sectors = sectors or {}

    @property
    def plan(self) -> ValidationPlan:
        return self._plan

    @property
    def registry(self) -> StrategyRegistry:
        return self._registry

    @property
    def repository(self) -> ValidationRepository:
        return self._repo

    async def run(self, request: ResearchRequest) -> ValidationReport:
        generation = self._generator.generate(self._plan.limits)
        bars_by_symbol = await self._load_bars(request)
        dev, validation, oos = split_windows(
            request.start, request.end,
            self._plan.dev_fraction, self._plan.validation_fraction,
        )
        windows_labels = (dev.label, validation.label, oos.label)
        dev_bars = slice_bars_by_symbol(bars_by_symbol, dev)
        validation_bars = slice_bars_by_symbol(bars_by_symbol, validation)
        oos_bars = slice_bars_by_symbol(bars_by_symbol, oos)

        n_total = len(generation.specs)
        counts = {
            "rejected_initial": 0,
            "rejected_development": 0,
            "rejected_robustness": 0,
            "rejected_walk_forward": 0,
            "rejected_validation": 0,
            "rejected_oos": 0,
            "reached_oos": 0,
            "validated": 0,
            "failed": 0,
        }
        rejected_reasons: dict[str, str] = {}

        for index, spec in enumerate(generation.specs):
            record = self._repo.register(
                ValidationRecord(
                    spec=spec,
                    stage=ValidationStage.GENERATED,
                    final_status=None,
                    hypotheses_tested_before=index,
                    universe=tuple(request.symbols),
                    dataset_version=request.dataset_version,
                    windows=windows_labels,
                )
            )
            self._register_in_registry(spec)

            dev_result = await self._backtest_window(spec, dev_bars, dev, "dev", request)
            if dev_result is None:
                record = self._advance(
                    record, ValidationStage.FAILED, note="no development price history"
                )
                counts["failed"] += 1
                continue

            jittered = await self._jitter_backtests(
                spec, dev_bars, dev, request, self._plan.initial_filter.jitter_runs
            )
            filter_report = self._filter.check(spec, dev_result.summary, jittered)
            record = self._advance(
                record, ValidationStage.GENERATED,
                dev_result=StageResult(label="dev", summary=dev_result.summary),
            )
            if not filter_report.passed:
                reason = "; ".join(filter_report.reasons.values()) or "initial filter"
                record = self._advance(
                    record, ValidationStage.REJECTED_INITIAL_FILTER, note=reason,
                    dev_result=StageResult(label="dev", summary=dev_result.summary),
                )
                counts["rejected_initial"] += 1
                rejected_reasons[spec.id] = reason
                continue

            if not self._plan.dev_gate.passes(dev_result.summary):
                record = self._advance(
                    record, ValidationStage.REJECTED_DEVELOPMENT,
                    note="failed development gate",
                    dev_result=StageResult(label="dev", summary=dev_result.summary),
                )
                counts["rejected_development"] += 1
                rejected_reasons[spec.id] = "failed development gate"
                continue

            robustness = await self._run_robustness(spec, dev_bars, dev, request, dev_result)
            record = self._advance(
                record, ValidationStage.GENERATED,
                regime_report=robustness.regime,
                cross_asset_report=robustness.cross_asset,
                cost_report=robustness.cost,
            )
            if not robustness.parameters.passed:
                reason = (
                    f"parameter robustness failed "
                    f"(instability {robustness.parameters.instability})"
                )
                record = self._advance(
                    record, ValidationStage.REJECTED_ROBUSTNESS,
                    note=reason, flags=robustness.flags,
                )
                counts["rejected_robustness"] += 1
                rejected_reasons[spec.id] = reason
                continue

            wf = await self._walk_forward(spec, request, dev.start, validation.end)
            if wf is None or not self._plan.wf_gate.passes(wf.aggregate.summary):
                record = self._advance(
                    record, ValidationStage.REJECTED_WALK_FORWARD,
                    note="walk-forward OOS failed gate", flags=robustness.flags,
                )
                counts["rejected_walk_forward"] += 1
                rejected_reasons[spec.id] = "walk-forward OOS failed gate"
                continue
            record = self._advance(
                record, ValidationStage.GENERATED, wf_result=wf, flags=robustness.flags
            )

            validation_result = await self._backtest_window(
                spec, validation_bars, validation, "validation", request
            )
            if (
                validation_result is None
                or not self._plan.dev_gate.passes(validation_result.summary)
            ):
                record = self._advance(
                    record, ValidationStage.REJECTED_VALIDATION,
                    note="validation window failed gate", flags=robustness.flags,
                )
                counts["rejected_validation"] += 1
                rejected_reasons[spec.id] = "validation window failed gate"
                continue
            record = self._advance(
                record, ValidationStage.GENERATED,
                validation_result=StageResult(
                    label="validation", summary=validation_result.summary
                ),
                flags=robustness.flags,
            )

            benchmark_report = await self._benchmarks(
                spec, dev_bars, dev, request, dev_result
            )
            record = self._advance(
                record, ValidationStage.RESEARCH_FURTHER,
                benchmark_report=benchmark_report,
                note="dev pipeline passed; awaiting OOS",
                flags=robustness.flags,
            )
            counts["reached_oos"] += 1

            oos_result = await self._backtest_window(spec, oos_bars, oos, "oos", request)
            if oos_result is None:
                record = self._advance(
                    record, ValidationStage.FAILED, note="no OOS price history"
                )
                counts["failed"] += 1
                continue
            record = self._advance(
                record, ValidationStage.RESEARCH_FURTHER,
                oos_result=StageResult(label="oos", summary=oos_result.summary),
            )

            oos_pass = self._plan.oos_gate.passes(oos_result.summary)
            benchmark_pass = True
            if oos_pass and self._plan.benchmark_gate:
                oos_bench = await self._benchmarks(
                    spec, oos_bars, oos, request, oos_result
                )
                benchmark_pass = oos_bench.value_added

            if oos_pass and benchmark_pass:
                edge = compute_edge_stats(
                    oos_result.summary,
                    [trade.pnl_pct for trade in oos_result.trades],
                    stability_mean_sharpe=wf.stability_mean_sharpe,
                    stability_std_sharpe=wf.stability_std_sharpe,
                )
                mtest = multiple_testing_report(
                    _opt_float(oos_result.summary.sharpe),
                    n_total,
                    max(len(oos_result.equity_curve) - 1, 1),
                )
                record = self._advance(
                    record, ValidationStage.VALIDATED,
                    edge=edge, multiple_testing=mtest,
                    note="validated on untouched OOS data",
                )
                counts["validated"] += 1
                self._mark_registry(spec.id, StrategyStatus.VALIDATED)
                await self._log(
                    "INFO", "strategy validated",
                    strategy=spec.id,
                    sharpe=_opt_float(oos_result.summary.sharpe),
                    trades=oos_result.summary.trades_count,
                    prob_real=mtest.prob_real,
                )
            else:
                reason = "failed OOS gate" if not oos_pass else "does not beat OOS benchmarks"
                record = self._advance(
                    record, ValidationStage.REJECTED_OOS, note=reason
                )
                counts["rejected_oos"] += 1
                rejected_reasons[spec.id] = reason

        best = self._rank_validated()
        research_further = sum(
            1
            for record in self._repo.list_all()
            if record.final_status is FinalStatus.RESEARCH_FURTHER
        )
        await self._log(
            "INFO", "validation run complete",
            generated=n_total,
            rejected_initial=counts["rejected_initial"],
            validated=counts["validated"],
            reached_oos=counts["reached_oos"],
        )
        return ValidationReport(
            total_generated=n_total,
            rejected_by_generator=len(generation.rejections),
            rejected_initial_filter=counts["rejected_initial"],
            rejected_development=counts["rejected_development"],
            rejected_robustness=counts["rejected_robustness"],
            rejected_walk_forward=counts["rejected_walk_forward"],
            rejected_validation=counts["rejected_validation"],
            reached_oos=counts["reached_oos"],
            rejected_oos=counts["rejected_oos"],
            research_further=research_further,
            validated=counts["validated"],
            failed=counts["failed"],
            best_validated=best,
            rejected_reasons=rejected_reasons,
        )

    # ------------------------------------------------------------------ #
    # Stages
    # ------------------------------------------------------------------ #

    async def _run_robustness(
        self,
        spec: StrategySpec,
        bars: dict[str, list[PriceBar]],
        window: DataWindow,
        request: ResearchRequest,
        dev_result: BacktestResult,
    ) -> RobustnessBundle:
        param = await self._parameter_robustness(spec, dev_result.summary, bars, window, request)
        timeframes = await self._timeframe_robustness(spec, request, window)
        regime = await self._regime_testing(spec, bars, request, dev_result)
        cross_asset = await self._cross_asset(spec, bars, window, request)
        cost = await self._cost_sensitivity(spec, bars, window, request)
        flags: list[str] = []
        if timeframes.positive_fraction < 0.5:
            flags.append("works on few timeframes")
        if cross_asset.single_symbol_dependence:
            flags.append("single-symbol dependence")
        if cross_asset.single_sector_dependence:
            flags.append("single-sector dependence")
        if not cost.edge_retained_at_realistic:
            flags.append("edge lost at realistic costs")
        return RobustnessBundle(
            parameters=param,
            timeframes=timeframes,
            regime=regime,
            cross_asset=cross_asset,
            cost=cost,
            flags=tuple(flags),
        )

    async def _parameter_robustness(
        self,
        spec: StrategySpec,
        summary: PerformanceSummary,
        bars: dict[str, list[PriceBar]],
        window: DataWindow,
        request: ResearchRequest,
    ) -> ParameterRobustnessReport:
        variants = parameter_variants(
            spec, max_variants=self._plan.param_robustness_runs
        )
        variant_sharpes: list[float | None] = []
        for variant in variants:
            result = await self._backtest_window(variant, bars, window, "param-jitter", request)
            variant_sharpes.append(
                _opt_float(result.summary.sharpe) if result is not None else None
            )
        return self._param_checker.check(_opt_float(summary.sharpe), variant_sharpes)

    async def _timeframe_robustness(
        self,
        spec: StrategySpec,
        request: ResearchRequest,
        window: DataWindow,
    ) -> MultiTimeframeReport:
        results: list[TimeframeResult] = []
        for interval in self._plan.intervals:
            bars = await self._load_bars_interval(request.symbols, interval, window)
            if not any(bars.values()):
                continue
            result = await self._backtest_window(
                spec, bars, window, f"tf-{interval.value}", request, interval=interval
            )
            if result is None:
                continue
            summary = result.summary
            results.append(
                TimeframeResult(
                    interval=interval.value,
                    trades=summary.trades_count or 0,
                    sharpe=_opt_float(summary.sharpe),
                    total_return=_opt_float(summary.total_return),
                )
            )
        return multi_timeframe_report(results)

    async def _regime_testing(
        self,
        spec: StrategySpec,
        bars: dict[str, list[PriceBar]],
        request: ResearchRequest,
        dev_result: BacktestResult,
    ) -> RegimeReport:
        del spec, request
        labels_by_symbol: dict[str, dict[date, Any]] = {}
        for symbol, symbol_bars in bars.items():
            if not symbol_bars:
                continue
            labels_by_symbol[symbol] = {
                day.date: day for day in regime_labels_for(symbol_bars)
            }
        buckets: dict[str, list[Decimal]] = {}
        for trade in dev_result.trades:
            by_day = labels_by_symbol.get(trade.symbol, {})
            day = by_day.get(trade.entry_time.date())
            if day is None:
                continue
            buckets.setdefault(f"market:{day.market}", []).append(trade.pnl_pct)
            buckets.setdefault(f"vol:{day.volatility}", []).append(trade.pnl_pct)
        return regime_report_from_buckets(buckets)

    async def _cross_asset(
        self,
        spec: StrategySpec,
        bars: dict[str, list[PriceBar]],
        window: DataWindow,
        request: ResearchRequest,
    ) -> CrossAssetReport:
        asset_results: list[tuple[str, str | None, int, float, float | None]] = []
        for symbol, symbol_bars in bars.items():
            if not symbol_bars:
                continue
            result = await self._backtest_window(
                spec, {symbol: symbol_bars}, window, f"cross-{symbol}", request
            )
            if result is None:
                continue
            summary = result.summary
            asset_results.append(
                (
                    symbol,
                    self._sectors.get(symbol),
                    summary.trades_count or 0,
                    float(summary.total_return or 0.0),
                    _opt_float(summary.sharpe),
                )
            )
        return cross_asset_report(asset_results)

    async def _cost_sensitivity(
        self,
        spec: StrategySpec,
        bars: dict[str, list[PriceBar]],
        window: DataWindow,
        request: ResearchRequest,
    ) -> CostSensitivityReport:
        levels: list[CostLevelResult] = []
        for commission_bps, slippage_bps in self._plan.cost_levels:
            result = await self._backtest_window(
                spec, bars, window, f"cost-{commission_bps:g}/{slippage_bps:g}",
                request, commission_bps=commission_bps, slippage_bps=slippage_bps,
            )
            if result is None:
                continue
            summary = result.summary
            levels.append(
                CostLevelResult(
                    commission_bps=commission_bps,
                    slippage_bps=slippage_bps,
                    trades=summary.trades_count or 0,
                    total_return=_opt_float(summary.total_return),
                    profit_factor=_opt_float(summary.profit_factor),
                    sharpe=_opt_float(summary.sharpe),
                    win_rate=_opt_float(summary.win_rate),
                )
            )
        return cost_sensitivity_report(levels)

    async def _benchmarks(
        self,
        spec: StrategySpec,
        bars: dict[str, list[PriceBar]],
        window: DataWindow,
        request: ResearchRequest,
        strategy_result: BacktestResult,
    ) -> BenchmarkReport:
        commission = self._plan.commission_bps
        slippage = self._plan.slippage_bps
        strategy_res = _series_result("strategy", strategy_result.summary)
        bh_curve = buy_and_hold_curve(bars, request.initial_capital, commission, slippage)
        bh_summary = PerformanceMetrics.from_series(
            strategy="buy-and-hold",
            mode=TradingMode.BACKTEST,
            period_start=window.start,
            period_end=window.end,
            equity_curve=bh_curve,
            trade_pnl_pcts=[],
            interval=request.interval,
        )
        bh_res = _series_result("buy-and-hold", bh_summary)
        index_res = _series_result("equal-weight-index", bh_summary)

        sma_spec = sma200_filter_spec(f"{spec.id}-bench")
        sma_result = await self._backtest_window(sma_spec, bars, window, "bench-sma200", request)
        sma_res = _series_result("sma200", sma_result.summary if sma_result else None)

        mom_result = await self._backtest_window(
            spec, bars, window, "bench-momentum", request, momentum=True
        )
        mom_res = _series_result("momentum", mom_result.summary if mom_result else None)

        random_res = random_permutation_result(
            [float(trade.pnl_pct) for trade in strategy_result.trades],
            seeds=self._plan.random_benchmark_seeds,
        )
        return build_benchmark_report(
            BenchmarkInputs(
                strategy=strategy_res,
                buy_and_hold=bh_res,
                index=index_res,
                sma200=sma_res,
                momentum=mom_res,
                random=random_res,
            )
        )

    async def _walk_forward(
        self,
        spec: StrategySpec,
        request: ResearchRequest,
        start: date,
        end: date,
    ) -> WalkForwardResult | None:
        validator = ValidationWalkForwardValidator(
            prices=self._prices,
            performance=self._performance,
            risk_calculator=self._risk,
            indicator_engine=self._indicators,
            logs=self._logs,
            strategy=spec,
            evaluator=self._evaluator,
            folds=self._plan.folds,
            lookback_bars=self._plan.lookback_bars,
            horizon_bars=self._plan.horizon_bars,
            strategy_label=spec.id,
        )
        aggregate = await validator.validate(
            symbols=list(request.symbols),
            start=start,
            end=end,
            initial_capital=request.initial_capital,
            interval=request.interval,
            commission_bps=self._plan.commission_bps,
            slippage_bps=self._plan.slippage_bps,
        )
        folds = validator.fold_results()
        if aggregate is None:
            return None
        sharpes = [fold.sharpe for fold in folds if fold.sharpe is not None]
        mean = statistics.fmean(sharpes) if sharpes else None
        std = statistics.pstdev(sharpes) if len(sharpes) >= 2 else None
        positive = (
            sum(1 for fold in folds if (fold.sharpe or 0.0) > 0.0) / len(folds)
            if folds
            else None
        )
        return WalkForwardResult(
            folds=folds,
            aggregate=StageResult(label="walk-forward", summary=aggregate),
            stability_mean_sharpe=mean,
            stability_std_sharpe=std,
            positive_fold_fraction=positive,
        )

    # ------------------------------------------------------------------ #
    # Backtest plumbing
    # ------------------------------------------------------------------ #

    async def _load_bars(self, request: ResearchRequest) -> dict[str, list[PriceBar]]:
        window = DataWindow(request.start, request.end)
        return await self._load_bars_interval(request.symbols, request.interval, window)

    async def _load_bars_interval(
        self,
        symbols: tuple[str, ...],
        interval: Interval,
        window: DataWindow,
    ) -> dict[str, list[PriceBar]]:
        start_dt = datetime.combine(window.start, time.min, tzinfo=UTC)
        end_dt = datetime.combine(window.end, time.max, tzinfo=UTC)
        by_symbol: dict[str, list[PriceBar]] = {}
        for symbol in symbols:
            bars = await self._prices.history(symbol, interval, start_dt, end_dt, limit=50_000)
            by_symbol[symbol] = sorted(bars, key=lambda b: b.ts)
        return by_symbol

    async def _backtest_window(
        self,
        spec: StrategySpec,
        bars_by_symbol: dict[str, list[PriceBar]],
        window: DataWindow,
        label: str,
        request: ResearchRequest,
        *,
        commission_bps: float | None = None,
        slippage_bps: float | None = None,
        momentum: bool = False,
        interval: Interval | None = None,
    ) -> BacktestResult | None:
        if not any(bars_by_symbol.values()):
            return None
        interval = interval or request.interval
        commission = (
            self._plan.commission_bps if commission_bps is None else commission_bps
        )
        slippage = self._plan.slippage_bps if slippage_bps is None else slippage_bps
        run = BacktestRun(
            name=f"validation-{spec.id}-{label}",
            universe=list(bars_by_symbol),
            start=window.start,
            end=window.end,
            initial_capital=Money(request.initial_capital),
            interval=interval,
            strategy="momentum-benchmark" if momentum else spec.id,
            commission_bps=Decimal(str(commission)),
            slippage_bps=Decimal(str(slippage)),
        )
        params = BacktestParams(
            interval=interval,
            strategy=spec.id,
            commission_bps=commission,
            slippage_bps=slippage,
            warmup_bars=self._plan.warmup_bars,
        )
        model_outputs = None
        series = None
        if not momentum:
            series = {
                symbol: self._indicators.compute_series(bars, symbol, interval)
                for symbol, bars in bars_by_symbol.items()
                if bars
            }
            model_outputs = self._evaluator.probs(spec, bars_by_symbol, series)
        runner = BacktestRunner(
            prices=self._prices,
            backtests=_NoopBacktestRepository(),
            performance=self._performance,
            risk_calculator=self._risk,
            indicator_engine=self._indicators,
            logs=self._logs,
        )
        return runner._simulate(  # noqa: SLF001
            run,
            bars_by_symbol,
            request.initial_capital,
            params,
            model_outputs=model_outputs,
            series=series,
        )

    async def _jitter_backtests(
        self,
        spec: StrategySpec,
        bars: dict[str, list[PriceBar]],
        window: DataWindow,
        request: ResearchRequest,
        max_runs: int,
    ) -> list[PerformanceSummary | None]:
        summaries: list[PerformanceSummary | None] = []
        for variant in parameter_variants(spec, max_variants=max_runs):
            result = await self._backtest_window(variant, bars, window, "filter-jitter", request)
            summaries.append(result.summary if result is not None else None)
        return summaries

    # ------------------------------------------------------------------ #
    # Bookkeeping
    # ------------------------------------------------------------------ #

    def _advance(
        self,
        record: ValidationRecord,
        stage: ValidationStage,
        *,
        note: str = "",
        flags: tuple[str, ...] = (),
        **fields: Any,
    ) -> ValidationRecord:
        return self._repo.update(
            replace(
                record,
                stage=stage,
                final_status=final_status_for(stage),
                robustness_flags=flags,
                notes=note,
                **fields,
            )
        )

    def _rank_validated(self) -> tuple[str, ...]:
        validated = [
            record
            for record in self._repo.list_all()
            if record.stage is ValidationStage.VALIDATED
        ]
        ranked = self._ranker.rank(validated)
        return tuple(entry.strategy_id for entry in ranked[: self._plan.max_ranked])

    def _register_in_registry(self, spec: StrategySpec) -> None:
        if self._registry.get(spec.id) is not None:
            return
        self._registry.register(spec, status=StrategyStatus.GENERATED)

    def _mark_registry(self, strategy_id: str, status: StrategyStatus) -> None:
        self._registry.set_status(strategy_id, status, note="phase 3 validation")

    async def _log(self, level: str, message: str, **context: Any) -> None:
        if self._logs is None:
            return
        await self._logs.record(
            SystemLog(
                level=level,
                component="strategy_validation",
                message=message,
                context=context,
            )
        )


@dataclass(frozen=True, slots=True)
class RobustnessBundle:
    """All dev-window robustness outcomes for one candidate."""

    parameters: ParameterRobustnessReport
    timeframes: MultiTimeframeReport
    regime: RegimeReport
    cross_asset: CrossAssetReport
    cost: CostSensitivityReport
    flags: tuple[str, ...]


def _opt_float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _series_result(
    name: str, summary: PerformanceSummary | None
) -> BenchmarkSeriesResult:
    if summary is None:
        return BenchmarkSeriesResult(name=name, total_return=None, sharpe=None,
                                     max_drawdown=None, profit_factor=None, trades=None)
    return BenchmarkSeriesResult(
        name=name,
        total_return=_opt_float(summary.total_return),
        sharpe=_opt_float(summary.sharpe),
        max_drawdown=_opt_float(summary.max_drawdown),
        profit_factor=_opt_float(summary.profit_factor),
        trades=summary.trades_count,
    )


__all__ = [
    "RobustnessBundle",
    "StrategyValidationEngine",
    "ValidationWalkForwardValidator",
    "AssetSlice",
    "CostSensitivityReport",
    "CrossAssetReport",
    "EdgeStats",
    "MultiTimeframeReport",
    "MultipleTestingReport",
    "ParameterRobustnessReport",
    "RegimeReport",
    "TimeframeResult",
    "ValidationPlan",
    "ValidationRecord",
    "ValidationReport",
    "ValidationStage",
    "WalkForwardResult",
]
