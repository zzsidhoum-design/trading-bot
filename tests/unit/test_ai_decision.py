"""Phase 6 — Decision Engine (selection + signals -> auditable proposal)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from qtrader.application.ai.decision import DecisionConfig, DecisionEngine
from qtrader.application.ai.models import (
    AgentSignal,
    AgentSignalSet,
    AgentWeightsConfig,
    AssetContext,
    ProposalVerdict,
    StrategySelection,
)
from qtrader.application.ai.signals import WeightedEnsemble
from qtrader.domain.value_objects import TradeSide
from tests.unit.fakes_portfolio_mgmt import make_validation_record

_TS = datetime(2025, 1, 1, tzinfo=UTC)


def _signal(agent: str, score: float, confidence: float = 0.8) -> AgentSignal:
    return AgentSignal(agent, "1.0", score, confidence, "reason", _TS)


def _signals(*signals: AgentSignal) -> AgentSignalSet:
    return AgentSignalSet(asset="AAPL", as_of=_TS, signals=signals)


def _selection(direction: str = "long") -> StrategySelection:
    return StrategySelection(
        strategy_id="s1",
        strategy_version=1,
        score=0.9,
        reasons=("x",),
        regime_suitability=0.5,
    )


def _record(direction: str = "long") -> object:
    record = make_validation_record("s1")
    spec = replace(record.spec, direction=direction)
    return replace(record, spec=spec)


def _asset(price: str = "100") -> AssetContext:
    return AssetContext(symbol="AAPL", price=Decimal(price))


def _engine(
    *, min_abs: float = 0.15, min_conf: float = 0.0, min_agree: int = 1
) -> DecisionEngine:
    config = DecisionConfig(
        min_ensemble_abs_score=min_abs,
        min_confidence=min_conf,
        min_agreeing_agents=min_agree,
    )
    weights = AgentWeightsConfig(
        version="1.0",
        weights={"technical": 1.0, "news": 1.0, "fundamental": 1.0},
    )
    return DecisionEngine(WeightedEnsemble(weights), config)


@pytest.mark.asyncio
async def test_proposed_for_confident_aligned_signals() -> None:
    signals = _signals(
        _signal("technical", 0.8, 0.8),
        _signal("news", 0.4, 0.8),
    )
    outcome = await _engine().decide(
        strategy=_selection(),
        record=_record(),  # type: ignore[arg-type]
        asset=_asset(),
        signals=signals,
        capital=Decimal("100000"),
    )
    assert outcome.verdict is ProposalVerdict.PROPOSED
    proposal = outcome.proposal
    assert proposal is not None
    assert proposal.side is TradeSide.BUY
    assert proposal.strategy_id == "s1"
    assert proposal.requested_quantity == Decimal("12")
    assert proposal.expected_direction == "long"


@pytest.mark.asyncio
async def test_no_trade_when_ensemble_below_minimum() -> None:
    signals = _signals(_signal("technical", 0.05, 0.8))
    outcome = await _engine().decide(
        strategy=_selection(),
        record=_record(),  # type: ignore[arg-type]
        asset=_asset(),
        signals=signals,
    )
    assert outcome.verdict is ProposalVerdict.NO_TRADE
    assert outcome.proposal is None


@pytest.mark.asyncio
async def test_degraded_when_confidence_below_minimum() -> None:
    signals = _signals(_signal("technical", 0.8, 0.8))
    outcome = await _engine(min_conf=0.9).decide(
        strategy=_selection(),
        record=_record(),  # type: ignore[arg-type]
        asset=_asset(),
        signals=signals,
    )
    assert outcome.verdict is ProposalVerdict.DEGRADED
    assert outcome.proposal is None


@pytest.mark.asyncio
async def test_degraded_when_insufficient_agreeing_agents() -> None:
    signals = _signals(
        _signal("technical", 0.9, 0.8),
        _signal("news", -0.9, 0.8),
        _signal("fundamental", -0.9, 0.8),
    )
    outcome = await _engine(min_agree=3).decide(
        strategy=_selection(),
        record=_record(),  # type: ignore[arg-type]
        asset=_asset(),
        signals=signals,
    )
    assert outcome.verdict is ProposalVerdict.DEGRADED


@pytest.mark.asyncio
async def test_short_strategy_flips_side() -> None:
    signals = _signals(_signal("technical", 0.8, 0.8))
    outcome = await _engine().decide(
        strategy=_selection(),
        record=_record(direction="short"),  # type: ignore[arg-type]
        asset=_asset(),
        signals=signals,
    )
    assert outcome.proposal is not None
    assert outcome.proposal.side is TradeSide.SELL


@pytest.mark.asyncio
async def test_quantity_scales_with_confidence_and_capital() -> None:
    signals = _signals(_signal("technical", 0.8, 1.0))
    outcome = await _engine().decide(
        strategy=_selection(),
        record=_record(),  # type: ignore[arg-type]
        asset=_asset(),
        signals=signals,
        capital=Decimal("50000"),
    )
    assert outcome.proposal is not None
    assert outcome.proposal.requested_quantity == Decimal("8")


@pytest.mark.asyncio
async def test_expectations_come_from_prediction_signal() -> None:
    prediction = AgentSignal(
        "prediction", "model-x-v1", 0.6, 0.8, "p", _TS,
        features={"expected_return": 0.02, "expected_volatility": 0.03},
    )
    signals = _signals(_signal("technical", 0.8, 0.8), prediction)
    outcome = await _engine().decide(
        strategy=_selection(),
        record=_record(),  # type: ignore[arg-type]
        asset=_asset(),
        signals=signals,
    )
    assert outcome.proposal is not None
    assert outcome.proposal.expected_return == 0.02
    assert outcome.proposal.expected_risk == 0.03


@pytest.mark.asyncio
async def test_decision_config_validation() -> None:
    with pytest.raises(ValueError):
        DecisionConfig(position_size_pct=1.5)
    with pytest.raises(ValueError):
        DecisionConfig(leverage=0.0)
