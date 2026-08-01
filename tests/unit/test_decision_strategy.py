"""Unit tests for the ensemble decision strategy."""

from __future__ import annotations

import pytest

from qtrader.application.services.decision_strategy import EnsembleDecisionStrategy
from qtrader.domain.entities import AgentEvidence
from qtrader.domain.value_objects import Decision


def _ev(agent: str, score: float) -> AgentEvidence:
    return AgentEvidence(agent=agent, score=score, reason=f"{agent} signal")


def test_no_evidence_holds() -> None:
    outcome = EnsembleDecisionStrategy().decide([])
    assert outcome.decision is Decision.HOLD
    assert "no signals" in outcome.rationale


def test_strong_buy() -> None:
    outcome = EnsembleDecisionStrategy().decide(
        [
            _ev("technical", 0.8),
            _ev("news", 0.6),
            _ev("fundamental", 0.5),
            _ev("prediction", 0.7),
        ]
    )
    assert outcome.decision is Decision.BUY
    assert outcome.confidence > 0.0
    assert outcome.agent_scores["technical"] == pytest.approx(0.8)


def test_strong_sell() -> None:
    outcome = EnsembleDecisionStrategy().decide(
        [
            _ev("technical", -0.8),
            _ev("news", -0.6),
            _ev("prediction", -0.7),
        ]
    )
    assert outcome.decision is Decision.SELL


def test_conflicting_strong_signals_hold() -> None:
    outcome = EnsembleDecisionStrategy().decide(
        [
            _ev("technical", 0.9),
            _ev("news", -0.9),
            _ev("prediction", 0.8),
        ]
    )
    assert outcome.decision is Decision.HOLD
    assert "conflicting" in outcome.rationale


def test_insufficient_coverage_holds() -> None:
    outcome = EnsembleDecisionStrategy().decide([_ev("prediction", 0.9)])
    assert outcome.decision is Decision.HOLD
    assert "coverage" in outcome.rationale


def test_weak_signal_holds() -> None:
    outcome = EnsembleDecisionStrategy().decide(
        [
            _ev("technical", 0.05),
            _ev("news", 0.05),
            _ev("fundamental", 0.05),
            _ev("prediction", 0.05),
        ]
    )
    assert outcome.decision is Decision.HOLD
    assert "weak" in outcome.rationale


def test_custom_weights_and_thresholds_respected() -> None:
    strategy = EnsembleDecisionStrategy(
        weights={"prediction": 1.0, "technical": 0.0, "news": 0.0, "fundamental": 0.0},
        buy_threshold=0.5,
        conflict_threshold=0.99,
    )
    outcome = strategy.decide([_ev("prediction", 0.8)])
    assert outcome.decision is Decision.BUY
