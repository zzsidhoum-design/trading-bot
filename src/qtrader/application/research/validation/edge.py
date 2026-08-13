"""Statistical edge and multiple-testing correction.

A validated strategy must earn its status in two ways: the full risk/return
picture (:class:`EdgeStats`, never win-rate alone) and a deflated Sharpe that
accounts for the fact that it was selected from many searched hypotheses
(:class:`MultipleTestingReport`). The deflated Sharpe follows the Bailey /
Lopez de Prado approach: subtract the expected best-of-N null Sharpe from the
observed Sharpe and re-derive the probability the edge survives the selection
process.
"""

from __future__ import annotations

import math
import statistics
from decimal import Decimal

from qtrader.application.research.validation.records import (
    EdgeStats,
    MultipleTestingReport,
)
from qtrader.domain.entities import PerformanceSummary

_ANNUALIZATION = 252.0
_EPSILON = 1e-12


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def expected_max_sharpe(n_trials: int, n_samples: int) -> float:
    """Approx. expected best annualized Sharpe among ``n_trials`` null trials.

    Under the null of a zero-mean strategy the annualized Sharpe estimated from
    ``n_samples`` observations has standard error ``sqrt(annualization/n)`` and
    the maximum of ``n_trials`` independent normal draws concentrates around
    ``sqrt(2 ln n_trials)``. ``n_trials`` and ``n_samples`` are floored so the
    estimate stays finite for degenerate inputs.
    """
    trials = max(n_trials, 2)
    samples = max(n_samples, 2)
    return math.sqrt(2.0 * math.log(trials)) * math.sqrt(_ANNUALIZATION / samples)


def deflated_sharpe(
    observed_sharpe: float, n_trials: int, n_samples: int
) -> tuple[float, float]:
    """(deflated Sharpe, probability the edge is real) given the search size."""
    expected_max = expected_max_sharpe(n_trials, n_samples)
    std = math.sqrt(_ANNUALIZATION / max(n_samples, 2))
    if std <= _EPSILON:
        return 0.0, 0.0
    adjusted = (observed_sharpe - expected_max) / std
    return adjusted, _normal_cdf(adjusted)


def multiple_testing_report(
    observed_sharpe: float | None,
    n_trials: int,
    n_return_samples: int,
) -> MultipleTestingReport:
    """Classify the multiple-testing risk of one survivor."""
    if observed_sharpe is None:
        return MultipleTestingReport(
            hypotheses_tested=n_trials,
            n_return_samples=n_return_samples,
            observed_sharpe=None,
            expected_max_sharpe=expected_max_sharpe(n_trials, n_return_samples),
            deflated_sharpe=None,
            prob_real=None,
            risk="high",
        )
    deflated, prob_real = deflated_sharpe(observed_sharpe, n_trials, n_return_samples)
    if prob_real >= 0.95:
        risk = "low"
    elif prob_real >= 0.80:
        risk = "medium"
    else:
        risk = "high"
    return MultipleTestingReport(
        hypotheses_tested=n_trials,
        n_return_samples=n_return_samples,
        observed_sharpe=round(observed_sharpe, 4),
        expected_max_sharpe=round(expected_max_sharpe(n_trials, n_return_samples), 4),
        deflated_sharpe=round(deflated, 4),
        prob_real=round(prob_real, 4),
        risk=risk,
    )


def compute_edge_stats(
    summary: PerformanceSummary,
    trade_pnl_pcts: list[Decimal] | None = None,
    stability_mean_sharpe: float | None = None,
    stability_std_sharpe: float | None = None,
) -> EdgeStats:
    """Full statistical picture of one window's results."""
    pnl_floats = [float(p) for p in (trade_pnl_pcts or [])]
    return EdgeStats(
        expectancy=_opt(summary.expectancy),
        sharpe=_opt(summary.sharpe),
        sortino=_opt(summary.sortino),
        max_drawdown=_opt(summary.max_drawdown),
        profit_factor=_opt(summary.profit_factor),
        win_rate=_opt(summary.win_rate),
        avg_win=_opt(summary.avg_win),
        avg_loss=_opt(summary.avg_loss),
        trades=summary.trades_count or 0,
        turnover=_opt(summary.turnover),
        total_costs=_opt(summary.total_costs),
        trade_return_mean=_mean(pnl_floats),
        trade_return_std=_std(pnl_floats),
        trade_return_skew=_skew(pnl_floats),
        trade_return_kurtosis=_kurtosis(pnl_floats),
        stability_mean_sharpe=stability_mean_sharpe,
        stability_std_sharpe=stability_std_sharpe,
    )


def _opt(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _std(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    return statistics.pstdev(values)


def _skew(values: list[float]) -> float | None:
    if len(values) < 3:
        return None
    mean = statistics.fmean(values)
    std = statistics.pstdev(values)
    if std <= _EPSILON:
        return None
    return sum((v - mean) ** 3 for v in values) / (len(values) * std**3)


def _kurtosis(values: list[float]) -> float | None:
    if len(values) < 4:
        return None
    mean = statistics.fmean(values)
    std = statistics.pstdev(values)
    if std <= _EPSILON:
        return None
    return sum((v - mean) ** 4 for v in values) / (len(values) * std**4) - 3.0


__all__ = [
    "compute_edge_stats",
    "deflated_sharpe",
    "expected_max_sharpe",
    "multiple_testing_report",
]
