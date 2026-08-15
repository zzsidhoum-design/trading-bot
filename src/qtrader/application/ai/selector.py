"""Strategy Selector — multi-factor ranking over Phase 3/4 validated strategies.

Selection uses OOS performance, walk-forward stability, execution-robustness,
cross-asset dependence, regime fit and complexity. **Historical return alone
never determines selection** — every factor is bounded into [0, 1] and the
final score is the weighted average across the configured factor weights.

Hard exclusions (never selectable):
- status outside VALIDATED / EXECUTION_SENSITIVE / EXECUTION_ROBUST;
- any strategy in the ``suspended`` set (Phase 5 control status);
- strategies whose market-regime operating conditions cannot be verified
  against the current snapshot (missing features / cross-bar operators);
- strategies whose walk-forward positive-fold fraction is below the config
  threshold;
- strategies missing an OOS result (nothing to rank).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from qtrader.application.ai.models import (
    ExcludedStrategy,
    RegimeAssessment,
    SelectorConfig,
    SelectorReport,
    StrategySelection,
)
from qtrader.application.research.strategy.specs import Operator, StrategySpec
from qtrader.application.research.validation.records import (
    FinalStatus,
    ValidationRecord,
)

ELIGIBLE_STATUSES = frozenset(
    {
        FinalStatus.VALIDATED,
        FinalStatus.EXECUTION_SENSITIVE,
        FinalStatus.EXECUTION_ROBUST,
    }
)


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _factor(value: float | Decimal | None, cap: float, scale: float = 1.0) -> float:
    if value is None:
        return 0.5
    return _clip(float(value) / (cap * scale), 0.0, 1.0)


def _evaluate_regime_conditions(
    spec: StrategySpec,
    features: dict[str, float],
) -> tuple[bool, str]:
    """Verify the strategy's operating conditions against the snapshot.

    Returns ``(satisfied, reason)``. Any unverifiable condition fails safe.
    """
    if spec.regime is None or not spec.regime.conditions:
        return True, ""
    for condition in spec.regime.conditions:
        if condition.op in (Operator.CROSS_ABOVE, Operator.CROSS_BELOW):
            return False, "regime_cross_condition_unverifiable"
        if condition.ref_feature is not None:
            if condition.feature not in features or condition.ref_feature not in features:
                return False, "regime_feature_missing"
            left = features[condition.feature]
            right = features[condition.ref_feature]
        else:
            if condition.feature not in features:
                return False, "regime_feature_missing"
            left = features[condition.feature]
            right = condition.value
        ok = {
            Operator.GT: left > right,
            Operator.LT: left < right,
            Operator.GE: left >= right,
            Operator.LE: left <= right,
        }.get(condition.op)
        if ok is None:
            return False, "regime_operator_unverifiable"
        if not ok:
            return False, "regime_condition_violated"
    return True, ""


class StrategySelector:
    """Deterministic multi-factor selector over validated strategies."""

    def __init__(self, config: SelectorConfig | None = None) -> None:
        self._config = config or SelectorConfig()

    @property
    def config(self) -> SelectorConfig:
        return self._config

    def select(
        self,
        records: Sequence[ValidationRecord],
        *,
        regime: RegimeAssessment | None = None,
        features: dict[str, float] | None = None,
        suspended: set[str] | frozenset[str] = frozenset(),
        as_of: datetime | None = None,
    ) -> SelectorReport:
        """Rank every eligible strategy; never raise on a bad record."""
        as_of = as_of or datetime.now(UTC)
        excluded: list[ExcludedStrategy] = []
        selections: list[StrategySelection] = []

        for record in records:
            spec = record.spec
            strategy_id = record.strategy_id

            if record.final_status not in ELIGIBLE_STATUSES:
                excluded.append(
                    ExcludedStrategy(
                        strategy_id,
                        f"status:{record.final_status.value if record.final_status else 'none'}",
                    )
                )
                continue
            if strategy_id in suspended:
                excluded.append(ExcludedStrategy(strategy_id, "suspended"))
                continue
            if record.oos_result is None:
                excluded.append(ExcludedStrategy(strategy_id, "missing_oos"))
                continue

            satisfied, reason = _evaluate_regime_conditions(spec, features or {})
            if not satisfied:
                excluded.append(ExcludedStrategy(strategy_id, reason))
                continue

            wf = record.wf_result
            pos_fold = wf.positive_fold_fraction if wf else None
            if pos_fold is not None and pos_fold < self._config.min_positive_fold_fraction:
                excluded.append(ExcludedStrategy(strategy_id, "walk_forward_unstable"))
                continue

            factors, reasons = self._factors(record, regime, as_of)
            score = self._weighted_score(factors)
            selections.append(
                StrategySelection(
                    strategy_id=strategy_id,
                    strategy_version=spec.version,
                    score=round(score, 6),
                    reasons=tuple(reasons),
                    regime_suitability=round(factors["volatility_match"], 6),
                )
            )

        selections.sort(key=lambda s: s.score, reverse=True)
        return SelectorReport(
            as_of=as_of,
            regime=regime,
            selections=tuple(selections),
            excluded=tuple(excluded),
        )

    # ------------------------------------------------------------------ #
    def _factors(
        self,
        record: ValidationRecord,
        regime: RegimeAssessment | None,
        as_of: datetime,
    ) -> tuple[dict[str, float], list[str]]:
        cfg = self._config
        spec = record.spec
        oos_result = record.oos_result
        if oos_result is None:
            return {}, []
        oos = oos_result.summary
        wf = record.wf_result
        factors: dict[str, float] = {}
        reasons: list[str] = []

        sharpe = _factor(oos.sharpe, cfg.oos_sharpe, 2.0)
        factors["oos_sharpe"] = sharpe
        reasons.append(f"oos_sharpe={oos.sharpe}")

        cagr = oos.cagr if oos.cagr is not None else oos.total_return
        ret = _factor(cagr, 0.5) if oos.cagr is not None else _factor(oos.total_return, 2.0)
        factors["oos_return"] = ret
        reasons.append(f"oos_return={oos.cagr}")

        sortino = _factor(oos.sortino, cfg.oos_sortino, 2.0)
        factors["oos_sortino"] = sortino
        reasons.append(f"oos_sortino={oos.sortino}")

        if wf is not None and wf.stability_std_sharpe is not None:
            std_factor = _clip(1.0 - wf.stability_std_sharpe, 0.0, 1.0)
            pos = wf.positive_fold_fraction if wf.positive_fold_fraction is not None else 0.0
            stability = 0.5 * pos + 0.5 * std_factor
        else:
            stability = 0.5
        factors["stability"] = stability
        reasons.append(f"wf_stability={wf.positive_fold_fraction if wf else None}")

        factors["execution"] = self._execution_factor(record)
        status = record.final_status.value if record.final_status else "none"
        reasons.append(f"execution_status={status}")

        age_days = max((as_of - record.created_at).total_seconds() / 86400.0, 0.0)
        factors["recent"] = _clip(1.0 - age_days / 365.0, 0.05, 1.0)

        factors["volatility_match"] = self._volatility_match(spec, regime)
        factors["cross_asset"] = self._cross_asset_factor(record)
        factors["correlation"] = self._correlation_factor(record)
        factors["risk"] = 1.0 - _factor(oos.max_drawdown, 0.5)
        factors["regime"] = self._regime_factor(record, regime)
        factors["complexity"] = _clip(1.0 - spec.complexity / 10.0, 0.0, 1.0)

        reasons.append(f"complexity={spec.complexity}")
        return factors, reasons

    def _execution_factor(self, record: ValidationRecord) -> float:
        report = record.execution_report
        if report is None or not report.scenarios:
            return 0.8
        baseline = next(
            (s.metrics for s in report.scenarios if s.scenario.value == "baseline"),
            None,
        )
        if baseline is None:
            return 0.8
        fill = _clip(baseline.fill_rate, 0.0, 1.0)
        return 0.5 * fill + 0.5 * _clip(1.0 - baseline.rejected_rate, 0.0, 1.0)

    def _volatility_match(self, spec: StrategySpec, regime: RegimeAssessment | None) -> float:
        if regime is None:
            return 0.5
        if regime.timeframe in spec.timeframes:
            return 1.0
        return 0.5

    def _cross_asset_factor(self, record: ValidationRecord) -> float:
        report = record.cross_asset_report
        if report is None or not report.symbols_tested:
            return 0.5
        return _clip(
            report.symbols_with_profit / report.symbols_tested,
            0.0,
            1.0,
        )

    def _correlation_factor(self, record: ValidationRecord) -> float:
        report = record.cross_asset_report
        if report is None:
            return 0.5
        penalty = 0.0
        if report.single_symbol_dependence:
            penalty += 0.6
        if report.single_sector_dependence:
            penalty += 0.3
        return _clip(0.8 - penalty, 0.0, 1.0)

    def _regime_factor(self, record: ValidationRecord, regime: RegimeAssessment | None) -> float:
        report = record.regime_report
        if regime is None or report is None or report.best_regime is None:
            return 0.5
        return 1.0 if report.best_regime == regime.regime.value else 0.6

    def _weighted_score(self, factors: dict[str, float]) -> float:
        cfg = self._config
        weights = {
            "oos_sharpe": cfg.oos_sharpe,
            "oos_return": cfg.oos_return,
            "oos_sortino": cfg.oos_sortino,
            "stability": cfg.stability,
            "execution": cfg.execution,
            "recent": cfg.recent,
            "volatility_match": cfg.volatility_match,
            "cross_asset": cfg.cross_asset,
            "risk": cfg.risk,
            "correlation": cfg.correlation,
            "regime": cfg.regime,
            "complexity": cfg.complexity,
        }
        total = sum(weights.values())
        if total == 0.0:
            return 0.0
        return sum(factors.get(name, 0.0) * weight for name, weight in weights.items()) / total


__all__ = ["ELIGIBLE_STATUSES", "StrategySelector"]
