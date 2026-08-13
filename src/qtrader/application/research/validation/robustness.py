"""Robustness dimensions — what happens away from the fitted point.

For every surviving candidate the engine measures (all on the development
window, never the OOS window): behaviour across nearby parameter values,
consistency across timeframe combinations, performance inside each market
regime, generalization across assets/sectors, and edge retention under
realistic execution costs. The checkers here are pure; the engine supplies the
windowed backtests.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, replace
from decimal import Decimal

from qtrader.application.research.strategy.specs import Condition, StrategySpec
from qtrader.application.research.validation.records import (
    AssetSlice,
    CostLevelResult,
    CostSensitivityReport,
    CrossAssetReport,
    MultiTimeframeReport,
    ParameterRobustnessReport,
    RegimeReport,
    RegimeSlice,
    SectorSlice,
    TimeframeResult,
)

MIN_PARAM_ROBUSTNESS_POSITIVE = 0.5
REALISTIC_COMMISSION_BPS = 10.0
REALISTIC_SLIPPAGE_BPS = 50.0


def parameter_variants(
    spec: StrategySpec, max_variants: int = 8, span: int = 2
) -> list[StrategySpec]:
    """Nearby-parameter copies: nudge each numeric threshold by +/- 1..span steps.

    A step is ``max(1.0, 0.25 * |threshold|)``. Only specs with at least one
    numeric threshold produce variants; the result is capped at ``max_variants``.
    """
    targets = [
        condition
        for condition in list(spec.entry.conditions) + list(spec.exit.conditions)
        if condition.ref_feature is None and condition.value is not None
    ]
    variants: list[StrategySpec] = []
    for target in targets:
        step = max(1.0, abs(target.value) * 0.25)
        for k in range(1, span + 1):
            for sign in (1.0, -1.0):
                if len(variants) >= max_variants:
                    return variants
                variants.append(_nudge(spec, target, target.value + sign * k * step))
    return variants


def _nudge(spec: StrategySpec, target: Condition, new_value: float) -> StrategySpec:
    def _remap(condition: Condition) -> Condition:
        if condition is not target:
            return condition
        return replace(condition, value=round(new_value, 6))

    entry = replace(
        spec.entry, conditions=tuple(_remap(c) for c in spec.entry.conditions)
    )
    exit_rule = replace(
        spec.exit, conditions=tuple(_remap(c) for c in spec.exit.conditions)
    )
    return replace(spec, entry=entry, exit=exit_rule)


@dataclass(frozen=True, slots=True)
class ParameterRobustnessLimits:
    """Gate thresholds for the nearby-parameter study."""

    max_instability: float = 0.5
    min_positive_fraction: float = MIN_PARAM_ROBUSTNESS_POSITIVE


class ParameterRobustnessChecker:
    """Judges base + jittered Sharpe outcomes for parameter instability."""

    def __init__(self, limits: ParameterRobustnessLimits | None = None) -> None:
        self._limits = limits or ParameterRobustnessLimits()

    def check(
        self,
        base_sharpe: float | None,
        variant_sharpes: list[float | None],
    ) -> ParameterRobustnessReport:
        sharpes = [
            s for s in [base_sharpe, *variant_sharpes] if s is not None
        ]
        instability: float | None = statistics.pstdev(sharpes) if len(sharpes) >= 2 else None
        positives = [s for s in variant_sharpes if s is not None and s > 0.0]
        denom = len([s for s in variant_sharpes if s is not None])
        positive_fraction = len(positives) / denom if denom else 0.0
        stable = instability is None or instability <= self._limits.max_instability
        robust = positive_fraction >= self._limits.min_positive_fraction
        return ParameterRobustnessReport(
            passed=stable and robust,
            base_sharpe=base_sharpe,
            variant_sharpes=tuple(s for s in variant_sharpes if s is not None),
            instability=instability,
            positive_fraction=round(positive_fraction, 4),
            variants_tested=len(variant_sharpes),
        )


def multi_timeframe_report(
    results: list[TimeframeResult],
) -> MultiTimeframeReport:
    """Aggregate per-interval dev outcomes into a consistency report."""
    if not results:
        return MultiTimeframeReport(
            results=(), best_interval=None, positive_fraction=0.0, consistency_sharpe_std=None
        )
    sharpes = [r.sharpe for r in results if r.sharpe is not None]
    consistency = statistics.pstdev(sharpes) if len(sharpes) >= 2 else None
    positives = [r for r in results if r.sharpe is not None and r.sharpe > 0.0]
    best = max(results, key=lambda r: r.sharpe if r.sharpe is not None else float("-inf"))
    return MultiTimeframeReport(
        results=tuple(results),
        best_interval=best.interval,
        positive_fraction=round(len(positives) / len(results), 4),
        consistency_sharpe_std=consistency,
    )


def regime_report_from_buckets(
    buckets: dict[str, list[Decimal]],
) -> RegimeReport:
    """Turn per-regime trade P/L lists into a :class:`RegimeReport`."""
    slices: list[RegimeSlice] = []
    for regime, pnls in sorted(buckets.items()):
        if not pnls:
            continue
        wins = sum(1 for p in pnls if p > 0)
        total = float(sum(pnls))
        sharpes = [float(p) for p in pnls]
        slices.append(
            RegimeSlice(
                regime=regime,
                trades=len(pnls),
                win_rate=round(wins / len(pnls), 4),
                total_return_pct=round(total, 6),
                sharpe=_sharpe(sharpes),
            )
        )
    best_regime: str | None = None
    worst_regime: str | None = None
    if slices:
        best_regime = max(slices, key=lambda s: s.total_return_pct).regime
        worst_regime = min(slices, key=lambda s: s.total_return_pct).regime
    return RegimeReport(
        slices=tuple(slices), best_regime=best_regime, worst_regime=worst_regime
    )


def _sharpe(pnl_pcts: list[float], annualization: float = 252.0) -> float | None:
    if len(pnl_pcts) < 2:
        return None
    mean = statistics.fmean(pnl_pcts)
    std = statistics.pstdev(pnl_pcts)
    if std == 0.0:
        return None
    return mean / std * math.sqrt(annualization)


def cross_asset_report(
    asset_results: list[tuple[str, str | None, int, float, float | None]],
) -> CrossAssetReport:
    """Aggregate per-symbol dev outcomes, flagging concentration risk.

    ``asset_results`` is ``(symbol, sector, trades, total_return_pct, sharpe)``.
    """
    symbols = tuple(
        AssetSlice(
            symbol=symbol,
            sector=sector,
            trades=trades,
            total_return_pct=round(total_return_pct, 6),
            sharpe=sharpe,
        )
        for symbol, sector, trades, total_return_pct, sharpe in asset_results
    )
    by_sector: dict[str, list[AssetSlice]] = {}
    for slice_ in symbols:
        sector = slice_.sector or "unknown"
        by_sector.setdefault(sector, []).append(slice_)
    sectors = tuple(
        SectorSlice(
            sector=sector,
            symbols=len(group),
            trades=sum(s.trades for s in group),
            total_return_pct=round(sum(s.total_return_pct for s in group), 6),
        )
        for sector, group in sorted(by_sector.items())
    )
    symbols_with_profit = sum(1 for s in symbols if s.total_return_pct > 0)
    sectors_with_profit = sum(1 for s in sectors if s.total_return_pct > 0)
    symbols_tested = len(symbols)
    sectors_tested = len(sectors)
    return CrossAssetReport(
        symbols=symbols,
        sectors=sectors,
        symbols_with_profit=symbols_with_profit,
        symbols_tested=symbols_tested,
        sectors_with_profit=sectors_with_profit,
        sectors_tested=sectors_tested,
        single_symbol_dependence=(
            symbols_tested > 1 and symbols_with_profit <= 1
        ),
        single_sector_dependence=(
            sectors_tested > 1 and sectors_with_profit <= 1
        ),
    )


def cost_sensitivity_report(
    levels: list[CostLevelResult],
) -> CostSensitivityReport:
    """Judge whether the edge survives realistic execution assumptions."""
    realistic = next(
        (
            level
            for level in levels
            if level.commission_bps == REALISTIC_COMMISSION_BPS
            and level.slippage_bps == REALISTIC_SLIPPAGE_BPS
        ),
        levels[-1] if levels else None,
    )
    retained = bool(
        realistic is not None
        and realistic.total_return is not None
        and realistic.total_return > 0.0
        and realistic.profit_factor is not None
        and realistic.profit_factor >= 1.0
    )
    break_even: str | None = None
    for level in levels:
        if level.profit_factor is not None and level.profit_factor >= 1.0:
            break_even = f"{level.commission_bps:g}/{level.slippage_bps:g}bps"
    return CostSensitivityReport(
        levels=tuple(levels),
        edge_retained_at_realistic=retained,
        break_even_level=break_even,
    )


__all__ = [
    "ParameterRobustnessChecker",
    "ParameterRobustnessLimits",
    "cost_sensitivity_report",
    "cross_asset_report",
    "multi_timeframe_report",
    "parameter_variants",
    "regime_report_from_buckets",
]
