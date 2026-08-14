"""Correlation & concentration monitoring (asset, sector, strategy, portfolio).

Key principle: multiple strategies do NOT diversify if they hold highly
correlated positions. These tools compute (a) per-symbol average correlation to
the existing book, (b) sector exposures, (c) normalized Herfindahl–Hirschman
concentration indices, and (d) the fraction of the book held in assets that
correlate above the configured threshold.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from qtrader.application.portfolio_mgmt.metrics import pearson
from qtrader.application.portfolio_mgmt.models import (
    PortfolioSnapshot,
    PositionSize,
    ProposedTrade,
)

# correlation source: caller supplies symbol pair correlations (e.g. computed
# from the price history window). Mapping[symbol, mapping[other_symbol, corr]].
CorrelationProvider = Callable[[str, Sequence[str]], Mapping[str, float]]


def concentration_index(weights: Sequence[float]) -> float:
    """Normalized Herfindahl–Hirschman concentration in [0, 1].

    Returns 0 for a perfectly diversified (flat) book and 1 for a fully
    concentrated one. Uses the normalized HHI = (H - 1/n) / (1 - 1/n).
    """
    positive = [w for w in weights if w > 0]
    if not positive:
        return 0.0
    total = sum(positive)
    if total <= 0:
        return 0.0
    shares = [w / total for w in positive]
    hhi = sum(s * s for s in shares)
    n = len(shares)
    if n <= 1:
        return 1.0
    return (hhi - 1.0 / n) / (1.0 - 1.0 / n)


def sector_exposures(snapshot: PortfolioSnapshot) -> dict[str, float]:
    """Weighted exposure per sector (fraction of equity)."""
    out: dict[str, float] = {}
    for holding in snapshot.positions:
        sector = holding.sector or "unknown"
        out[sector] = out.get(sector, 0.0) + holding.weight_pct
    return out


def proposed_sector_exposure(
    snapshot: PortfolioSnapshot,
    trade: ProposedTrade,
    size: PositionSize,
) -> dict[str, float]:
    """Sector exposures after the proposed trade is added."""
    exposures = sector_exposures(snapshot)
    sector = trade.sector or "unknown"
    exposures[sector] = exposures.get(sector, 0.0) + size.weight_pct
    return exposures


def correlated_exposure(
    snapshot: PortfolioSnapshot,
    correlation_provider: CorrelationProvider,
    threshold: float = 0.70,
) -> float:
    """Fraction of equity in holdings whose average correlation to the rest of
    the book is at or above ``threshold``."""
    symbols = [h.symbol for h in snapshot.positions]
    if not symbols:
        return 0.0
    correlated_weight = 0.0
    for holding in snapshot.positions:
        others = [s for s in symbols if s != holding.symbol]
        if not others:
            continue
        corrs = correlation_provider(holding.symbol, others)
        avg = _mean([abs(v) for v in corrs.values()])
        if avg >= threshold:
            correlated_weight += holding.weight_pct
    return correlated_weight


def proposed_correlated_exposure(
    snapshot: PortfolioSnapshot,
    trade: ProposedTrade,
    size: PositionSize,
    correlation_provider: CorrelationProvider,
    threshold: float = 0.70,
) -> float:
    """Correlated exposure after the proposed trade is added.

    Uses the trade's pre-computed ``correlation_to_portfolio`` when provided
    (the engine supplies it from the correlation provider), otherwise queries
    the provider against every holding.
    """
    current = correlated_exposure(snapshot, correlation_provider, threshold)
    if size.weight_pct <= 0.0:
        return current
    if trade.correlation_to_portfolio is not None and trade.correlation_to_portfolio >= threshold:
        return current + size.weight_pct
    holdings = snapshot.positions
    if not holdings:
        return current + size.weight_pct
    corrs = correlation_provider(trade.symbol, [h.symbol for h in holdings])
    avg = _mean([abs(v) for v in corrs.values()])
    if avg >= threshold:
        return current + size.weight_pct
    return current


def portfolio_concentration(snapshot: PortfolioSnapshot) -> float:
    """Concentration of the current book (normalized HHI of weights)."""
    return concentration_index([h.weight_pct for h in snapshot.positions])


def projected_concentration(
    snapshot: PortfolioSnapshot,
    size: PositionSize,
) -> float:
    """Concentration if the proposed size is added as a new position."""
    weights = [h.weight_pct for h in snapshot.positions] + [size.weight_pct]
    return concentration_index(weights)


def strategy_correlation(
    returns_by_strategy: Mapping[str, Sequence[float]],
    strategy_a: str,
    strategy_b: str,
) -> float | None:
    """Pearson correlation between two strategies' return series."""
    a = returns_by_strategy.get(strategy_a)
    b = returns_by_strategy.get(strategy_b)
    if a is None or b is None or len(a) < 2 or len(a) != len(b):
        return None
    return pearson(a, b)


def average_strategy_correlation(
    returns_by_strategy: Mapping[str, Sequence[float]],
    strategy_id: str,
) -> float:
    """Average |correlation| of one strategy vs every other strategy."""
    other_ids = [s for s in returns_by_strategy if s != strategy_id]
    if not other_ids:
        return 0.0
    corrs = [
        strategy_correlation(returns_by_strategy, strategy_id, other)
        for other in other_ids
    ]
    present = [c for c in corrs if c is not None]
    if not present:
        return 0.0
    return _mean([abs(c) for c in present])


def _mean(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


__all__ = [
    "CorrelationProvider",
    "average_strategy_correlation",
    "concentration_index",
    "correlated_exposure",
    "portfolio_concentration",
    "projected_concentration",
    "proposed_correlated_exposure",
    "proposed_sector_exposure",
    "sector_exposures",
    "strategy_correlation",
]
