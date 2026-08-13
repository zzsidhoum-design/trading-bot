"""Phase 3 data model — every stage's results, stored per strategy.

The research database keeps one :class:`ValidationRecord` per generated
strategy. Each record carries the full, reproducible history of that
hypothesis: the spec, the windows used, the outcome of every stage
(initial filter, development, robustness dimensions, walk-forward folds,
validation confirmation, OOS), the multiple-testing correction, the
statistical edge, and the final status. ``to_dict``/``from_dict`` make every
record JSON-round-trippable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from qtrader.application.research.strategy.engine import MetricGate
from qtrader.application.research.strategy.generator import SearchLimits
from qtrader.application.research.strategy.specs import StrategySpec, decode_spec, encode_spec
from qtrader.application.research.validation.filters import InitialFilterLimits
from qtrader.domain.entities import PerformanceSummary
from qtrader.domain.value_objects import Interval, TradingMode


class ValidationStage(StrEnum):
    """Fine-grained lifecycle state of one strategy through the pipeline."""

    GENERATED = "generated"
    REJECTED_INITIAL_FILTER = "rejected_initial_filter"
    REJECTED_DEVELOPMENT = "rejected_development"
    REJECTED_ROBUSTNESS = "rejected_robustness"
    REJECTED_WALK_FORWARD = "rejected_walk_forward"
    REJECTED_VALIDATION = "rejected_validation"
    RESEARCH_FURTHER = "research_further"
    REJECTED_OOS = "rejected_oos"
    VALIDATED = "validated"
    FAILED = "failed"


class FinalStatus(StrEnum):
    """The three research verdicts a strategy may finally carry."""

    REJECTED = "rejected"
    RESEARCH_FURTHER = "research_further"
    VALIDATED = "validated"


def final_status_for(stage: ValidationStage) -> FinalStatus | None:
    """Map a pipeline stage onto one of the three research verdicts."""
    if stage is ValidationStage.VALIDATED:
        return FinalStatus.VALIDATED
    if stage in (
        ValidationStage.RESEARCH_FURTHER,
        ValidationStage.REJECTED_OOS,
    ):
        return FinalStatus.RESEARCH_FURTHER
    if stage is ValidationStage.GENERATED:
        return None
    return FinalStatus.REJECTED


@dataclass(frozen=True, slots=True)
class ValidationPlan:
    """Everything one validation run needs: splits, gates, robustness knobs."""

    limits: SearchLimits = field(default_factory=SearchLimits)
    initial_filter: InitialFilterLimits = field(default_factory=InitialFilterLimits)
    dev_gate: MetricGate = field(default_factory=MetricGate)
    wf_gate: MetricGate = field(default_factory=MetricGate)
    oos_gate: MetricGate = field(default_factory=MetricGate)
    dev_fraction: float = 0.5
    validation_fraction: float = 0.25
    initial_capital: Decimal = Decimal("100000")
    commission_bps: float = 10.0
    slippage_bps: float = 50.0
    warmup_bars: int = 30
    folds: int = 4
    lookback_bars: int = 60
    horizon_bars: int = 12
    intervals: tuple[Interval, ...] = (Interval.D1,)
    cost_levels: tuple[tuple[float, float], ...] = (
        (0.0, 0.0),
        (1.0, 5.0),
        (5.0, 20.0),
        (10.0, 50.0),
        (25.0, 100.0),
    )
    param_robustness_runs: int = 8
    random_benchmark_seeds: int = 8
    benchmark_gate: bool = True
    max_ranked: int = 10

    def __post_init__(self) -> None:
        if self.dev_fraction <= 0.0 or self.validation_fraction <= 0.0:
            raise ValueError("split fractions must be positive")
        if self.dev_fraction + self.validation_fraction >= 1.0:
            raise ValueError("dev + validation fractions must leave an OOS window")
        if not self.intervals:
            raise ValueError("at least one interval is required")
        if self.folds < 2:
            raise ValueError("walk-forward requires at least two folds")


@dataclass(frozen=True, slots=True)
class StageResult:
    """One windowed backtest outcome (summary + window label)."""

    label: str
    summary: PerformanceSummary


@dataclass(frozen=True, slots=True)
class FoldResult:
    """One held-out walk-forward period (never just the aggregate)."""

    fold: int
    window_label: str
    trades: int
    sharpe: float | None
    total_return: float | None
    profit_factor: float | None


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    """Every walk-forward period plus the chained aggregate and stability."""

    folds: tuple[FoldResult, ...]
    aggregate: StageResult
    stability_mean_sharpe: float | None = None
    stability_std_sharpe: float | None = None
    positive_fold_fraction: float | None = None


@dataclass(frozen=True, slots=True)
class ParameterRobustnessReport:
    """Nearby-parameter behaviour: base vs jittered Sharpe distribution."""

    passed: bool
    base_sharpe: float | None
    variant_sharpes: tuple[float, ...]
    instability: float | None
    positive_fraction: float
    variants_tested: int


@dataclass(frozen=True, slots=True)
class TimeframeResult:
    """One interval's dev-window outcome in the multi-timeframe study."""

    interval: str
    trades: int
    sharpe: float | None
    total_return: float | None


