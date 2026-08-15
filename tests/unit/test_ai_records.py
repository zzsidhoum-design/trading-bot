"""Phase 6 — Decision ledger (JSON-lines audit trail) + record builder."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from qtrader.application.ai.models import (
    AiDecisionRecord,
    AssetContext,
    DecisionProposal,
    ExecutionAssumptions,
    ExecutionOutcome,
    FailureEvent,
    FailureSeverity,
    RiskGateResult,
    StrategySelection,
)
from qtrader.application.ai.records import (
    DecisionLedger,
    build_decision_record,
    ledger_stats,
)
from qtrader.application.portfolio_mgmt.models import GateVerdict
from qtrader.domain.value_objects import TradeSide

_TS = datetime(2025, 1, 1, tzinfo=UTC)


def _asset() -> AssetContext:
    return AssetContext(symbol="AAPL", price=Decimal("100"))


def _strategy() -> StrategySelection:
    return StrategySelection("s1", 2, 0.8, ("x",), 0.5)


def _risk(approved: bool = True) -> RiskGateResult:
    return RiskGateResult(
        approved=approved,
        verdict=GateVerdict.APPROVE if approved else GateVerdict.REJECT,
        approved_quantity=Decimal("10") if approved else None,
        reasons=("ok",),
    )


def _execution() -> ExecutionOutcome:
    return ExecutionOutcome(
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


def _proposal() -> DecisionProposal:
    return DecisionProposal(
        strategy_id="s1",
        strategy_version=2,
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
        timeframes=(),
        expected_direction="long",
    )


def _record(decision_id: str = "d1") -> AiDecisionRecord:
    return build_decision_record(
        decision_id=decision_id,
        timestamp=_TS,
        asset=_asset(),
        strategy=_strategy(),
        agents_involved=("technical",),
        agent_signals={"technical": {"score": 0.5}},
        sentiment=None,
        market_regime=None,
        timeframes=("D1",),
        confidence=0.5,
        expected_return=0.01,
        expected_risk=0.02,
        proposed_position_size=Decimal("10"),
        risk=_risk(),
        execution=_execution(),
        failures=(FailureEvent("agent_disagreement", FailureSeverity.WARNING, "m"),),
        proposal=_proposal(),
    )


def test_build_decision_record_maps_risk_verdict() -> None:
    record = _record()
    assert record.risk_approval == "approved"
    assert record.risk_reason == "ok"
    assert record.execution_result is not None
    assert record.failure_events == ("agent_disagreement",)
    assert record.proposal is not None


def test_build_decision_record_not_gated_default() -> None:
    record = build_decision_record(
        decision_id="d2",
        timestamp=_TS,
        asset=_asset(),
        strategy=_strategy(),
        agents_involved=(),
        agent_signals={},
        sentiment=None,
        market_regime=None,
        timeframes=(),
        confidence=None,
        expected_return=None,
        expected_risk=None,
        proposed_position_size=None,
    )
    assert record.risk_approval == "not_gated"
    assert record.execution_assumptions is None


def test_ledger_record_get_count() -> None:
    ledger = DecisionLedger()
    ledger.record(_record("a"))
    ledger.record(_record("b"))
    assert ledger.count() == 2
    assert ledger.get("a") is not None
    assert ledger.get("missing") is None
    assert [r.decision_id for r in ledger.all()] == ["a", "b"]


def test_ledger_all_respects_limit_and_order() -> None:
    ledger = DecisionLedger()
    for name in ("b", "a", "c"):
        ledger.record(_record(name))
    assert [r.decision_id for r in ledger.all(limit=2)] == ["a", "c"]


def test_ledger_write_and_load_round_trip(tmp_path) -> None:
    path = tmp_path / "decisions.jsonl"
    ledger = DecisionLedger()
    ledger.record(_record("a"))
    ledger.record(_record("b"))
    assert ledger.write(path) == 2

    loaded = DecisionLedger(path=path)
    assert loaded.count() == 2
    restored = loaded.get("a")
    assert restored is not None
    assert restored.to_dict() == _record("a").to_dict()


def test_ledger_stats_aggregates() -> None:
    ledger = DecisionLedger()
    ledger.record(_record("a"))
    ledger.record(_record("b"))
    rejected = replace(_record("c"), risk_approval="rejected", risk_reason="no")
    ledger.record(rejected)
    stats = ledger_stats(ledger)
    assert stats.total == 3
    assert stats.proposed == 3
    assert stats.approved == 2
    assert stats.rejected == 1
    assert stats.earliest == _TS
    assert stats.latest == _TS
