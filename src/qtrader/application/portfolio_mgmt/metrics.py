"""Risk-adjusted evaluation of return series (pure math, documented assumptions).

All percentages are expressed as fractions (0.10 == 10%). The conventions used:

* Expected return — geometric mean of periodic returns, annualised.
* Volatility — population sample standard deviation of periodic returns,
  annualised by ``sqrt(periods_per_year)`` (i.i.d. returns assumption).
* Sharpe — (annualised return - risk-free) / annualised volatility.
* Sortino — (annualised return - risk-free) / downside deviation, where the
  downside deviation uses only the negative excess returns.
* Max drawdown — largest peak-to-trough decline of the (1+r) equity curve.
* VaR 95 — historical 95th percentile loss of the periodic returns.
* Expected shortfall — mean of the losses that exceed the VaR 95 cutoff.

These are statistical assumptions and are documented per evaluation via
``RiskEvaluation.assumptions``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from qtrader.application.portfolio_mgmt.models import RiskEvaluation


def annualized_return(returns: Sequence[float], periods_per_year: float) -> float:
    """Geometric-mean annualised return of a periodic return series."""
    if not returns:
        return 0.0
    product = 1.0
    for r in returns:
        product *= 1.0 + r
    if product <= 0.0:
        return -1.0
    factor = float(product ** (periods_per_year / len(returns)))
    return float(factor - 1.0)


def volatility(returns: Sequence[float], periods_per_year: float) -> float:
    """Sample standard deviation of periodic returns, annualised."""
    n = len(returns)
    if n < 2:
        return 0.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    return math.sqrt(var) * math.sqrt(periods_per_year)


def sharpe_ratio(returns: Sequence[float], periods_per_year: float, risk_free: float) -> float:
    ann = annualized_return(returns, periods_per_year)
    vol = volatility(returns, periods_per_year)
    if vol <= 0.0:
        return 0.0
    return (ann - risk_free) / vol


def sortino_ratio(returns: Sequence[float], periods_per_year: float, risk_free: float) -> float:
    ann = annualized_return(returns, periods_per_year)
    excess = [r - risk_free / periods_per_year for r in returns]
    n = len(excess)
    if n < 2:
        return 0.0
    downside_sq = sum(min(e, 0.0) ** 2 for e in excess) / n
    downside = math.sqrt(downside_sq) * math.sqrt(periods_per_year)
    if downside <= 0.0:
        return 0.0
    return (ann - risk_free) / downside


def max_drawdown(returns: Sequence[float]) -> float:
    """Largest peak-to-trough decline of the compounded equity curve."""
    peak = 1.0
    equity = 1.0
    mdd = 0.0
    for r in returns:
        equity *= 1.0 + r
        if equity > peak:
            peak = equity
        drawdown = 1.0 - equity / peak
        if drawdown > mdd:
            mdd = drawdown
    return mdd


def _sorted_returns(returns: Sequence[float]) -> list[float]:
    return sorted(returns)


def value_at_risk(returns: Sequence[float], confidence: float = 0.95) -> float:
    """Historical VaR: the loss cut at the ``confidence`` quantile (positive)."""
    if not returns:
        return 0.0
    ordered = _sorted_returns(returns)
    index = int(math.ceil((1.0 - confidence) * len(ordered))) - 1
    index = max(0, min(index, len(ordered) - 1))
    return max(0.0, -ordered[index])


def expected_shortfall(returns: Sequence[float], confidence: float = 0.95) -> float:
    """Mean of the worst ``(1-confidence)`` losses (positive number)."""
    if not returns:
        return 0.0
    ordered = _sorted_returns(returns)
    tail = max(1, int(math.ceil((1.0 - confidence) * len(ordered))))
    worst = ordered[:tail]
    mean_loss = -sum(worst) / len(worst)
    return max(0.0, mean_loss)


def average_correlation(returns_by_series: Sequence[Sequence[float]]) -> float:
    """Average pairwise Pearson correlation across equal-length return series.

    Returns 0.0 when fewer than two series or insufficient observations.
    """
    series = [s for s in returns_by_series if len(s) >= 2]
    if len(series) < 2:
        return 0.0
    n = len(series[0])
    if any(len(s) != n for s in series):
        return 0.0
    pairs = 0
    total = 0.0
    for i in range(len(series)):
        for j in range(i + 1, len(series)):
            corr = _pearson(series[i], series[j])
            if corr is not None:
                total += abs(corr)
                pairs += 1
    return total / pairs if pairs else 0.0


def _pearson(a: Sequence[float], b: Sequence[float]) -> float | None:
    n = len(a)
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b, strict=True))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((y - mean_b) ** 2 for y in b)
    denom = math.sqrt(var_a * var_b)
    if denom <= 0.0:
        return None
    return cov / denom


pearson = _pearson


def compute_risk_metrics(
    returns: Sequence[float],
    *,
    periods_per_year: float = 252.0,
    risk_free_rate: float = 0.0,
    var_confidence: float = 0.95,
) -> RiskEvaluation:
    """One :class:`RiskEvaluation` for a periodic return series.

    ``returns`` are per-period arithmetic returns (e.g. daily). If empty or a
    single point, metrics degrade to zero (documented in the assumptions).
    """
    assumptions = (
        "returns are i.i.d. periodic arithmetic returns",
        f"annualization factor sqrt({periods_per_year:g})",
        "VaR/ES are historical quantiles of the periodic return distribution",
        f"VaR confidence {var_confidence:.0%}",
    )
    if len(returns) < 2:
        return RiskEvaluation(
            expected_return_pct=0.0,
            volatility_pct=0.0,
            sharpe=0.0,
            sortino=0.0,
            max_drawdown_pct=max_drawdown(returns),
            var_95_pct=value_at_risk(returns, var_confidence),
            expected_shortfall_pct=expected_shortfall(returns, var_confidence),
            avg_correlation=0.0,
            assumptions=assumptions,
        )
    return RiskEvaluation(
        expected_return_pct=annualized_return(returns, periods_per_year),
        volatility_pct=volatility(returns, periods_per_year),
        sharpe=sharpe_ratio(returns, periods_per_year, risk_free_rate),
        sortino=sortino_ratio(returns, periods_per_year, risk_free_rate),
        max_drawdown_pct=max_drawdown(returns),
        var_95_pct=value_at_risk(returns, var_confidence),
        expected_shortfall_pct=expected_shortfall(returns, var_confidence),
        avg_correlation=average_correlation([returns]),
        assumptions=assumptions,
    )


__all__ = [
    "annualized_return",
    "average_correlation",
    "compute_risk_metrics",
    "expected_shortfall",
    "max_drawdown",
    "pearson",
    "sharpe_ratio",
    "sortino_ratio",
    "value_at_risk",
    "volatility",
]