@dataclass(frozen=True, slots=True)
class MultiTimeframeReport:
    """Robustness across timeframe combinations (never just the best one)."""

    results: tuple[TimeframeResult, ...]
    best_interval: str | None
    positive_fraction: float
    consistency_sharpe_std: float | None


@dataclass(frozen=True, slots=True)
class RegimeSlice:
    """Performance within one market/volatility regime bucket."""

    regime: str
    trades: int
    win_rate: float
    total_return_pct: float
    sharpe: float | None


@dataclass(frozen=True, slots=True)
class RegimeReport:
    """Where the strategy works and where it fails, by regime."""

    slices: tuple[RegimeSlice, ...]
    best_regime: str | None
    worst_regime: str | None


@dataclass(frozen=True, slots=True)
class AssetSlice:
    """Per-symbol dev outcome (cross-asset robustness)."""

    symbol: str
    sector: str | None
    trades: int
    total_return_pct: float
    sharpe: float | None


@dataclass(frozen=True, slots=True)
class SectorSlice:
    """Per-sector dev outcome (sector dependence)."""

    sector: str
    symbols: int
    trades: int
    total_return_pct: float


@dataclass(frozen=True, slots=True)
class CrossAssetReport:
    """Whether the edge generalizes across assets and sectors."""

    symbols: tuple[AssetSlice, ...]
    sectors: tuple[SectorSlice, ...]
    symbols_with_profit: int
    symbols_tested: int
    sectors_with_profit: int
    sectors_tested: int
    single_symbol_dependence: bool
    single_sector_dependence: bool


@dataclass(frozen=True, slots=True)
class CostLevelResult:
    """One execution-assumption level's outcome."""

    commission_bps: float
    slippage_bps: float
    trades: int
    total_return: float | None
    profit_factor: float | None
    sharpe: float | None
    win_rate: float | None


@dataclass(frozen=True, slots=True)
class CostSensitivityReport:
    """Edge retention across realistic execution assumptions."""

    levels: tuple[CostLevelResult, ...]
    edge_retained_at_realistic: bool
    break_even_level: str | None


@dataclass(frozen=True, slots=True)
class BenchmarkSeriesResult:
    """One benchmark's dev/OOS statistics."""

    name: str
    total_return: float | None
    sharpe: float | None
    max_drawdown: float | None
    profit_factor: float | None
    trades: int | None


