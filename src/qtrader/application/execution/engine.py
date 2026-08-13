"""Execution-robustness engine — Phase 4 verdicts on validated strategies.

For one validated :class:`StrategyRecord`, the engine runs a reference
(theoretical) backtest on the validation window — identical research assumptions
(10bps commission / 50bps slippage) — then an execution-aware backtest per
:class:`ExecutionScenario` using the same ``model_outputs`` (identical signals),
and produces a :class:`StrategyExecutionReport` with per-scenario metrics and the
EXECUTION_REJECTED / EXECUTION_SENSITIVE / EXECUTION_ROBUST verdict. The engine
never trades live and only ever reuses the production ``BacktestRunner`` fill
loop (through the execution broker) — signals, sizing and risk gates are exactly
the ones the strategy was validated with.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from qtrader.application.execution.backtest import ExecutionAwareBacktestRunner
from qtrader.application.execution.metrics import (
    classify_execution,
    compute_execution_metrics,
)
from qtrader.application.execution.models import (
    ExecutionPlan,
    ExecutionScenario,
    ExecutionStatus,
    ScenarioResult,
    StrategyExecutionReport,
)
from qtrader.application.research.strategy.evaluator import StrategyEvaluator
from qtrader.application.research.strategy.registry import StrategyRecord
from qtrader.application.services.backtest import (
    BacktestParams,
    BacktestResult,
    BacktestRunner,
)
from qtrader.application.services.indicators import IndicatorEngine
from qtrader.application.services.risk_calculator import RiskCalculator
from qtrader.config.logging import get_logger
from qtrader.domain.entities import BacktestRun, SystemLog
from qtrader.domain.ports import (
    BacktestRepository,
    PerformanceRepository,
    PriceRepository,
    SystemLogRepository,
)
from qtrader.domain.value_objects import Interval, Money, PriceBar

logger = get_logger("qtrader.execution")

_DEFAULT_CAPITAL = Decimal("100000")
_WARMUP = 30
_DEFAULT_COMMISSION_BPS = 10.0
_DEFAULT_SLIPPAGE_BPS = 50.0


class _NoopBacktestRepository(BacktestRepository):
    """Execution runs are analysis-only; nothing here persists."""

    async def create(self, run: BacktestRun) -> BacktestRun:
        return run

    async def save(self, run: BacktestRun) -> BacktestRun:
        return run

    async def get(self, run_id: int) -> BacktestRun | None:
        return None

    async def latest(self, name: str | None = None, limit: int = 5) -> list[BacktestRun]:
        return []


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """Window and universe to run the execution-robustness check over."""

    symbols: tuple[str, ...]
    start: date
    end: date
    interval: Interval = Interval.D1
    initial_capital: Decimal = _DEFAULT_CAPITAL
    dataset_version: str = ""


class StrategyExecutionEngine:
    """Runs theoretical + execution-aware backtests and classifies a strategy."""

    def __init__(
        self,
        prices: PriceRepository,
        performance: PerformanceRepository,
        risk_calculator: RiskCalculator,
        indicator_engine: IndicatorEngine | None = None,
        logs: SystemLogRepository | None = None,
        plan: ExecutionPlan | None = None,
        evaluator: StrategyEvaluator | None = None,
        sectors: dict[str, str] | None = None,
        research_commission_bps: float = _DEFAULT_COMMISSION_BPS,
        research_slippage_bps: float = _DEFAULT_SLIPPAGE_BPS,
        warmup_bars: int = _WARMUP,
    ) -> None:
        self._prices = prices
        self._performance = performance
        self._risk = risk_calculator
        self._indicators = indicator_engine or IndicatorEngine()
        self._logs = logs
        self._plan = plan or ExecutionPlan()
        self._evaluator = evaluator or StrategyEvaluator(warmup_bars=warmup_bars)
        self._sectors = sectors
        self._research_commission_bps = research_commission_bps
        self._research_slippage_bps = research_slippage_bps
        self._warmup = warmup_bars

    @property
    def plan(self) -> ExecutionPlan:
        return self._plan

    async def run(
        self, record: StrategyRecord, request: ExecutionRequest
    ) -> StrategyExecutionReport:
        """Execute one strategy across all scenarios and classify it."""
        bars_by_symbol = await self._load_bars(request)
        if not bars_by_symbol:
            return self._empty_report(record, request, "no price history")
        series = {
            symbol: self._indicators.compute_series(bars, symbol, request.interval)
            for symbol, bars in bars_by_symbol.items()
            if bars
        }
        probs = self._evaluator.probs(record.spec, bars_by_symbol, series)

        theoretical = self._theoretical_backtest(record, request, bars_by_symbol, series, probs)

        scenario_results: list[ScenarioResult] = []
        worst_sharpe_deg: float | None = None
        worst_return_deg: float | None = None
        worst_scenario: ExecutionScenario | None = None
        for scenario in self._plan.scenarios:
            result = self._execution_backtest(
                record, request, bars_by_symbol, series, probs, scenario
            )
            stats = self._last_stats
            if stats is None:
                raise RuntimeError("execution runner did not produce statistics")
            metrics = compute_execution_metrics(
                scenario=scenario,
                theoretical=theoretical.summary,
                execution_summary=result.summary,
                execution_equity_curve=result.equity_curve,
                trades=result.trades,
                stats=stats,
                assessments=self._last_assessments,
                adv_seen=self._last_adv_seen,
                liquidity=self._plan.liquidity,
            )
            scenario_results.append(
                ScenarioResult(
                    scenario=scenario,
                    summary=result.summary,
                    stats=stats,
                    metrics=metrics,
                )
            )
            if metrics.degradation_sharpe is not None and (
                worst_sharpe_deg is None or metrics.degradation_sharpe > worst_sharpe_deg
            ):
                worst_sharpe_deg = metrics.degradation_sharpe
                worst_scenario = scenario
            if metrics.degradation_return is not None and (
                worst_return_deg is None or metrics.degradation_return > worst_return_deg
            ):
                worst_return_deg = metrics.degradation_return

        baseline = next(
            (s.metrics for s in scenario_results if s.scenario is ExecutionScenario.BASELINE),
            scenario_results[0].metrics if scenario_results else None,
        )
        status = ExecutionStatus.EXECUTION_ROBUST
        if baseline is not None:
            status = classify_execution(
                baseline=baseline,
                worst_degradation_sharpe=worst_sharpe_deg,
                worst_degradation_return=worst_return_deg,
                plan=self._plan,
            )
        report = StrategyExecutionReport(
            strategy_id=record.spec.id,
            status=status,
            theoretical_return=_optional_float(theoretical.summary.total_return),
            theoretical_sharpe=_optional_float(theoretical.summary.sharpe),
            theoretical_trades=theoretical.summary.trades_count or 0,
            scenarios=tuple(scenario_results),
            execution_sensitivity=worst_sharpe_deg,
            worst_scenario=worst_scenario,
        )
        await self._log(
            "INFO",
            "execution classified",
            strategy=record.spec.id,
            status=status.value,
        )
        return report

    def _theoretical_backtest(
        self,
        record: StrategyRecord,
        request: ExecutionRequest,
        bars_by_symbol: dict[str, list[PriceBar]],
        series: dict[str, list[Any]],
        probs: dict[str, dict[datetime, float]],
    ) -> BacktestResult:
        run = BacktestRun(
            name=f"execution-theoretical-{record.spec.id}",
            universe=list(bars_by_symbol),
            start=request.start,
            end=request.end,
            initial_capital=Money(request.initial_capital),
            interval=request.interval,
            strategy=record.spec.id,
            commission_bps=Decimal(str(self._research_commission_bps)),
            slippage_bps=Decimal(str(self._research_slippage_bps)),
        )
        params = BacktestParams(
            interval=request.interval,
            strategy=record.spec.id,
            commission_bps=self._research_commission_bps,
            slippage_bps=self._research_slippage_bps,
            warmup_bars=self._warmup,
        )
        runner = BacktestRunner(
            prices=self._prices,
            backtests=_NoopBacktestRepository(),
            performance=self._performance,
            risk_calculator=self._risk,
            indicator_engine=self._indicators,
            logs=self._logs,
            sectors=self._sectors,
        )
        return runner._simulate(  # noqa: SLF001
            run, bars_by_symbol, request.initial_capital, params, model_outputs=probs, series=series
        )

    def _execution_backtest(
        self,
        record: StrategyRecord,
        request: ExecutionRequest,
        bars_by_symbol: dict[str, list[PriceBar]],
        series: dict[str, list[Any]],
        probs: dict[str, dict[datetime, float]],
        scenario: ExecutionScenario,
    ) -> BacktestResult:
        run = BacktestRun(
            name=f"execution-aware-{record.spec.id}-{scenario.value}",
            universe=list(bars_by_symbol),
            start=request.start,
            end=request.end,
            initial_capital=Money(request.initial_capital),
            interval=request.interval,
            strategy=record.spec.id,
            commission_bps=Decimal(str(self._plan.commission_bps)),
            slippage_bps=Decimal("0"),
        )
        params = BacktestParams(
            interval=request.interval,
            strategy=record.spec.id,
            commission_bps=0.0,
            slippage_bps=0.0,
            warmup_bars=self._warmup,
        )
        runner = ExecutionAwareBacktestRunner(
            prices=self._prices,
            backtests=_NoopBacktestRepository(),
            performance=self._performance,
            risk_calculator=self._risk,
            indicator_engine=self._indicators,
            logs=self._logs,
            sectors=self._sectors,
            scenario=scenario,
            plan=self._plan,
        )
        result = runner._simulate(  # noqa: SLF001
            run,
            bars_by_symbol,
            request.initial_capital,
            params,
            model_outputs=probs,
            series=series,
        )
        self._last_stats = runner.last_stats()
        self._last_assessments = runner.last_assessments()
        self._last_adv_seen = runner.last_adv_seen()
        if self._last_stats is None:
            raise RuntimeError("execution runner did not produce statistics")
        return result

    async def _load_bars(
        self, request: ExecutionRequest
    ) -> dict[str, list[PriceBar]]:
        start_dt = datetime.combine(request.start, time.min, tzinfo=UTC)
        end_dt = datetime.combine(request.end, time.max, tzinfo=UTC)
        by_symbol: dict[str, list[PriceBar]] = {}
        for symbol in request.symbols:
            bars = await self._prices.history(
                symbol, request.interval, start_dt, end_dt, limit=50_000
            )
            by_symbol[symbol] = sorted(bars, key=lambda b: b.ts)
        return by_symbol

    def _empty_report(
        self,
        record: StrategyRecord,
        request: ExecutionRequest,
        reason: str,
    ) -> StrategyExecutionReport:
        return StrategyExecutionReport(
            strategy_id=record.spec.id,
            status=ExecutionStatus.EXECUTION_REJECTED,
            theoretical_return=None,
            theoretical_sharpe=None,
            theoretical_trades=0,
            scenarios=(),
            notes=reason,
        )

    async def _log(self, level: str, message: str, **context: Any) -> None:
        if self._logs is None:
            return
        await self._logs.record(
            SystemLog(level=level, component="execution", message=message, context=context)
        )


def _optional_float(value: Decimal | float | None) -> float | None:
    return None if value is None else float(value)


__all__ = [
    "ExecutionRequest",
    "StrategyExecutionEngine",
]
