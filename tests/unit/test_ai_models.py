"""Phase 6 — AI model serialization and validation."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from qtrader.application.ai.models import (
    AgentSignal,
    AgentSignalSet,
    DecisionProposal,
    ExecutionAssumptions,
    ExecutionOutcome,
    RegimeAssessment,
    SelectorConfig,
)
from qtrader.application.services.market_regime import MarketRegime, VolatilityRegime
from qtrader.domain.value_objects import Interval, TradeSide

_TS = datetime(2025, 1, 1, tzinfo=UTC)


def test_regime_assessment_round_trips_with_volatility() -> None:
    assessment = RegimeAssessment(
        ts=_TS,
        regime=MarketRegime.BULL,
        confidence=0.8,
        volatility=VolatilityRegime.HIGH,
        trend="bull",
        timeframe=Interval.D1,
    )
    restored = RegimeAssessment.from_dict(assessment.to_dict())
    assert restored == assessment


def test_regime_assessment_round_trips_with_none_volatility() -> None:
    assessment = RegimeAssessment(
        ts=_TS,
        regime=MarketRegime.SIDEWAYS,
        confidence=0.5,
        volatility=None,
        trend="sideways",
        timeframe=Interval.M5,
    )
    restored = RegimeAssessment.from_dict(assessment.to_dict())
    assert restored == assessment
    assert restored.volatility is None


def test_decision_proposal_to_dict() -> None:
    proposal = DecisionProposal(
        strategy_id="s1",
        strategy_version=1,
        symbol="AAPL",
        side=TradeSide.BUY,
        reference_price=Decimal("100"),
        requested_quantity=Decimal("10"),
        confidence=0.5,
        expected_return=0.01,
        expected_risk=0.02,
        agents_involved=("technical",),
        agent_scores={"technical": 0.5},
        regime=None,
        rationale=("r",),
        timeframes=(Interval.D1,),
        expected_direction="long",
    )
    data = proposal.to_dict()
    assert data["side"] == "BUY"
    assert data["reference_price"] == "100"
    assert data["timeframes"] == [Interval.D1.value]
    assert data["regime"] is None


def test_execution_outcome_to_dict() -> None:
    outcome = ExecutionOutcome(
        filled=True,
        fill_rate=1.0,
        rejected_rate=0.0,
        net_return=0.01,
        avg_slippage_bps=2.0,
        commission=Decimal("0.10"),
        scenario="baseline",
        assumptions=ExecutionAssumptions(
            scenario="baseline",
            commission_bps=10.0,
            slippage_bps=2.0,
            max_participation_rate=0.1,
            seed=42,
        ),
    )
    data = outcome.to_dict()
    assert data["commission"] == "0.10"
    assert data["assumptions"]["seed"] == 42


def test_selector_config_validates_non_negative_weights() -> None:
    with pytest.raises(ValueError):
        SelectorConfig(oos_sharpe=-1.0)


def test_selector_config_skips_fold_fraction_validation() -> None:
    SelectorConfig(min_positive_fold_fraction=2.0)


def test_agent_signal_set_by_agent() -> None:
    signals = AgentSignalSet(
        asset="AAPL",
        as_of=_TS,
        signals=(
            AgentSignal("technical", "1", 0.5, 0.5, "r", _TS),
            AgentSignal("news", "1", -0.2, 0.5, "r", _TS),
        ),
    )
    by_agent = signals.by_agent()
    assert set(by_agent) == {"technical", "news"}
