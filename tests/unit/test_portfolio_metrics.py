"""Phase 5 — risk-adjusted metrics (Sharpe/Sortino/max drawdown/VaR/ES)."""

from __future__ import annotations

import math

import pytest

from qtrader.application.portfolio_mgmt.metrics import (
    annualized_return,
    average_correlation,
    compute_risk_metrics,
    expected_shortfall,
    max_drawdown,
    sharpe_ratio,
    sortino_ratio,
    value_at_risk,
    volatility,
)


def test_annualized_return_matches_compounding() -> None:
    returns = [0.01] * 252
    assert annualized_return(returns, 252) == pytest.approx(1.01**252 - 1)


def test_annualized_return_empty_is_zero() -> None:
    assert annualized_return([], 252) == 0.0


def test_annualized_return_negative_series_clamps_at_minus_one() -> None:
    returns = [-2.0] * 9  # odd count -> product <= 0
    assert annualized_return(returns, 252) == -1.0


def test_volatility_constant_series_is_zero() -> None:
    returns = [0.01] * 50
    assert volatility(returns, 252) == 0.0


def test_volatility_scales_with_annualization() -> None:
    returns = [0.01, -0.01] * 100
    assert volatility(returns, 252) == pytest.approx(volatility(returns, 4) * math.sqrt(63))


def test_sharpe_zero_vol_is_zero() -> None:
    assert sharpe_ratio([0.01] * 50, 252, 0.0) == 0.0


def test_sharpe_positive_for_positive_mean() -> None:
    returns = [0.005, 0.005, -0.001, 0.006, 0.004, -0.002] * 20
    assert sharpe_ratio(returns, 252, 0.0) > 0.0


def test_sortino_differs_from_sharpe_with_upside_volatility() -> None:
    a = [0.01] * 100 + [-0.01] * 50
    b = [0.05] * 100 + [-0.01] * 50
    s_a = sortino_ratio(a, 252, 0.0)
    s_b = sortino_ratio(b, 252, 0.0)
    sh_a = sharpe_ratio(a, 252, 0.0)
    assert s_a >= 0.0
    assert s_b >= 0.0
    # More upside variance penalises Sharpe but not Sortino.
    assert s_b > sh_a


def test_max_drawdown_detects_peak_to_trough() -> None:
    returns = [0.10, 0.10, -0.50, 0.10, -0.90]
    assert max_drawdown(returns) == pytest.approx(0.945, abs=1e-9)


def test_max_drawdown_monotonic_uptrend_is_zero() -> None:
    assert max_drawdown([0.01] * 100) == 0.0


def test_value_at_risk_historical_quantile() -> None:
    returns = [-0.05, -0.04, -0.03, -0.02, -0.01, 0.0, 0.01, 0.02, 0.03, 0.05]
    assert value_at_risk(returns, 0.95) == pytest.approx(0.05)


def test_expected_shortfall_means_the_worst_tail() -> None:
    returns = [-0.05, -0.04, -0.03, -0.02, -0.01, 0.0, 0.01, 0.02, 0.03, 0.05]
    es = expected_shortfall(returns, 0.95)
    assert es == pytest.approx(0.05)
    assert es >= value_at_risk(returns, 0.95)


def test_compute_risk_metrics_returns_evaluation() -> None:
    returns = [0.01 if i % 3 else -0.005 for i in range(252)]
    evaluation = compute_risk_metrics(returns, periods_per_year=252, risk_free_rate=0.0)
    assert evaluation.expected_return_pct > 0.0
    assert evaluation.volatility_pct > 0.0
    assert evaluation.sharpe > 0.0
    assert evaluation.max_drawdown_pct >= 0.0
    assert evaluation.var_95_pct >= 0.0
    assert evaluation.expected_shortfall_pct >= evaluation.var_95_pct
    assert evaluation.assumptions


def test_compute_risk_metrics_single_point_degrades_gracefully() -> None:
    evaluation = compute_risk_metrics([0.01], periods_per_year=252)
    assert evaluation.volatility_pct == 0.0
    assert evaluation.sharpe == 0.0


def test_average_correlation_identical_series_is_one() -> None:
    series = [[0.01, -0.01] * 10, [0.01, -0.01] * 10]
    assert average_correlation(series) == pytest.approx(1.0, abs=1e-9)


def test_average_correlation_inverse_series_is_one_abs() -> None:
    series = [[0.01, -0.01] * 10, [-0.01, 0.01] * 10]
    assert average_correlation(series) == pytest.approx(1.0, abs=1e-9)


def test_average_correlation_insufficient_data_is_zero() -> None:
    assert average_correlation([]) == 0.0
    assert average_correlation([[0.01]]) == 0.0
