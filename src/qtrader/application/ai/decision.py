"""Decision Engine — turns selected strategy + agent signals into a proposal.

The engine is deliberately dumb in a good way: it combines the validated
strategy (the only source of expected edge) with the weighted agent ensemble
(the only source of timing/direction), sizes a *requested* quantity from
``capital`` and a configurable position fraction, and emits a structured
:class:`DecisionProposal`. The Phase 5 risk gate remains the **final
authority** — nothing this module produces is ever executed directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any

from qtrader.application.ai.models import (
    AgentSignalSet,
    AssetContext,
    DecisionProposal,
    ProposalVerdict,
    StrategySelection,
)
from qtrader.application.ai.signals import WeightedEnsemble
from qtrader.application.research.validation.records import ValidationRecord
from qtrader.domain.value_objects import Interval, TradeSide

MIN_PRICE = Decimal("0.000001")


@dataclass(frozen=True, slots=True)
class DecisionConfig:
    """Knobs for the decision engine (all auditable configuration)."""

    min_ensemble_abs_score: float = 0.15
    min_confidence: float = 0.0
    min_agreeing_agents: int = 1
    position_size_pct: float = 0.02
    leverage: float = 1.0

    def __post_init__(self) -> None:
        if self.min_ensemble_abs_score < 0.0:
            raise ValueError("min_ensemble_abs_score must be non-negative")
        if not (0.0 <= self.position_size_pct <= 1.0):
            raise ValueError("position_size_pct must be in [0, 1]")
        if self.leverage <= 0.0:
            raise ValueError("leverage must be positive")


@dataclass(frozen=True, slots=True)
class DecisionOutcome:
    """Full trace of one decision point (for the ledger + failure monitor)."""

    verdict: ProposalVerdict
    ensemble_score: float
    confidence: float
    agreement: float
    weighted: dict[str, float]
    raw: dict[str, float]
    proposal: DecisionProposal | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "ensemble_score": self.ensemble_score,
            "confidence": self.confidence,
            "agreement": self.agreement,
            "weighted": dict(self.weighted),
            "raw": dict(self.raw),
            "proposal": self.proposal.to_dict() if self.proposal else None,
            "reason": self.reason,
        }


def _direction_side(
    ensemble_sign: int,
    strategy_direction: str,
) -> TradeSide:
    """Map ensemble sign + strategy direction to a side.

    A ``short`` strategy expresses bearish timing with a positive ensemble (its
    research semantics are inverted), so the engine flips the side accordingly.
    """
    if strategy_direction == "short":
        return TradeSide.BUY if ensemble_sign < 0 else TradeSide.SELL
    return TradeSide.BUY if ensemble_sign > 0 else TradeSide.SELL


def _conviction(ensemble: float, signals: AgentSignalSet) -> float:
    """Scale ensemble conviction by regime confidence (context, not direction)."""
    if signals.regime is None:
        return abs(ensemble)
    return abs(ensemble) * (0.5 + 0.5 * signals.regime.confidence)


def _agreement(ensemble_sign: int, signals: AgentSignalSet) -> float:
    """Fraction of directional agents agreeing with the ensemble sign."""
    matching = 0
    total = 0
    for signal in signals.signals:
        if signal.score == 0.0:
            continue
        total += 1
        if (signal.score > 0.0) == (ensemble_sign > 0):
            matching += 1
    if total == 0:
        return 0.0
    return matching / total


class DecisionEngine:
    """Composes selection + signals into a structured, auditable proposal."""

    def __init__(
        self,
        ensemble: WeightedEnsemble,
        config: DecisionConfig | None = None,
    ) -> None:
        self._ensemble = ensemble
        self._config = config or DecisionConfig()

    @property
    def config(self) -> DecisionConfig:
        return self._config

    async def decide(
        self,
        *,
        strategy: StrategySelection,
        record: ValidationRecord,
        asset: AssetContext,
        signals: AgentSignalSet,
        capital: Decimal = Decimal("100000"),
    ) -> DecisionOutcome:
        """Produce a proposal for one selected strategy / asset / signal set."""
        cfg = self._config
        ensemble, weighted, raw = self._ensemble.aggregate(signals)
        reasons: list[str] = []

        if abs(ensemble) < cfg.min_ensemble_abs_score:
            return DecisionOutcome(
                verdict=ProposalVerdict.NO_TRADE,
                ensemble_score=round(ensemble, 6),
                confidence=0.0,
                agreement=0.0,
                weighted=weighted,
                raw=raw,
                reason=(
                    f"ensemble_abs={abs(ensemble):.4f} < "
                    f"min={cfg.min_ensemble_abs_score}"
                ),
            )

        ensemble_sign = 1 if ensemble > 0 else -1
        agreement = _agreement(ensemble_sign, signals)
        confidence = _conviction(ensemble, signals)
        if confidence < cfg.min_confidence:
            return DecisionOutcome(
                verdict=ProposalVerdict.DEGRADED,
                ensemble_score=round(ensemble, 6),
                confidence=round(confidence, 6),
                agreement=round(agreement, 6),
                weighted=weighted,
                raw=raw,
                reason=f"confidence={confidence:.4f} < min={cfg.min_confidence}",
            )

        agreeing = sum(
            1
            for s in signals.signals
            if s.score != 0.0 and (s.score > 0.0) == (ensemble_sign > 0)
        )
        if agreeing < cfg.min_agreeing_agents:
            return DecisionOutcome(
                verdict=ProposalVerdict.DEGRADED,
                ensemble_score=round(ensemble, 6),
                confidence=round(confidence, 6),
                agreement=round(agreement, 6),
                weighted=weighted,
                raw=raw,
                reason=(
                    f"agreeing_agents={agreeing} < "
                    f"min={cfg.min_agreeing_agents}"
                ),
            )

        spec = record.spec
        side = _direction_side(ensemble_sign, spec.direction)
        timeframes = self._timeframes(strategy, record)
        expected_return, expected_risk = self._expectations(record, signals)
        quantity = self._quantity(asset, confidence, capital)

        proposal = DecisionProposal(
            strategy_id=strategy.strategy_id,
            strategy_version=strategy.strategy_version,
            symbol=asset.symbol,
            side=side,
            reference_price=asset.price,
            requested_quantity=quantity,
            confidence=round(confidence, 6),
            expected_return=expected_return,
            expected_risk=expected_risk,
            agents_involved=tuple(sorted(s.agent for s in signals.signals)),
            agent_scores=dict(raw),
            regime=signals.regime,
            rationale=tuple(reasons),
            timeframes=timeframes,
            expected_direction=spec.direction,
        )
        reasons.append(
            f"ensemble={ensemble:+.4f} agreement={agreement:.2f} "
            f"confidence={confidence:.3f}"
        )
        return DecisionOutcome(
            verdict=ProposalVerdict.PROPOSED,
            ensemble_score=round(ensemble, 6),
            confidence=round(confidence, 6),
            agreement=round(agreement, 6),
            weighted=weighted,
            raw=raw,
            proposal=proposal,
            reason=" ".join(reasons),
        )

    # ------------------------------------------------------------------ #
    def _timeframes(
        self,
        strategy: StrategySelection,
        record: ValidationRecord,
    ) -> tuple[Interval, ...]:
        intervals = record.spec.timeframes
        return tuple(intervals)

    def _expectations(
        self,
        record: ValidationRecord,
        signals: AgentSignalSet,
    ) -> tuple[float | None, float | None]:
        prediction = signals.by_agent().get("prediction")
        if prediction is not None:
            features = prediction.features
            return (
                features.get("expected_return"),
                features.get("expected_volatility"),
            )
        oos_result = record.oos_result
        if oos_result is None:
            return None, None
        oos = oos_result.summary
        return (
            float(oos.expectancy) if oos.expectancy is not None else None,
            abs(float(oos.max_drawdown)) if oos.max_drawdown is not None else None,
        )

    def _quantity(
        self,
        asset: AssetContext,
        confidence: float,
        capital: Decimal,
    ) -> Decimal:
        if asset.price is None or asset.price <= 0:
            return Decimal("0")
        notional = (
            Decimal(str(capital))
            * Decimal(str(self._config.position_size_pct))
            * Decimal(str(min(1.0, confidence)))
            * Decimal(str(self._config.leverage))
        )
        quantity = (notional / asset.price).quantize(Decimal("1"), rounding=ROUND_DOWN)
        return max(quantity, Decimal("0"))


__all__ = ["DecisionConfig", "DecisionEngine", "DecisionOutcome"]
