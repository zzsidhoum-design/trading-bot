"""Initial candidate filtering — cheap rejection before expensive validation.

Every generated hypothesis is screened on the **development** window only (the
out-of-sample window is never touched here). A candidate is dropped when it
has too few trades, absurd historical performance, an excessive drawdown or
turnover, excessive complexity, a razor-thin parameter spread, dependence on a
single indicator, or unstable results around its own parameter values.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from enum import StrEnum

from qtrader.application.research.strategy.specs import StrategySpec
from qtrader.domain.entities import PerformanceSummary

NARROW_PARAM_EPSILON = 2.0


class InitialFilterCheck(StrEnum):
    MIN_TRADES = "min_trades"
    EXTREME_PERFORMANCE = "extreme_performance"
    DRAWDDOWN = "drawdown"
    TURNOVER = "turnover"
    COMPLEXITY = "complexity"
    NARROW_PARAMS = "narrow_params"
    SINGLE_INDICATOR = "single_indicator"
    PARAM_INSTABILITY = "param_instability"


@dataclass(frozen=True, slots=True)
class InitialFilterLimits:
    """Lightweight screening thresholds (research constants, never tuned on OOS)."""

    min_trades: int = 30
    max_cagr: float = 2.0
    max_total_return: float = 20.0
    max_drawdown: float = -0.5
    max_turnover: float = 20.0
    max_complexity: int = 8
    min_distinct_indicators: int = 2
    max_instability: float = 0.5
    jitter_runs: int = 4


@dataclass(frozen=True, slots=True)
class InitialFilterReport:
    """Per-check verdicts plus a human-readable reason per failure."""

    passed: bool
    checks: dict[str, bool]
    reasons: dict[str, str]
    instability: float | None = None


class InitialCandidateFilter:
    """Screens one strategy against the lightweight development-window rules."""

    def __init__(self, limits: InitialFilterLimits | None = None) -> None:
        self._limits = limits or InitialFilterLimits()

    @property
    def limits(self) -> InitialFilterLimits:
        return self._limits

    def check(
        self,
        spec: StrategySpec,
        summary: PerformanceSummary,
        jittered: list[PerformanceSummary | None] | None = None,
    ) -> InitialFilterReport:
        checks: dict[str, bool] = {
            InitialFilterCheck.MIN_TRADES.value: self._min_trades_ok(summary),
            InitialFilterCheck.EXTREME_PERFORMANCE.value: self._extreme_performance_ok(summary),
            InitialFilterCheck.DRAWDDOWN.value: self._drawdown_ok(summary),
            InitialFilterCheck.TURNOVER.value: self._turnover_ok(summary),
            InitialFilterCheck.COMPLEXITY.value: self._complexity_ok(spec),
            InitialFilterCheck.NARROW_PARAMS.value: self._narrow_params_ok(spec),
            InitialFilterCheck.SINGLE_INDICATOR.value: self._single_indicator_ok(spec),
        }
        reasons: dict[str, str] = {}
        instability: float | None = None
        if jittered:
            instability = self._instability(jittered)
            checks[InitialFilterCheck.PARAM_INSTABILITY.value] = (
                instability <= self._limits.max_instability
            )
        for key, ok in checks.items():
            if not ok:
                reasons[key] = self._reason(key)
        return InitialFilterReport(
            passed=all(checks.values()),
            checks=checks,
            reasons=reasons,
            instability=instability,
        )

    def _min_trades_ok(self, summary: PerformanceSummary) -> bool:
        return (summary.trades_count or 0) >= self._limits.min_trades

    def _extreme_performance_ok(self, summary: PerformanceSummary) -> bool:
        if summary.cagr is not None and float(summary.cagr) > self._limits.max_cagr:
            return False
        return not (
            (summary.trades_count or 0) > 0
            and summary.total_return is not None
            and float(summary.total_return) > self._limits.max_total_return
        )

    def _drawdown_ok(self, summary: PerformanceSummary) -> bool:
        if summary.max_drawdown is None:
            return True
        return float(summary.max_drawdown) >= self._limits.max_drawdown

    def _turnover_ok(self, summary: PerformanceSummary) -> bool:
        if summary.turnover is None:
            return True
        return float(summary.turnover) <= self._limits.max_turnover

    def _complexity_ok(self, spec: StrategySpec) -> bool:
        return spec.complexity <= self._limits.max_complexity

    def _narrow_params_ok(self, spec: StrategySpec) -> bool:
        for entry_condition in spec.entry.conditions:
            for exit_condition in spec.exit.conditions:
                if (
                    entry_condition.feature == exit_condition.feature
                    and entry_condition.ref_feature is None
                    and exit_condition.ref_feature is None
                    and abs(entry_condition.value - exit_condition.value) < NARROW_PARAM_EPSILON
                ):
                    return False
        return True

    def _single_indicator_ok(self, spec: StrategySpec) -> bool:
        used: set[str] = set()
        for condition in list(spec.entry.conditions) + list(spec.exit.conditions):
            if condition.feature != "close":
                used.add(condition.feature)
            if condition.ref_feature:
                used.add(condition.ref_feature)
        return len(used) >= self._limits.min_distinct_indicators

    @staticmethod
    def _instability(jittered: list[PerformanceSummary | None]) -> float:
        sharpes = [
            float(s.sharpe)
            for s in jittered
            if s is not None and s.sharpe is not None
        ]
        if not sharpes:
            return float("inf")
        return statistics.pstdev(sharpes)

    def _reason(self, key: str) -> str:
        reasons = {
            InitialFilterCheck.MIN_TRADES.value: (
                f"too few trades (< {self._limits.min_trades})"
            ),
            InitialFilterCheck.EXTREME_PERFORMANCE.value: (
                f"unrealistic returns (cagr > {self._limits.max_cagr} or "
                f"total return > {self._limits.max_total_return})"
            ),
            InitialFilterCheck.DRAWDDOWN.value: (
                f"drawdown worse than {self._limits.max_drawdown}"
            ),
            InitialFilterCheck.TURNOVER.value: (
                f"turnover above {self._limits.max_turnover}"
            ),
            InitialFilterCheck.COMPLEXITY.value: (
                f"too complex (score > {self._limits.max_complexity})"
            ),
            InitialFilterCheck.NARROW_PARAMS.value: "narrow entry-vs-exit parameter range",
            InitialFilterCheck.SINGLE_INDICATOR.value: (
                f"depends on a single indicator (< {self._limits.min_distinct_indicators})"
            ),
            InitialFilterCheck.PARAM_INSTABILITY.value: (
                f"unstable across nearby params (pstdev > {self._limits.max_instability})"
            ),
        }
        return reasons[key]


__all__ = [
    "InitialCandidateFilter",
    "InitialFilterCheck",
    "InitialFilterLimits",
    "InitialFilterReport",
]
