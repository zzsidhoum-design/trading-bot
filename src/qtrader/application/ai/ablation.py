"""Agent contribution (ablation) testing — does AI add measurable value?

Runs the 8 required ablation cases over per-decision return series and
computes the same risk-adjusted metrics used across the repo (Sharpe, Sortino,
max drawdown, expected value, stability, execution-adjusted return). Each case
is the previous case plus exactly one agent, so the *delta* between consecutive
cases isolates that agent's contribution. The verdict is evidence-based:

- ``keep``   — the agent improved (or held) risk-adjusted performance;
- ``reduce`` — mixed evidence (improved EV but worse Sharpe);
- ``remove`` — the agent meaningfully degraded risk-adjusted performance.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence

from qtrader.application.ai.models import (
    AblationCase,
    AblationMetrics,
    AblationReport,
    AblationResult,
    AgentContribution,
)
from qtrader.application.portfolio_mgmt.metrics import compute_risk_metrics

# The 8 required ablation cases, in additive order.
_BASE = ("technical", "news", "fundamental", "pattern", "prediction", "regime")
ABLATION_CASES: tuple[AblationCase, ...] = (
    AblationCase(name="strategies_only", enabled_agents=()),
    AblationCase(name="with_technical", enabled_agents=_BASE[:1]),
    AblationCase(name="with_news", enabled_agents=_BASE[:2]),
    AblationCase(name="with_fundamental", enabled_agents=_BASE[:3]),
    AblationCase(name="with_pattern", enabled_agents=_BASE[:4]),
    AblationCase(name="with_prediction", enabled_agents=_BASE[:5]),
    AblationCase(name="with_regime", enabled_agents=_BASE[:6]),
    AblationCase(name="full_system", enabled_agents=_BASE),
)

METRIC_KEYS: tuple[str, ...] = (
    "total_return",
    "expected_value",
    "sharpe",
    "sortino",
    "max_drawdown",
    "stability",
    "execution_adjusted_return",
)

REMOVE_SHARPE_DELTA = -0.05


def ablation_metrics(
    returns: Sequence[float],
    *,
    fill_rate: float = 1.0,
    risk_free_rate: float = 0.0,
    periods_per_year: float = 252.0,
) -> AblationMetrics:
    """One case's metrics from its per-decision return series."""
    evaluation = compute_risk_metrics(
        returns,
        periods_per_year=periods_per_year,
        risk_free_rate=risk_free_rate,
    )
    total = float(sum(returns))
    expected_value = float(statistics.fmean(returns)) if returns else 0.0
    win_rate = sum(1 for r in returns if r > 0) / len(returns) if returns else 0.0
    return AblationMetrics(
        total_return=round(total, 6),
        expected_value=round(expected_value, 6),
        sharpe=round(evaluation.sharpe, 6),
        sortino=round(evaluation.sortino, 6),
        max_drawdown=round(evaluation.max_drawdown_pct, 6),
        stability=round(win_rate, 6),
        execution_adjusted_return=round(
            expected_value * max(0.0, min(1.0, fill_rate)), 6
        ),
        trades=len(returns),
        fill_rate=round(max(0.0, min(1.0, fill_rate)), 6),
    )


def run_ablation(
    case_returns: Mapping[str, Sequence[float]],
    *,
    case_fill_rates: Mapping[str, float] | None = None,
    risk_free_rate: float = 0.0,
    periods_per_year: float = 252.0,
) -> AblationReport:
    """Compute metrics and per-agent contributions for every required case."""
    baseline = ABLATION_CASES[0]
    baseline_metrics = _metrics_for(
        baseline, case_returns, case_fill_rates or {}, risk_free_rate, periods_per_year
    )

    results: list[AblationResult] = []
    contributions: list[AgentContribution] = []
    previous_metrics = baseline_metrics
    previous_case = baseline
    for case in ABLATION_CASES:
        metrics = _metrics_for(
            case, case_returns, case_fill_rates or {}, risk_free_rate, periods_per_year
        )
        delta = _delta(previous_metrics, metrics)
        results.append(
            AblationResult(
                case=case,
                metrics=metrics,
                delta=delta,
            )
        )
        if case is not baseline:
            added = [
                agent
                for agent in case.enabled_agents
                if agent not in previous_case.enabled_agents
            ]
            # ``full_system`` repeats the final agent set as a sanity case, so it
            # contributes no new agent and no delta to report.
            if added:
                contributions.append(
                    AgentContribution(
                        agent=added[-1],
                        metric_deltas=delta,
                        verdict=_verdict(delta),
                    )
                )
        previous_metrics = metrics
        previous_case = case

    recommendation = _recommendation(contributions)
    return AblationReport(
        baseline=baseline,
        results=tuple(results),
        contributions=tuple(contributions),
        recommendation=recommendation,
    )


def _metrics_for(
    case: AblationCase,
    case_returns: Mapping[str, Sequence[float]],
    case_fill_rates: Mapping[str, float],
    risk_free_rate: float,
    periods_per_year: float,
) -> AblationMetrics:
    returns = case_returns.get(case.name, ())
    fill_rate = case_fill_rates.get(case.name, 1.0)
    return ablation_metrics(
        returns,
        fill_rate=fill_rate,
        risk_free_rate=risk_free_rate,
        periods_per_year=periods_per_year,
    )


def _delta(
    previous: AblationMetrics,
    current: AblationMetrics,
) -> dict[str, float]:
    return {
        key: round(getattr(current, key) - getattr(previous, key), 6)
        for key in METRIC_KEYS
    }


def _verdict(delta: Mapping[str, float]) -> str:
    sharpe = delta.get("sharpe", 0.0) or 0.0
    ev = delta.get("expected_value", 0.0) or 0.0
    if sharpe >= 0.0 and ev >= 0.0:
        return "keep"
    if sharpe < REMOVE_SHARPE_DELTA:
        return "remove"
    return "reduce"


def _recommendation(contributions: Sequence[AgentContribution]) -> str:
    if not contributions:
        return "no_agents_tested"
    removed = [c.agent for c in contributions if c.verdict == "remove"]
    reduced = [c.agent for c in contributions if c.verdict == "reduce"]
    if removed:
        return (
            f"remove:{','.join(removed)}; "
            f"reduce:{','.join(reduced)}" if reduced else f"remove:{','.join(removed)}"
        )
    if reduced:
        return f"reduce:{','.join(reduced)}"
    return "keep_all"


__all__ = [
    "ABLATION_CASES",
    "METRIC_KEYS",
    "ablation_metrics",
    "run_ablation",
]
