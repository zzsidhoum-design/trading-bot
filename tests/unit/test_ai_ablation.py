"""Phase 6 — Agent contribution (ablation) testing, all 8 required cases."""

from __future__ import annotations

from qtrader.application.ai.ablation import (
    ABLATION_CASES,
    METRIC_KEYS,
    ablation_metrics,
    run_ablation,
)


def test_ablation_cases_are_additive_and_complete() -> None:
    assert len(ABLATION_CASES) == 8
    assert ABLATION_CASES[0].name == "strategies_only"
    assert ABLATION_CASES[-1].name == "full_system"
    for previous, current in zip(ABLATION_CASES, ABLATION_CASES[1:], strict=False):
        added = [a for a in current.enabled_agents if a not in previous.enabled_agents]
        # Every case adds exactly one agent, except ``full_system`` which is a
        # sanity re-run of the complete agent set.
        if current.name == "full_system":
            assert current.enabled_agents == previous.enabled_agents
        else:
            assert len(added) == 1


def test_ablation_metrics_matches_expected_series_math() -> None:
    returns = [0.01, -0.005, 0.02, 0.0, 0.015]
    metrics = ablation_metrics(returns, periods_per_year=252)
    assert metrics.trades == 5
    assert metrics.total_return == round(sum(returns), 6)
    assert metrics.expected_value == round(sum(returns) / 5, 6)
    assert metrics.fill_rate == 1.0
    assert metrics.execution_adjusted_return == metrics.expected_value


def test_ablation_metrics_empty_series_degrades() -> None:
    metrics = ablation_metrics([])
    assert metrics.trades == 0
    assert metrics.expected_value == 0.0
    assert metrics.sharpe == 0.0


def test_run_ablation_produces_eight_results_and_six_contributions() -> None:
    returns = [0.01, 0.005, -0.002, 0.012, 0.008]
    case_returns = {case.name: returns for case in ABLATION_CASES}
    report = run_ablation(case_returns)
    assert len(report.results) == 8
    assert len(report.contributions) == 6
    assert [c.agent for c in report.contributions] == [
        "technical",
        "news",
        "fundamental",
        "pattern",
        "prediction",
        "regime",
    ]
    assert all(c.verdict == "keep" for c in report.contributions)
    assert report.recommendation == "keep_all"


def test_contribution_verdict_removes_harmful_agent() -> None:
    good = [0.01, -0.001, 0.01, -0.001, 0.01, -0.001]
    flat = [0.0] * 6
    case_returns = {"strategies_only": good, "with_technical": flat}
    for case in ABLATION_CASES[2:]:
        case_returns[case.name] = flat
    report = run_ablation(case_returns)
    technical = report.contributions[0]
    assert technical.agent == "technical"
    assert technical.verdict == "remove"
    assert "remove:technical" in report.recommendation


def test_delta_is_computed_between_consecutive_cases() -> None:
    returns = [0.01, 0.01, 0.01, -0.01, 0.01]
    case_returns = {case.name: returns for case in ABLATION_CASES}
    report = run_ablation(case_returns)
    for result in report.results[1:]:
        assert set(result.delta) == set(METRIC_KEYS)
