"""Anti-data-mining checks — flag overfit-looking hypotheses before validation.

A strategy is only advanced to walk-forward/OOS if it passes every robustness
check: bounded complexity, enough trades to be statistically meaningful, no
absurd historical performance, no razor-thin parameter ranges and (when jitter
data is supplied) no parameter instability. Thresholds are research constants,
never tuned on the out-of-sample window.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from qtrader.application.research.strategy.specs import StrategySpec
from qtrader.domain.entities import PerformanceSummary

NARROW_PARAM_EPSILON = 2.0


class RobustnessCheck(StrEnum):
    COMPLEXITY = "complexity"
    MIN_TRADES = "min_trades"
    EXTREME_PERFORMANCE = "extreme_performance"
    NARROW_PARAMS = "narrow_params"
    PARAM_INSTABILITY = "param_instability"


@dataclass(frozen=True, slots=True)
class RobustnessReport:
    """Per-check pass/fail plus the complexity score and instability."""

    passed: bool
    checks: dict[str, bool]
    complexity_score: int
    instability: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": dict(self.checks),
            "complexity_score": self.complexity_score,
            "instability": self.instability,
        }


@dataclass(frozen=True, slots=True)
class RobustnessLimits:
    """Research constants for the anti-data-mining gate."""

    max_complexity: int = 8
    min_trades: int = 30
    max_cagr: float = 2.0
    max_drawdown: float = -0.5
    max_instability: float = 0.5


class RobustnessChecker:
    def __init__(self, limits: RobustnessLimits | None = None) -> None:
        self._limits = limits or RobustnessLimits()

    def check(
        self,
        spec: StrategySpec,
        summary: PerformanceSummary,
        jittered: list[PerformanceSummary | None] | None = None,
    ) -> RobustnessReport:
        checks: dict[str, bool] = {
            RobustnessCheck.COMPLEXITY.value: self._complexity_ok(spec),
            RobustnessCheck.MIN_TRADES.value: self._min_trades_ok(summary),
            RobustnessCheck.EXTREME_PERFORMANCE.value: self._extreme_performance_ok(summary),
            RobustnessCheck.NARROW_PARAMS.value: self._narrow_params_ok(spec),
        }
        instability: float | None = None
        if jittered:
            instability = self._instability(jittered)
            checks[RobustnessCheck.PARAM_INSTABILITY.value] = (
                instability <= self._limits.max_instability
            )
        return RobustnessReport(
            passed=all(checks.values()),
            checks=checks,
            complexity_score=spec.complexity,
            instability=instability,
        )

    def _complexity_ok(self, spec: StrategySpec) -> bool:
        return spec.complexity <= self._limits.max_complexity

    def _min_trades_ok(self, summary: PerformanceSummary) -> bool:
        return (summary.trades_count or 0) >= self._limits.min_trades

    def _extreme_performance_ok(self, summary: PerformanceSummary) -> bool:
        if summary.cagr is not None and float(summary.cagr) > self._limits.max_cagr:
            return False
        if (
            summary.max_drawdown is not None
            and float(summary.max_drawdown) < self._limits.max_drawdown
        ):
            return False
        extreme_return = (
            summary.trades_count
            and summary.trades_count > 0
            and summary.total_return is not None
            and float(summary.total_return) > 20.0
        )
        return not extreme_return

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

    @staticmethod
    def _instability(jittered: list[PerformanceSummary | None]) -> float:
        sharpe_values = [
            float(s.sharpe)
            for s in jittered
            if s is not None and s.sharpe is not None
        ]
        if not sharpe_values:
            return float("inf")
        return statistics.pstdev(sharpe_values)


__all__ = ["RobustnessCheck", "RobustnessChecker", "RobustnessLimits", "RobustnessReport"]
