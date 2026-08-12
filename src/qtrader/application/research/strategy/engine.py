"""Strategy research workflow — generation to validated registry entries.

Pipeline: Feature Library -> Generation -> Initial Backtest (net of costs) ->
Filtering -> Robustness -> Walk-Forward/OOS -> Validation -> Registry. Every
backtest reuses the production ``BacktestRunner`` execution model (fills, costs,
ATR sizing, bracket/time exits) through its ``model_outputs`` contract; only
strategies that pass every stage may carry ``VALIDATED`` status. Nothing here
trades live and no profitability claim is made from a backtest alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any, cast

from qtrader.application.research.strategy.evaluator import StrategyEvaluator
from qtrader.application.research.strategy.generator import (
    SearchLimits,
    StrategyGenerator,
)
from qtrader.application.research.strategy.registry import (
    InMemoryStrategyRegistry,
    StrategyRecord,
    StrategyRegistry,
    StrategyStatus,
)
from qtrader.application.research.strategy.robustness import (
    RobustnessChecker,
    RobustnessLimits,
)
from qtrader.application.research.strategy.specs import Condition, StrategySpec
from qtrader.application.services.backtest import (
    BacktestParams,
    BacktestResult,
    BacktestRunner,
)
from qtrader.application.services.indicators import IndicatorEngine
from qtrader.application.services.risk_calculator import RiskCalculator
from qtrader.application.services.walk_forward import WalkForwardValidator
from qtrader.config.logging import get_logger
from qtrader.domain.entities import BacktestRun, PerformanceSummary, SystemLog
from qtrader.domain.ports import (
    BacktestRepository,
    PerformanceRepository,
    PriceRepository,
    SystemLogRepository,
)
from qtrader.domain.value_objects import Interval, Money

logger = get_logger("qtrader.strategy_research")

_DEFAULT_CAPITAL = Decimal("100000")
_FOLD_LOOKBACK_BARS = 60
_FOLD_HORIZON_BARS = 12
_FOLDS = 4
_MIN_TRAIN_SAMPLES = 0
_WARMUP = 30


@dataclass(frozen=True, slots=True)
class MetricGate:
    """Filtering stage: a net-of-cost backtest must clear every threshold."""

    min_sharpe: float = 0.0
    min_profit_factor: float = 1.0
    min_win_rate: float = 0.4
    min_trades: int = 30
    max_drawdown: float = -0.5

    def passes(self, summary: PerformanceSummary) -> bool:
        if summary.sharpe is not None and float(summary.sharpe) < self.min_sharpe:
            return False
        if (
            summary.profit_factor is not None
            and float(summary.profit_factor) < self.min_profit_factor
        ):
            return False
        if summary.win_rate is not None and float(summary.win_rate) < self.min_win_rate:
            return False
        if (summary.trades_count or 0) < self.min_trades:
            return False
        drawdown_ok = not (
            summary.max_drawdown is not None
            and float(summary.max_drawdown) < self.max_drawdown
        )
        return drawdown_ok


@dataclass(frozen=True, slots=True)
class ResearchPlan:
    """Everything a research run needs: search space, costs, gates, budget."""

    limits: SearchLimits = field(default_factory=SearchLimits)
    gate: MetricGate = field(default_factory=MetricGate)
    robustness: RobustnessLimits = field(default_factory=RobustnessLimits)
    initial_capital: Decimal = _DEFAULT_CAPITAL
    commission_bps: float = 10.0
    slippage_bps: float = 50.0
    warmup_bars: int = _WARMUP
    instability_budget: int = 12
    folds: int = _FOLDS


@dataclass(frozen=True, slots=True)
class ResearchRequest:
    """One research run: symbols, window and interval to search over."""

    symbols: tuple[str, ...]
    start: date
    end: date
    interval: Interval = Interval.D1
    initial_capital: Decimal = _DEFAULT_CAPITAL
    dataset_version: str = ""


@dataclass(frozen=True, slots=True)
class ResearchReport:
    """Summary of a research run (generated/rejected/passing/validated counts)."""

    generated: int
    rejected: int
    passing_initial: int
    validated: int
    backtests_run: int
    rejected_reasons: dict[str, str] = field(default_factory=dict)


class _NoopBacktestRepository(BacktestRepository):
    """Research runs are ephemeral; only validated OOS summaries persist."""

    async def create(self, run: BacktestRun) -> BacktestRun:
        return run

    async def save(self, run: BacktestRun) -> BacktestRun:
        return run

    async def get(self, run_id: int) -> BacktestRun | None:
        return None

    async def latest(self, name: str | None = None, limit: int = 5) -> list[BacktestRun]:
        return []


class StrategyResearchEngine:
    """Runs the full strategy research workflow against a plan and request."""

    def __init__(
        self,
        prices: PriceRepository,
        performance: PerformanceRepository,
        risk_calculator: RiskCalculator,
        indicator_engine: IndicatorEngine | None = None,
        logs: SystemLogRepository | None = None,
        plan: ResearchPlan | None = None,
        registry: StrategyRegistry | None = None,
        evaluator: StrategyEvaluator | None = None,
        generator: StrategyGenerator | None = None,
        checker: RobustnessChecker | None = None,
    ) -> None:
        self._prices = prices
        self._performance = performance
        self._risk = risk_calculator
        self._indicators = indicator_engine or IndicatorEngine()
        self._logs = logs
        self._plan = plan or ResearchPlan()
        self._registry = registry or InMemoryStrategyRegistry()
        self._evaluator = evaluator or StrategyEvaluator(warmup_bars=self._plan.warmup_bars)
        self._generator = generator or StrategyGenerator()
        self._checker = checker or RobustnessChecker(self._plan.robustness)

    @property
    def registry(self) -> StrategyRegistry:
        return self._registry

    @property
    def plan(self) -> ResearchPlan:
        return self._plan

    async def run(self, request: ResearchRequest) -> ResearchReport:
        """Generate hypotheses, screen them net-of-cost, validate and register."""
        generation = self._generator.generate(self._plan.limits)
        for spec in generation.specs:
            self._registry.register(spec, status=StrategyStatus.GENERATED, note="generated")

        rejected_reasons: dict[str, str] = dict(generation.rejections)
        passing_initial = 0
        validated = 0
        backtests_run = 0
        budget = self._plan.limits.computational_budget
        instability_budget = self._plan.instability_budget

        for spec in generation.specs:
            if backtests_run >= budget:
                break
            backtests_run += 1

            initial = await self._initial_backtest(spec, request)
            if initial is None:
                self._set_status(spec.id, StrategyStatus.FAILED, note="no price history")
                rejected_reasons[spec.id] = "no price history"
                continue
            self._update_metrics(spec.id, initial.summary, request, StrategyStatus.INITIAL_BACKTEST)

            if not self._plan.gate.passes(initial.summary):
                self._set_status(spec.id, StrategyStatus.REJECTED, note="failed metric gate")
                rejected_reasons[spec.id] = "failed metric gate"
                continue
            passing_initial += 1

            jittered: list[PerformanceSummary | None] = []
            if instability_budget > 0:
                jittered, used = await self._jitter_checks(
                    spec, request, min(2, instability_budget)
                )
                instability_budget -= used
                backtests_run += used

            report = self._checker.check(spec, initial.summary, jittered or None)
            if not report.passed:
                flags = ", ".join(k for k, ok in report.checks.items() if not ok)
                self._set_status(spec.id, StrategyStatus.REJECTED, note=f"robustness: {flags}")
                rejected_reasons[spec.id] = f"robustness: {flags}"
                continue

            oos = await self._walk_forward(spec, request)
            if oos is None:
                self._set_status(
                    spec.id,
                    StrategyStatus.FAILED,
                    note="walk-forward produced no OOS",
                )
                rejected_reasons[spec.id] = "walk-forward produced no OOS"
                continue
            if not self._plan.gate.passes(oos):
                self._set_status(spec.id, StrategyStatus.REJECTED, note="failed OOS gate")
                rejected_reasons[spec.id] = "failed OOS gate"
                continue

            record = self._registry.get(spec.id)
            if record is not None:
                self._registry.update(
                    StrategyRecord(
                        spec=record.spec,
                        status=StrategyStatus.VALIDATED,
                        universe=record.universe,
                        dataset_version=record.dataset_version,
                        backtest_period=record.backtest_period,
                        metrics=oos,
                        robustness=report,
                        enabled=record.enabled,
                        created_at=record.created_at,
                        notes="validated via walk-forward OOS",
                    )
                )
            validated += 1
            await self._log(
                "INFO",
                "strategy validated",
                strategy=spec.id,
                trades=oos.trades_count,
                sharpe=_optional_float(oos.sharpe),
                total_return=_optional_float(oos.total_return),
            )

        await self._log(
            "INFO",
            "research run complete",
            generated=len(generation.specs),
            rejected=len(rejected_reasons),
            passing_initial=passing_initial,
            validated=validated,
            backtests_run=backtests_run,
        )
        return ResearchReport(
            generated=len(generation.specs),
            rejected=len(rejected_reasons),
            passing_initial=passing_initial,
            validated=validated,
            backtests_run=backtests_run,
            rejected_reasons=rejected_reasons,
        )

    async def _initial_backtest(
        self, spec: StrategySpec, request: ResearchRequest
    ) -> BacktestResult | None:
        bars_by_symbol = await self._load_bars(request)
        if not bars_by_symbol:
            return None
        return self._backtest(spec, bars_by_symbol, request, period_label="initial")

    def _backtest(
        self,
        spec: StrategySpec,
        bars_by_symbol: dict[str, list[Any]],
        request: ResearchRequest,
        *,
        period_label: str,
    ) -> BacktestResult:
        interval = request.interval
        series = {
            symbol: self._indicators.compute_series(bars, symbol, interval)
            for symbol, bars in bars_by_symbol.items()
            if bars
        }
        probs = self._evaluator.probs(spec, bars_by_symbol, series)
        run = BacktestRun(
            name=f"research-{spec.id}-{period_label}",
            universe=list(bars_by_symbol),
            start=request.start,
            end=request.end,
            initial_capital=Money(request.initial_capital),
            interval=interval,
            strategy=spec.id,
            commission_bps=Decimal(str(self._plan.commission_bps)),
            slippage_bps=Decimal(str(self._plan.slippage_bps)),
        )
        params = BacktestParams(
            interval=interval,
            strategy=spec.id,
            commission_bps=self._plan.commission_bps,
            slippage_bps=self._plan.slippage_bps,
            warmup_bars=self._plan.warmup_bars,
        )
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
            model_outputs=probs,
            series=series,
        )

    async def _jitter_checks(
        self,
        spec: StrategySpec,
        request: ResearchRequest,
        max_runs: int,
    ) -> tuple[list[PerformanceSummary | None], int]:
        bars_by_symbol = await self._load_bars(request)
        if not bars_by_symbol:
            return [], 0
        summaries: list[PerformanceSummary | None] = []
        used = 0
        for variant in self._jitter_variants(spec)[:max_runs]:
            result = self._backtest(variant, bars_by_symbol, request, period_label="jitter")
            summaries.append(result.summary)
            used += 1
        return summaries, used

    @staticmethod
    def _jitter_variants(spec: StrategySpec) -> list[StrategySpec]:
        variants: list[StrategySpec] = []
        for sign in (1.0, -1.0):
            rebuilt = _jitter_spec(spec, sign)
            if rebuilt is not None:
                variants.append(rebuilt)
        return variants

    async def _walk_forward(
        self, spec: StrategySpec, request: ResearchRequest
    ) -> PerformanceSummary | None:
        validator = StrategyWalkForwardValidator(
            prices=self._prices,
            performance=self._performance,
            risk_calculator=self._risk,
            indicator_engine=self._indicators,
            logs=self._logs,
            strategy=spec,
            evaluator=self._evaluator,
            folds=self._plan.folds,
            lookback_bars=_FOLD_LOOKBACK_BARS,
            horizon_bars=_FOLD_HORIZON_BARS,
            strategy_label=spec.id,
        )
        return await validator.validate(
            symbols=list(request.symbols),
            start=request.start,
            end=request.end,
            initial_capital=request.initial_capital,
            interval=request.interval,
            commission_bps=self._plan.commission_bps,
            slippage_bps=self._plan.slippage_bps,
        )

    async def _load_bars(
        self, request: ResearchRequest
    ) -> dict[str, list[Any]]:
        start_dt = datetime.combine(request.start, time.min, tzinfo=UTC)
        end_dt = datetime.combine(request.end, time.max, tzinfo=UTC)
        by_symbol: dict[str, list[Any]] = {}
        for symbol in request.symbols:
            bars = await self._prices.history(
                symbol, request.interval, start_dt, end_dt, limit=50_000
            )
            by_symbol[symbol] = sorted(bars, key=lambda b: b.ts)
        return by_symbol

    def _update_metrics(
        self,
        strategy_id: str,
        metrics: PerformanceSummary,
        request: ResearchRequest,
        status: StrategyStatus,
    ) -> None:
        record = self._registry.get(strategy_id)
        if record is None:
            return
        self._registry.update(
            StrategyRecord(
                spec=record.spec,
                status=status,
                universe=tuple(request.symbols),
                dataset_version=request.dataset_version,
                backtest_period=f"{request.start.isoformat()}/{request.end.isoformat()}",
                metrics=metrics,
                robustness=record.robustness,
                enabled=record.enabled,
                created_at=record.created_at,
                notes=record.notes,
            )
        )

    def _set_status(
        self, strategy_id: str, status: StrategyStatus, *, note: str
    ) -> None:
        self._registry.set_status(strategy_id, status, note=note)

    async def _log(self, level: str, message: str, **context: Any) -> None:
        if self._logs is None:
            return
        await self._logs.record(
            SystemLog(level=level, component="strategy_research", message=message, context=context)
        )


class StrategyWalkForwardValidator(WalkForwardValidator):
    """Walk-forward over a rule strategy instead of a trained logistic model.

    Reuses the parent's calendar-fold orchestration (expanding train windows,
    OOS chaining, aggregate metrics, persistence); only the signal source
    changes — a :class:`StrategySpec` evaluated into ``prob_up`` per bar.
    """

    def __init__(
        self,
        prices: PriceRepository,
        performance: PerformanceRepository,
        risk_calculator: RiskCalculator,
        strategy: StrategySpec,
        evaluator: StrategyEvaluator | None = None,
        indicator_engine: IndicatorEngine | None = None,
        logs: SystemLogRepository | None = None,
        folds: int = _FOLDS,
        lookback_bars: int = _FOLD_LOOKBACK_BARS,
        horizon_bars: int = _FOLD_HORIZON_BARS,
        prob_buy: float = 0.52,
        prob_sell: float = 0.48,
        sectors: dict[str, str] | None = None,
        strategy_label: str | None = None,
    ) -> None:
        super().__init__(
            prices=prices,
            performance=performance,
            risk_calculator=risk_calculator,
            indicator_engine=indicator_engine,
            logs=logs,
            min_train_samples=_MIN_TRAIN_SAMPLES,
            folds=folds,
            lookback_bars=lookback_bars,
            horizon_bars=horizon_bars,
            prob_buy=prob_buy,
            prob_sell=prob_sell,
            sectors=sectors,
            strategy_label=strategy_label or strategy.id,
        )
        self._strategy = strategy
        self._evaluator = evaluator or StrategyEvaluator(warmup_bars=_WARMUP)

    def _fit_model(  # type: ignore[override]
        self, train: dict[str, list[Any]]
    ) -> StrategySpec:
        return self._strategy

    def _simulate_fold(
        self,
        symbols: list[str],
        interval: Interval,
        model: object,
        full: dict[str, list[Any]],
        ts: int,
        te: int,
        initial_capital: Decimal,
        commission_bps: float,
        slippage_bps: float,
    ) -> BacktestResult:
        spec = cast(StrategySpec, model)
        all_dates = [bar.ts.date() for bars in full.values() for bar in bars[ts:te]]
        fold_start = min(all_dates) if all_dates else datetime.now(UTC).date()
        fold_end = max(all_dates) if all_dates else fold_start
        run = BacktestRun(
            name=f"walk-forward-{spec.id}-{fold_start.isoformat()}",
            universe=symbols,
            start=fold_start,
            end=fold_end,
            initial_capital=Money(initial_capital),
            interval=interval,
            strategy=spec.id,
            commission_bps=Decimal(str(commission_bps)),
            slippage_bps=Decimal(str(slippage_bps)),
        )
        runner = BacktestRunner(
            prices=self._prices,
            backtests=_NoopBacktestRepository(),
            performance=self._performance,
            risk_calculator=self._risk,
            indicator_engine=self._indicator_engine,
            model=None,
            model_prob_buy=self._prob_buy,
            model_prob_sell=self._prob_sell,
            sectors=self._sectors,
        )
        params = BacktestParams(
            interval=interval,
            strategy=spec.id,
            commission_bps=commission_bps,
            slippage_bps=slippage_bps,
        )
        series = {
            symbol: self._indicator_engine.compute_series(bars, symbol, interval)
            for symbol, bars in full.items()
            if bars
        }
        probs = self._evaluator.probs(spec, full, series)
        # Only bars in the held-out window [ts, te) may trade — everything else HOLDs.
        window_ts = {bar.ts for bars in full.values() for bar in bars[ts:te]}
        probs = {
            symbol: {ts: p for ts, p in symbol_probs.items() if ts in window_ts}
            for symbol, symbol_probs in probs.items()
        }
        return runner._simulate(  # noqa: SLF001
            run, full, initial_capital, params, model_outputs=probs, series=series
        )


def _jitter_spec(spec: StrategySpec, sign: float) -> StrategySpec | None:
    """Return a variant with the first numeric threshold nudged by ``sign``."""
    step = None
    target_value: float | None = None
    target: Condition | None = None
    for condition in list(spec.entry.conditions) + list(spec.exit.conditions):
        if condition.ref_feature is None and condition.value is not None:
            target = condition
            target_value = condition.value
            step = max(1.0, abs(condition.value) * 0.25)
            break
    if target is None or target_value is None or step is None:
        return None

    def _remap(condition: Condition) -> Condition:
        if condition is not target:
            return condition
        return replace(
            condition,
            value=round(target_value + sign * step, 6),
        )

    return replace(
        spec,
        entry=replace(spec.entry, conditions=tuple(_remap(c) for c in spec.entry.conditions)),
        exit=replace(spec.exit, conditions=tuple(_remap(c) for c in spec.exit.conditions)),
    )


def _optional_float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


__all__ = [
    "MetricGate",
    "ResearchPlan",
    "ResearchReport",
    "ResearchRequest",
    "StrategyResearchEngine",
    "StrategyWalkForwardValidator",
]