@dataclass(frozen=True, slots=True)
class RandomBaselineResult:
    """Permuted-trade-order control with the same trade population."""

    seeds: int
    trades: int
    mean_total_return: float
    p90_total_return: float
    worst_total_return: float


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    """Strategy vs passive/naive baselines (does the complexity add value?)."""

    strategy: BenchmarkSeriesResult
    buy_and_hold: BenchmarkSeriesResult
    index: BenchmarkSeriesResult
    sma200: BenchmarkSeriesResult
    momentum: BenchmarkSeriesResult
    random: RandomBaselineResult
    beats_buy_and_hold: bool
    beats_index: bool
    beats_sma200: bool
    beats_random_mean: bool
    value_added: bool


@dataclass(frozen=True, slots=True)
class EdgeStats:
    """The full statistical picture of the final (OOS) results."""

    expectancy: float | None
    sharpe: float | None
    sortino: float | None
    max_drawdown: float | None
    profit_factor: float | None
    win_rate: float | None
    avg_win: float | None
    avg_loss: float | None
    trades: int
    turnover: float | None
    total_costs: float | None
    trade_return_mean: float | None
    trade_return_std: float | None
    trade_return_skew: float | None
    trade_return_kurtosis: float | None
    stability_mean_sharpe: float | None
    stability_std_sharpe: float | None


@dataclass(frozen=True, slots=True)
class MultipleTestingReport:
    """Multiple-testing correction (deflated Sharpe) for one survivor."""

    hypotheses_tested: int
    n_return_samples: int
    observed_sharpe: float | None
    expected_max_sharpe: float | None
    deflated_sharpe: float | None
    prob_real: float | None
    risk: str


@dataclass(frozen=True, slots=True)
class ValidationRecord:
    """One strategy's complete, reproducible research history."""

    spec: StrategySpec
    stage: ValidationStage = ValidationStage.GENERATED
    final_status: FinalStatus | None = None
    hypotheses_tested_before: int = 0
    universe: tuple[str, ...] = ()
    dataset_version: str = ""
    windows: tuple[str, str, str] = ("", "", "")
    dev_result: StageResult | None = None
    validation_result: StageResult | None = None
    wf_result: WalkForwardResult | None = None
    oos_result: StageResult | None = None
    regime_report: RegimeReport | None = None
    cross_asset_report: CrossAssetReport | None = None
    cost_report: CostSensitivityReport | None = None
    benchmark_report: BenchmarkReport | None = None
    multiple_testing: MultipleTestingReport | None = None
    edge: EdgeStats | None = None
    robustness_flags: tuple[str, ...] = ()
    notes: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def strategy_id(self) -> str:
        return self.spec.id


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """The aggregate run report (the required Phase 3 output counts)."""

    total_generated: int
    rejected_by_generator: int
    rejected_initial_filter: int
    rejected_development: int
    rejected_robustness: int
    rejected_walk_forward: int
    rejected_validation: int
    reached_oos: int
    rejected_oos: int
    research_further: int
    validated: int
    failed: int
    best_validated: tuple[str, ...] = ()
    rejected_reasons: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# JSON-safe encoding
# --------------------------------------------------------------------------- #


