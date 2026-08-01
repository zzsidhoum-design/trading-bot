"""Ensemble decision strategy — fuses evidence streams into a final Decision.

Weights are per-signal-source (technical/news/fundamental/prediction). The
strategy enforces HOLD discipline: no evidence, insufficient coverage, or
conflicting strong signals all resolve to HOLD with an explanation.
"""

from __future__ import annotations

from qtrader.domain.entities import AgentEvidence, DecisionOutcome
from qtrader.domain.ports import DecisionStrategy
from qtrader.domain.value_objects import Decision

DEFAULT_WEIGHTS: dict[str, float] = {
    "technical": 0.30,
    "news": 0.25,
    "fundamental": 0.20,
    "prediction": 0.25,
}


def _clip(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


class EnsembleDecisionStrategy(DecisionStrategy):
    def __init__(
        self,
        weights: dict[str, float] | None = None,
        *,
        buy_threshold: float = 0.15,
        sell_threshold: float = -0.15,
        conflict_threshold: float = 0.5,
        min_coverage: float = 0.5,
    ) -> None:
        merged = dict(DEFAULT_WEIGHTS)
        if weights:
            merged.update(weights)
        self._weights = merged
        self._buy = buy_threshold
        self._sell = sell_threshold
        self._conflict = conflict_threshold
        self._min_coverage = min_coverage

    def decide(self, evidence: list[AgentEvidence]) -> DecisionOutcome:
        if not evidence:
            return DecisionOutcome(
                decision=Decision.HOLD,
                confidence=0.0,
                rationale="no signals available",
                agent_scores={},
            )

        total_weights = sum(self._weights.values()) or 1.0
        coverage = sum(self._weights.get(e.agent, 1.0) for e in evidence) / total_weights
        weighted = 0.0
        weight_sum = 0.0
        for e in evidence:
            w = self._weights.get(e.agent, 1.0)
            weighted += w * _clip(e.score)
            weight_sum += w
        score = weighted / weight_sum if weight_sum else 0.0

        strong_positive = [e for e in evidence if e.score >= self._conflict]
        strong_negative = [e for e in evidence if e.score <= -self._conflict]

        parts = [f"ensemble score={score:.3f}, coverage={coverage:.2f}"]
        for e in evidence:
            parts.append(
                f"{e.agent}: {_clip(e.score):+.3f} "
                f"(w={self._weights.get(e.agent, 1.0):.2f}) — {e.reason}"
            )

        notes: list[str] = []
        if strong_positive and strong_negative:
            notes.append("conflicting strong signals -> HOLD")
        elif coverage < self._min_coverage:
            notes.append("insufficient evidence coverage -> HOLD")
        elif not (score >= self._buy or score <= self._sell):
            notes.append("weak ensemble signal -> HOLD")

        decision = Decision.HOLD
        if not notes:
            decision = Decision.BUY if score >= self._buy else Decision.SELL
        if notes:
            parts.append(" | ".join(notes))

        confidence = max(0.0, min(1.0, coverage * (0.4 + 0.6 * abs(score))))
        return DecisionOutcome(
            decision=decision,
            confidence=round(confidence, 4),
            rationale="; ".join(parts),
            agent_scores={e.agent: round(_clip(e.score), 4) for e in evidence},
        )
