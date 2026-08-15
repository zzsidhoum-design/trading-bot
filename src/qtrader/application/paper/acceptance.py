"""Paper-trading acceptance criteria (required output #10).

Criteria are explicitly **not** profit-based — they measure whether the paper
layer behaves like a reliable, controllable execution environment: fill rates,
slippage, latency, risk-control consistency, drawdown, paper-vs-research
divergence, data reliability and failure rates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from qtrader.application.paper.comparison import ComparisonReport
from qtrader.application.paper.models import PaperRunStats
from qtrader.application.paper.telemetry import OperationalSummary


@dataclass(frozen=True, slots=True)
class AcceptanceThresholds:
    """Thresholds for each acceptance criterion (all percentages as fractions)."""

    min_fill_rate: float = 0.90
    max_slippage_bps: float = 50.0
    max_avg_latency_ms: float = 5000.0
    max_drawdown: float = 0.20
    max_paper_research_divergence: float = 0.10
    min_data_reliability: float = 0.95
    max_failure_rate: float = 0.05
    min_uptime_pct: float = 0.95


@dataclass(frozen=True, slots=True)
class AcceptanceCriterion:
    name: str
    description: str
    measured: float | None
    threshold: str
    passed: bool


@dataclass(frozen=True, slots=True)
class AcceptanceResult:
    criteria: tuple[AcceptanceCriterion, ...]
    overall_passed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall_passed": self.overall_passed,
            "criteria": [
                {
                    "name": c.name,
                    "description": c.description,
                    "measured": c.measured,
                    "threshold": c.threshold,
                    "passed": c.passed,
                }
                for c in self.criteria
            ],
        }


def _row_value(report: ComparisonReport, dimension: str) -> tuple[float | None, float | None]:
    for row in report.rows:
        if row.dimension == dimension:
            return row.paper_value, row.divergence
    return None, None


class AcceptanceEvaluator:
    """Evaluate paper-run statistics against the configured thresholds."""

    def __init__(self, thresholds: AcceptanceThresholds) -> None:
        self._thresholds = thresholds

    @property
    def thresholds(self) -> AcceptanceThresholds:
        return self._thresholds

    def evaluate(
        self,
        stats: PaperRunStats,
        operational: OperationalSummary,
        comparison: ComparisonReport,
    ) -> AcceptanceResult:
        t = self._thresholds
        paper_drawdown, _ = _row_value(comparison, "max_drawdown")
        _, return_divergence = _row_value(comparison, "total_return")
        criteria: list[AcceptanceCriterion] = [
            AcceptanceCriterion(
                name="fill_rate",
                description="share of submitted paper orders that filled",
                measured=stats.fill_rate,
                threshold=f">= {t.min_fill_rate:.0%}",
                passed=stats.fill_rate >= t.min_fill_rate,
            ),
            AcceptanceCriterion(
                name="slippage",
                description="average execution slippage in basis points",
                measured=stats.avg_slippage_bps,
                threshold=f"<= {t.max_slippage_bps:.0f} bps",
                passed=stats.avg_slippage_bps <= t.max_slippage_bps,
            ),
            AcceptanceCriterion(
                name="execution_latency",
                description="average submit-to-fill latency in milliseconds",
                measured=stats.avg_execution_latency_ms,
                threshold=f"<= {t.max_avg_latency_ms:.0f} ms",
                passed=stats.avg_execution_latency_ms <= t.max_avg_latency_ms,
            ),
            AcceptanceCriterion(
                name="drawdown",
                description="paper equity drawdown",
                measured=abs(paper_drawdown) if paper_drawdown is not None else None,
                threshold=f"<= {t.max_drawdown:.0%}",
                passed=(
                    abs(paper_drawdown) <= t.max_drawdown
                    if paper_drawdown is not None
                    else True
                ),
            ),
            AcceptanceCriterion(
                name="paper_research_divergence",
                description="total-return divergence between paper and research",
                measured=return_divergence,
                threshold=f"<= {t.max_paper_research_divergence:.0%}",
                passed=(
                    abs(return_divergence) <= t.max_paper_research_divergence
                    if return_divergence is not None
                    else True
                ),
            ),
            AcceptanceCriterion(
                name="data_reliability",
                description="share of data events that were not missing/invalid",
                measured=operational.data_reliability,
                threshold=f">= {t.min_data_reliability:.0%}",
                passed=operational.data_reliability >= t.min_data_reliability,
            ),
            AcceptanceCriterion(
                name="failure_rate",
                description="share of api calls that failed",
                measured=operational.failure_rate,
                threshold=f"<= {t.max_failure_rate:.0%}",
                passed=operational.failure_rate <= t.max_failure_rate,
            ),
        ]
        overall = all(c.passed for c in criteria)
        return AcceptanceResult(criteria=tuple(criteria), overall_passed=overall)


__all__ = [
    "AcceptanceCriterion",
    "AcceptanceEvaluator",
    "AcceptanceResult",
    "AcceptanceThresholds",
]