def _opt(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _opt_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _encode_summary(summary: PerformanceSummary) -> dict[str, Any]:
    return {
        "strategy": summary.strategy,
        "mode": summary.mode.value,
        "period_start": summary.period_start.isoformat(),
        "period_end": summary.period_end.isoformat(),
        "total_return": _opt_str(summary.total_return),
        "cagr": _opt_str(summary.cagr),
        "sharpe": _opt_str(summary.sharpe),
        "sortino": _opt_str(summary.sortino),
        "max_drawdown": _opt_str(summary.max_drawdown),
        "win_rate": _opt_str(summary.win_rate),
        "profit_factor": _opt_str(summary.profit_factor),
        "expectancy": _opt_str(summary.expectancy),
        "avg_win": _opt_str(summary.avg_win),
        "avg_loss": _opt_str(summary.avg_loss),
        "turnover": _opt_str(summary.turnover),
        "total_costs": _opt_str(summary.total_costs),
        "trades_count": summary.trades_count,
        "final_equity": _opt_str(summary.final_equity),
    }


def _decode_summary(data: dict[str, Any]) -> PerformanceSummary:
    return PerformanceSummary(
        strategy=str(data["strategy"]),
        mode=TradingMode(str(data["mode"])),
        period_start=date.fromisoformat(data["period_start"]),
        period_end=date.fromisoformat(data["period_end"]),
        total_return=_dec(data.get("total_return")),
        cagr=_dec(data.get("cagr")),
        sharpe=_dec(data.get("sharpe")),
        sortino=_dec(data.get("sortino")),
        max_drawdown=_dec(data.get("max_drawdown")),
        win_rate=_dec(data.get("win_rate")),
        profit_factor=_dec(data.get("profit_factor")),
        expectancy=_dec(data.get("expectancy")),
        avg_win=_dec(data.get("avg_win")),
        avg_loss=_dec(data.get("avg_loss")),
        turnover=_dec(data.get("turnover")),
        total_costs=_dec(data.get("total_costs")),
        trades_count=data.get("trades_count"),
        final_equity=_dec(data.get("final_equity")),
    )


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _decode_windows(data: object) -> tuple[str, str, str]:
    items = list(data) if isinstance(data, (list, tuple)) else []
    padded = [""] * 3
    for index, item in enumerate(items[:3]):
        padded[index] = str(item)
    return (padded[0], padded[1], padded[2])


def _encode_stage_result(result: StageResult) -> dict[str, Any]:
    return {"label": result.label, "summary": _encode_summary(result.summary)}


def _decode_stage_result(data: dict[str, Any] | None) -> StageResult | None:
    if data is None:
        return None
    return StageResult(label=str(data["label"]), summary=_decode_summary(data["summary"]))


def encode_fold_result(fold: FoldResult) -> dict[str, Any]:
    return asdict(fold)


def _decode_fold_result(data: dict[str, Any]) -> FoldResult:
    return FoldResult(
        fold=int(data["fold"]),
        window_label=str(data["window_label"]),
        trades=int(data["trades"]),
        sharpe=data.get("sharpe"),
        total_return=data.get("total_return"),
        profit_factor=data.get("profit_factor"),
    )


def encode_record(record: ValidationRecord) -> dict[str, Any]:
    """Serialize one :class:`ValidationRecord` to a JSON-safe dict."""
    payload: dict[str, Any] = {
        "spec": encode_spec(record.spec),
        "stage": record.stage.value,
        "final_status": record.final_status.value if record.final_status else None,
        "hypotheses_tested_before": record.hypotheses_tested_before,
        "universe": list(record.universe),
        "dataset_version": record.dataset_version,
        "windows": list(record.windows),
        "dev_result": _encode_stage_result(record.dev_result) if record.dev_result else None,
        "validation_result": (
            _encode_stage_result(record.validation_result) if record.validation_result else None
        ),
        "wf_result": _encode_wf(record.wf_result) if record.wf_result else None,
        "oos_result": _encode_stage_result(record.oos_result) if record.oos_result else None,
        "regime_report": _encode_regime(record.regime_report),
        "cross_asset_report": _encode_cross_asset(record.cross_asset_report),
        "cost_report": _encode_cost(record.cost_report),
        "benchmark_report": _encode_benchmark(record.benchmark_report),
        "multiple_testing": _encode_mtesting(record.multiple_testing),
        "edge": _encode_edge(record.edge),
        "robustness_flags": list(record.robustness_flags),
        "notes": record.notes,
        "created_at": record.created_at.isoformat(),
    }
    return _compact(payload)


def decode_record(data: dict[str, Any]) -> ValidationRecord:
    """Rebuild a :class:`ValidationRecord` from :func:`encode_record`."""
    return ValidationRecord(
        spec=decode_spec(data["spec"]),
        stage=ValidationStage(str(data["stage"])),
        final_status=(
            FinalStatus(str(data["final_status"])) if data.get("final_status") else None
        ),
        hypotheses_tested_before=int(data.get("hypotheses_tested_before", 0)),
        universe=tuple(data.get("universe", ())),
        dataset_version=str(data.get("dataset_version", "")),
        windows=_decode_windows(data.get("windows", ("", "", ""))),
        dev_result=_decode_stage_result(data.get("dev_result")),
        validation_result=_decode_stage_result(data.get("validation_result")),
        wf_result=_decode_wf(data.get("wf_result")),
        oos_result=_decode_stage_result(data.get("oos_result")),
        regime_report=_decode_regime(data.get("regime_report")),
        cross_asset_report=_decode_cross_asset(data.get("cross_asset_report")),
        cost_report=_decode_cost(data.get("cost_report")),
        benchmark_report=_decode_benchmark(data.get("benchmark_report")),
        multiple_testing=_decode_mtesting(data.get("multiple_testing")),
        edge=_decode_edge(data.get("edge")),
        robustness_flags=tuple(str(f) for f in data.get("robustness_flags", ())),
        notes=str(data.get("notes", "")),
        created_at=datetime.fromisoformat(data["created_at"]),
    )


def _encode_wf(result: WalkForwardResult) -> dict[str, Any]:
    return {
        "folds": [asdict(f) for f in result.folds],
        "aggregate": _encode_stage_result(result.aggregate),
        "stability_mean_sharpe": result.stability_mean_sharpe,
        "stability_std_sharpe": result.stability_std_sharpe,
        "positive_fold_fraction": result.positive_fold_fraction,
    }


def _decode_wf(data: dict[str, Any] | None) -> WalkForwardResult | None:
    if data is None:
        return None
    aggregate = _decode_stage_result(data["aggregate"])
    if aggregate is None:
        return None
    return WalkForwardResult(
        folds=tuple(_decode_fold_result(f) for f in data.get("folds", ())),
        aggregate=aggregate,
        stability_mean_sharpe=data.get("stability_mean_sharpe"),
        stability_std_sharpe=data.get("stability_std_sharpe"),
        positive_fold_fraction=data.get("positive_fold_fraction"),
    )


def _encode_regime(report: RegimeReport | None) -> dict[str, Any] | None:
    if report is None:
        return None
    return {
        "slices": [asdict(s) for s in report.slices],
        "best_regime": report.best_regime,
        "worst_regime": report.worst_regime,
    }


def _decode_regime(data: dict[str, Any] | None) -> RegimeReport | None:
    if data is None:
        return None
    return RegimeReport(
        slices=tuple(RegimeSlice(**item) for item in data.get("slices", ())),
        best_regime=data.get("best_regime"),
        worst_regime=data.get("worst_regime"),
    )


def _encode_cross_asset(report: CrossAssetReport | None) -> dict[str, Any] | None:
    if report is None:
        return None
    return {
        "symbols": [asdict(s) for s in report.symbols],
        "sectors": [asdict(s) for s in report.sectors],
        "symbols_with_profit": report.symbols_with_profit,
        "symbols_tested": report.symbols_tested,
        "sectors_with_profit": report.sectors_with_profit,
        "sectors_tested": report.sectors_tested,
        "single_symbol_dependence": report.single_symbol_dependence,
        "single_sector_dependence": report.single_sector_dependence,
    }


def _decode_cross_asset(data: dict[str, Any] | None) -> CrossAssetReport | None:
    if data is None:
        return None
    return CrossAssetReport(
        symbols=tuple(AssetSlice(**item) for item in data.get("symbols", ())),
        sectors=tuple(SectorSlice(**item) for item in data.get("sectors", ())),
        symbols_with_profit=int(data.get("symbols_with_profit", 0)),
        symbols_tested=int(data.get("symbols_tested", 0)),
        sectors_with_profit=int(data.get("sectors_with_profit", 0)),
        sectors_tested=int(data.get("sectors_tested", 0)),
        single_symbol_dependence=bool(data.get("single_symbol_dependence", False)),
        single_sector_dependence=bool(data.get("single_sector_dependence", False)),
    )


def _encode_cost(report: CostSensitivityReport | None) -> dict[str, Any] | None:
    if report is None:
        return None
    return {
        "levels": [asdict(level) for level in report.levels],
        "edge_retained_at_realistic": report.edge_retained_at_realistic,
        "break_even_level": report.break_even_level,
    }


def _decode_cost(data: dict[str, Any] | None) -> CostSensitivityReport | None:
    if data is None:
        return None
    return CostSensitivityReport(
        levels=tuple(CostLevelResult(**item) for item in data.get("levels", ())),
        edge_retained_at_realistic=bool(data.get("edge_retained_at_realistic", False)),
        break_even_level=data.get("break_even_level"),
    )


def _encode_benchmark(report: BenchmarkReport | None) -> dict[str, Any] | None:
    if report is None:
        return None
    return {
        "strategy": asdict(report.strategy),
        "buy_and_hold": asdict(report.buy_and_hold),
        "index": asdict(report.index),
        "sma200": asdict(report.sma200),
        "momentum": asdict(report.momentum),
        "random": asdict(report.random),
        "beats_buy_and_hold": report.beats_buy_and_hold,
        "beats_index": report.beats_index,
        "beats_sma200": report.beats_sma200,
        "beats_random_mean": report.beats_random_mean,
        "value_added": report.value_added,
    }


def _decode_benchmark(data: dict[str, Any] | None) -> BenchmarkReport | None:
    if data is None:
        return None
    return BenchmarkReport(
        strategy=BenchmarkSeriesResult(**data["strategy"]),
        buy_and_hold=BenchmarkSeriesResult(**data["buy_and_hold"]),
        index=BenchmarkSeriesResult(**data["index"]),
        sma200=BenchmarkSeriesResult(**data["sma200"]),
        momentum=BenchmarkSeriesResult(**data["momentum"]),
        random=RandomBaselineResult(**data["random"]),
        beats_buy_and_hold=bool(data.get("beats_buy_and_hold", False)),
        beats_index=bool(data.get("beats_index", False)),
        beats_sma200=bool(data.get("beats_sma200", False)),
        beats_random_mean=bool(data.get("beats_random_mean", False)),
        value_added=bool(data.get("value_added", False)),
    )


def _encode_mtesting(report: MultipleTestingReport | None) -> dict[str, Any] | None:
    return asdict(report) if report is not None else None


def _decode_mtesting(data: dict[str, Any] | None) -> MultipleTestingReport | None:
    if data is None:
        return None
    return MultipleTestingReport(**data)


def _encode_edge(report: EdgeStats | None) -> dict[str, Any] | None:
    return asdict(report) if report is not None else None


def _decode_edge(data: dict[str, Any] | None) -> EdgeStats | None:
    if data is None:
        return None
    return EdgeStats(**data)


def _compact(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop ``None`` values so the exported DB stays readable."""
    return {k: v for k, v in payload.items() if v is not None or k in ("spec", "stage")}


__all__ = [
    "AssetSlice",
    "BenchmarkReport",
    "BenchmarkSeriesResult",
    "CostLevelResult",
    "CostSensitivityReport",
    "CrossAssetReport",
    "EdgeStats",
    "FinalStatus",
    "FoldResult",
    "MultiTimeframeReport",
    "MultipleTestingReport",
    "ParameterRobustnessReport",
    "RandomBaselineResult",
    "RegimeReport",
    "RegimeSlice",
    "SectorSlice",
    "StageResult",
    "TimeframeResult",
    "ValidationPlan",
    "ValidationRecord",
    "ValidationReport",
    "ValidationStage",
    "WalkForwardResult",
    "decode_record",
    "encode_record",
    "final_status_for",
    "_opt",
]
