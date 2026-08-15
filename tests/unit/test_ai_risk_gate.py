"""Phase 6 — AI Risk Gate (Phase 5 risk engine is the final authority)."""

from __future__ import annotations

from decimal import Decimal

from qtrader.application.ai.models import (
    DecisionProposal,
    FailureReport,
)
from qtrader.application.ai.risk_gate import AiRiskGate
from qtrader.application.portfolio_mgmt.allocation import StrategyAllocator
from qtrader.application.portfolio_mgmt.drawdown import DrawdownGuard, KillSwitch
from qtrader.application.portfolio_mgmt.engine import PortfolioRiskEngine
from qtrader.application.portfolio_mgmt.manager import PortfolioManager
from qtrader.application.portfolio_mgmt.models import (
    AllocationPolicyConfig,
    DrawdownProtection,
    GateVerdict,
    PortfolioConstraints,
    PortfolioSnapshot,
    SizingPolicy,
    snapshot_from_state,
)
from qtrader.domain.value_objects import TradeSide


def _manager() -> PortfolioManager:
    return PortfolioManager(
        engine=PortfolioRiskEngine(
            constraints=PortfolioConstraints(),
            drawdown_protection=DrawdownProtection(),
            sizing_policy=SizingPolicy(),
            kill_switch=KillSwitch(),
        ),
        allocator=StrategyAllocator(AllocationPolicyConfig()),
        drawdown_guard=DrawdownGuard(DrawdownProtection()),
    )


def _snapshot(**overrides: object) -> PortfolioSnapshot:
    params = dict(equity=Decimal("100000"), cash=Decimal("100000"), gross_exposure_pct=0.0)
    params.update(overrides)
    return snapshot_from_state(**params)


def _proposal(quantity: str = "100") -> DecisionProposal:
    return DecisionProposal(
        strategy_id="s1",
        strategy_version=1,
        symbol="AAPL",
        side=TradeSide.BUY,
        reference_price=Decimal("100"),
        requested_quantity=Decimal(quantity),
        confidence=0.5,
        expected_return=0.01,
        expected_risk=0.02,
        agents_involved=("technical",),
        agent_scores={"technical": 0.5},
        regime=None,
        rationale=("ensemble",),
        timeframes=(),
        expected_direction="long",
    )


def test_gate_approves_proposal() -> None:
    gate = AiRiskGate(_manager())
    result = gate.gate(_proposal(), _snapshot())
    assert result.approved is True
    assert result.verdict is GateVerdict.APPROVE
    assert result.approved_quantity is not None
    assert result.approved_quantity > 0


def test_gate_rejects_when_ai_degraded_and_fail_safe() -> None:
    gate = AiRiskGate(_manager())
    report = FailureReport(degraded=True, reason="agent_disagreement")
    result = gate.gate(_proposal(), _snapshot(), failure_report=report)
    assert result.approved is False
    assert result.verdict is GateVerdict.REJECT
    assert "ai_degraded" in result.reasons[0]


def test_gate_ignores_failure_report_when_fail_safe_disabled() -> None:
    gate = AiRiskGate(_manager(), fail_safe_on_degraded=False)
    report = FailureReport(degraded=True, reason="agent_disagreement")
    result = gate.gate(_proposal(), _snapshot(), failure_report=report)
    assert result.approved is True


def test_gate_rejects_when_risk_engine_rejects() -> None:
    gate = AiRiskGate(_manager())
    over_exposed = _snapshot(equity=Decimal("100"), cash=Decimal("0"), gross_exposure_pct=0.95)
    result = gate.gate(_proposal(quantity="999999"), over_exposed)
    assert result.approved is False
    assert result.verdict is GateVerdict.REJECT
