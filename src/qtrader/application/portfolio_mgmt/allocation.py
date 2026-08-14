"""Risk-aware capital allocation across validated strategies.

The allocator consumes *research reports* (validation records, execution
reports) — never AI recommendations — and scores each strategy on risk,
drawdown, volatility, correlation, out-of-sample performance, execution
robustness and the current market regime. It explicitly avoids allocating
solely on historical returns.

Method (documented assumptions):
  * only strategies whose final verdict is EXECUTION_ROBUST, EXECUTION_SENSITIVE
    or VALIDATED are eligible for capital;
  * execution quality: 1.0 (ROBUST), 0.75 (SENSITIVE), 0.5 (VALIDATED without
    an execution verdict);
  * raw weight = score, where positive components (Sharpe, Sortino, OOS return,
    execution quality) are offset by penalty components (drawdown, volatility,
    strategy correlation) and scaled by a 0..1 market-regime quality;
  * weights are normalized to a total cap and bounded by max/min weight;
  * strategy failure controls (MONITORED/REDUCED/SUSPENDED) scale each
    allocation via the documented weight factors (SUSPENDED -> 0).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import Decimal

from qtrader.application.portfolio_mgmt.correlation import average_strategy_correlation
from qtrader.application.portfolio_mgmt.metrics import compute_risk_metrics
from qtrader.application.portfolio_mgmt.models import (
    AllocationPolicyConfig,
    AllocationReport,
    DrawdownProtection,
    RiskEvaluation,
    StrategyControlState,
    StrategyScore,
)
from qtrader.application.research.strategy.registry import StrategyStatus
from qtrader.application.research.validation.records import ValidationRecord

_ELIGIBLE = (
    StrategyStatus.EXECUTION_ROBUST,
    StrategyStatus.EXECUTION_SENSITIVE,
    StrategyStatus.VALIDATED,
)

_EXECUTION_QUALITY: dict[StrategyStatus, float] = {
    StrategyStatus.EXECUTION_ROBUST: 1.0,
    StrategyStatus.EXECUTION_SENSITIVE: 0.75,
    StrategyStatus.VALIDATED: 0.5,
}


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _to_float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


class StrategyAllocator:
    """Risk-aware allocator of capital between validated strategies."""

    def __init__(self, config: AllocationPolicyConfig) -> None:
        self._config = config

    @property
    def config(self) -> AllocationPolicyConfig:
        return self._config

    def allocate(
        self,
        strategies: Sequence[ValidationRecord],
        *,
        returns_by_strategy: Mapping[str, Sequence[float]] | None = None,
        regime_quality: Mapping[str, float] | None = None,
        control_states: Mapping[str, StrategyControlState] | None = None,
        protection: DrawdownProtection | None = None,
    ) -> AllocationReport:
        config = self._config
        eligible = [r for r in strategies if r.final_status in _ELIGIBLE]
        control_states = control_states or {}
        returns_by_strategy = returns_by_strategy or {}
        protection = protection or DrawdownProtection()

        scores: list[StrategyScore] = []
        notes: list[str] = []
        if not eligible:
            notes.append("no strategies eligible for allocation")
            return AllocationReport(strategies=(), total_weight_pct=0.0, notes=tuple(notes))

        for record in eligible:
            status = (
                StrategyStatus(record.final_status.value)
                if record.final_status is not None
                else StrategyStatus.VALIDATED
            )
            execution_quality = _EXECUTION_QUALITY.get(status, 0.5)
            summary = record.oos_result.summary if record.oos_result is not None else None
            sharpe = _to_float(summary.sharpe) if summary is not None else None
            sortino = _to_float(summary.sortino) if summary is not None else None
            max_dd = _to_float(summary.max_drawdown) if summary is not None else None
            total_return = _to_float(summary.total_return) if summary is not None else None

            returns = returns_by_strategy.get(record.strategy_id, ())
            vol = _series_volatility(returns) if returns else None
            corr = average_strategy_correlation(returns_by_strategy, record.strategy_id)

            regime = regime_quality.get(record.strategy_id, 1.0) if regime_quality else 1.0

            score = self._score(
                sharpe=sharpe,
                sortino=sortino,
                total_return=total_return,
                max_drawdown=max_dd,
                volatility=vol,
                correlation=corr,
                execution_quality=execution_quality,
                regime=regime,
            )
            rationale = self._rationale(
                sharpe=sharpe,
                sortino=sortino,
                max_drawdown=max_dd,
                volatility=vol,
                correlation=corr,
                execution_quality=execution_quality,
                regime=regime,
                status=status,
            )
            scores.append(
                StrategyScore(
                    strategy_id=record.strategy_id,
                    weight_pct=0.0,
                    score=score,
                    sharpe=sharpe,
                    sortino=sortino,
                    max_drawdown_pct=max_dd,
                    volatility_pct=vol,
                    execution_quality=execution_quality,
                    avg_strategy_correlation=corr,
                    rationale=tuple(rationale),
                )
            )

        weights = self._normalize(
            scores, control_states=control_states, config=config, protection=protection
        )
        scores = [
            replace(score, weight_pct=weight)
            for score, weight in zip(scores, weights, strict=True)
        ]
        notes.append(
            "weights derive from risk-adjusted scores, not historical returns alone"
        )
        if regime_quality:
            notes.append("market-regime quality applied")

        portfolio_risk = self._portfolio_risk(scores, returns_by_strategy, config)
        return AllocationReport(
            strategies=tuple(scores),
            total_weight_pct=sum(s.weight_pct for s in scores),
            risk=portfolio_risk,
            notes=tuple(notes),
        )

    def _score(
        self,
        *,
        sharpe: float | None,
        sortino: float | None,
        total_return: float | None,
        max_drawdown: float | None,
        volatility: float | None,
        correlation: float,
        execution_quality: float,
        regime: float,
    ) -> float:
        config = self._config
        positive = 0.0
        positive += config.sharpe_weight * (_clip(sharpe or 0.0, 0.0, 2.0) / 2.0)
        positive += config.sortino_weight * (_clip(sortino or 0.0, 0.0, 2.0) / 2.0)
        positive += config.oos_return_weight * (_clip(total_return or 0.0, 0.0, 0.5) / 0.5)
        positive += config.execution_weight * execution_quality
        penalties = 0.0
        penalties += config.drawdown_weight * (_clip(max_drawdown or 0.0, 0.0, 0.5) / 0.5)
        penalties += config.volatility_weight * (_clip(volatility or 0.0, 0.0, 0.5) / 0.5)
        penalties += config.correlation_weight * correlation
        return max(0.0, positive - penalties + config.regime_weight * (regime - 0.5))

    def _rationale(
        self,
        *,
        sharpe: float | None,
        sortino: float | None,
        max_drawdown: float | None,
        volatility: float | None,
        correlation: float,
        execution_quality: float,
        regime: float,
        status: StrategyStatus,
    ) -> list[str]:
        parts: list[str] = []
        if sharpe is not None:
            parts.append(f"OOS Sharpe {sharpe:.2f}")
        if sortino is not None:
            parts.append(f"Sortino {sortino:.2f}")
        if max_drawdown is not None:
            parts.append(f"max drawdown {max_drawdown * 100:.1f}%")
        if volatility is not None:
            parts.append(f"volatility {volatility * 100:.1f}%")
        parts.append(f"execution quality {execution_quality:.2f}")
        parts.append(f"strategy correlation {correlation:.2f}")
        parts.append(f"regime quality {regime:.2f}")
        parts.append(f"status {status.value}")
        return parts

    def _normalize(
        self,
        scores: Sequence[StrategyScore],
        *,
        control_states: Mapping[str, StrategyControlState],
        config: AllocationPolicyConfig,
        protection: DrawdownProtection,
    ) -> list[float]:
        weights = [max(s.score, 0.0) for s in scores]
        for i, s in enumerate(scores):
            state = control_states.get(s.strategy_id)
            if state is not None:
                weights[i] *= state.weight_factor(protection)
        total = sum(weights)
        if total <= 0.0:
            return [0.0] * len(scores)
        max_total = min(1.0, len(scores) * config.max_weight_pct)
        target = min(1.0, max_total)
        scaled = [w / total * target for w in weights]
        # Clamp to max_weight_pct and renormalize (single redistribution pass).
        clamped = [min(w, config.max_weight_pct) for w in scaled]
        remainder = target - sum(clamped)
        # Only strategies that already carry weight may receive redistributed
        # budget (a zero-weight strategy, e.g. SUSPENDED, must stay at zero).
        under_cap = [
            i for i, w in enumerate(clamped) if 0.0 < w < config.max_weight_pct
        ]
        if remainder > 1e-9 and under_cap:
            distributable = sum(
                config.max_weight_pct - clamped[i] for i in under_cap
            )
            if distributable > 0:
                for i in under_cap:
                    clamped[i] += remainder * (
                        (config.max_weight_pct - clamped[i]) / distributable
                    )
        return [max(0.0, min(w, config.max_weight_pct)) for w in clamped]

    def _portfolio_risk(
        self,
        scores: Sequence[StrategyScore],
        returns_by_strategy: Mapping[str, Sequence[float]],
        config: AllocationPolicyConfig,
    ) -> RiskEvaluation | None:
        present = [
            (s, returns_by_strategy.get(s.strategy_id))
            for s in scores
            if s.weight_pct > 0.0 and returns_by_strategy.get(s.strategy_id)
        ]
        series = [r for _, r in present if r is not None]
        if not present or len(series) < 2 or len({len(r) for r in series}) != 1:
            return None
        total_weight = sum(score.weight_pct for score, _ in present) or 1e-9
        portfolio_returns = [0.0] * len(series[0])
        for score, returns in present:
            weight = score.weight_pct / total_weight
            if returns is None:
                continue
            for i, r in enumerate(returns):
                portfolio_returns[i] += weight * r
        return compute_risk_metrics(
            portfolio_returns,
            periods_per_year=config.periods_per_year,
            risk_free_rate=config.risk_free_rate,
        )


def _series_volatility(returns: Sequence[float]) -> float | None:
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return float(var ** 0.5)


__all__ = ["StrategyAllocator"]
