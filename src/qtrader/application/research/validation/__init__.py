"""Phase 3 — automated strategy validation and edge detection.

Stage-gated validation pipeline built on top of the Phase 2 strategy research
engine: initial filtering on the development window, development testing,
parameter/timeframe/regime/cross-asset/cost robustness, per-fold walk-forward
over dev+validation, validation confirmation, untouched OOS testing, and a
benchmark/value gate. Every stage outcome is stored per strategy in a
JSON-round-trippable research database; only strategies that clear every gate
are ``VALIDATED``.
"""

from qtrader.application.research.validation.benchmarks import (
    BenchmarkInputs,
    build_benchmark_report,
    buy_and_hold_curve,
    random_permutation_result,
    sma200_filter_spec,
)
from qtrader.application.research.validation.edge import (
    compute_edge_stats,
    deflated_sharpe,
    expected_max_sharpe,
    multiple_testing_report,
)
from qtrader.application.research.validation.engine import (
    StrategyValidationEngine,
    ValidationWalkForwardValidator,
)
from qtrader.application.research.validation.filters import (
    InitialCandidateFilter,
    InitialFilterCheck,
    InitialFilterLimits,
    InitialFilterReport,
)
from qtrader.application.research.validation.ranking import (
    RankedStrategy,
    RankingWeights,
    StrategyRanker,
)
from qtrader.application.research.validation.records import (
    BenchmarkReport,
    BenchmarkSeriesResult,
    CostLevelResult,
    CostSensitivityReport,
    CrossAssetReport,
    EdgeStats,
    FinalStatus,
    FoldResult,
    MultipleTestingReport,
    MultiTimeframeReport,
    ParameterRobustnessReport,
    RandomBaselineResult,
    RegimeReport,
    RegimeSlice,
    SectorSlice,
    StageResult,
    TimeframeResult,
    ValidationPlan,
    ValidationRecord,
    ValidationReport,
    ValidationStage,
    WalkForwardResult,
    decode_record,
    encode_record,
    final_status_for,
)
from qtrader.application.research.validation.repository import (
    InMemoryValidationRepository,
    ValidationRepository,
)
from qtrader.application.research.validation.robustness import (
    ParameterRobustnessChecker,
    ParameterRobustnessLimits,
    cost_sensitivity_report,
    cross_asset_report,
    multi_timeframe_report,
    parameter_variants,
    regime_report_from_buckets,
)
from qtrader.application.research.validation.splits import (
    DataWindow,
    slice_bars,
    slice_bars_by_symbol,
    split_windows,
)

__all__ = [
    "BenchmarkInputs",
    "BenchmarkReport",
    "BenchmarkSeriesResult",
    "CostLevelResult",
    "CostSensitivityReport",
    "CrossAssetReport",
    "DataWindow",
    "EdgeStats",
    "FinalStatus",
    "FoldResult",
    "InMemoryValidationRepository",
    "InitialCandidateFilter",
    "InitialFilterCheck",
    "InitialFilterLimits",
    "InitialFilterReport",
    "MultiTimeframeReport",
    "MultipleTestingReport",
    "ParameterRobustnessChecker",
    "ParameterRobustnessLimits",
    "ParameterRobustnessReport",
    "RandomBaselineResult",
    "RankedStrategy",
    "RankingWeights",
    "RegimeReport",
    "RegimeSlice",
    "SectorSlice",
    "StageResult",
    "StrategyRanker",
    "StrategyValidationEngine",
    "TimeframeResult",
    "ValidationPlan",
    "ValidationRecord",
    "ValidationReport",
    "ValidationRepository",
    "ValidationStage",
    "ValidationWalkForwardValidator",
    "WalkForwardResult",
    "build_benchmark_report",
    "buy_and_hold_curve",
    "compute_edge_stats",
    "cost_sensitivity_report",
    "cross_asset_report",
    "decode_record",
    "deflated_sharpe",
    "encode_record",
    "expected_max_sharpe",
    "final_status_for",
    "multi_timeframe_report",
    "multiple_testing_report",
    "parameter_variants",
    "random_permutation_result",
    "regime_report_from_buckets",
    "slice_bars",
    "slice_bars_by_symbol",
    "sma200_filter_spec",
    "split_windows",
]
