"""AI Risk Gate — the Phase 5 risk engine is the final authority.

The Decision Engine's output is a :class:`DecisionProposal` — an *input* to the
Phase 5 :class:`PortfolioManager`, never a self-authorized order. This adapter
packages the proposal into a ``ProposedTrade`` and returns the authoritative
outcome. It also refuses to route anything while the AI failure monitor reports
a degraded state.
"""

from __future__ import annotations

from decimal import Decimal

from qtrader.application.ai.models import (
    DecisionProposal,
    FailureReport,
    RiskGateResult,
)
from qtrader.application.portfolio_mgmt.manager import PortfolioManager
from qtrader.application.portfolio_mgmt.models import (
    GateVerdict,
    PortfolioSnapshot,
)


class AiRiskGate:
    """Routes AI proposals through the authoritative Phase 5 risk gate."""

    def __init__(
        self,
        portfolio_manager: PortfolioManager,
        *,
        fail_safe_on_degraded: bool = True,
    ) -> None:
        self._manager = portfolio_manager
        self._fail_safe = fail_safe_on_degraded

    def gate(
        self,
        proposal: DecisionProposal,
        snapshot: PortfolioSnapshot,
        *,
        sector: str | None = None,
        atr_pct: float | None = None,
        annualized_vol_pct: float | None = None,
        correlation_to_portfolio: float | None = None,
        failure_report: FailureReport | None = None,
    ) -> RiskGateResult:
        """Run one proposal through the gate; never raises on a rejection."""
        if failure_report is not None and failure_report.degraded and self._fail_safe:
            return RiskGateResult(
                approved=False,
                verdict=GateVerdict.REJECT,
                approved_quantity=None,
                reasons=(f"ai_degraded:{failure_report.reason}",),
                warnings=(),
            )

        cleared = self._manager.propose(
            strategy_id=proposal.strategy_id,
            symbol=proposal.symbol,
            side=proposal.side,
            reference_price=proposal.reference_price,
            quantity=proposal.requested_quantity,
            sector=sector,
            atr_pct=atr_pct,
            annualized_vol_pct=annualized_vol_pct,
            signal_ts=proposal.regime.ts if proposal.regime else None,
            confidence=proposal.confidence,
            correlation_to_portfolio=correlation_to_portfolio,
            snapshot=snapshot,
        )

        if cleared is None:
            return RiskGateResult(
                approved=False,
                verdict=GateVerdict.REJECT,
                approved_quantity=None,
                reasons=("rejected_by_risk_engine",),
            )

        decision = cleared.decision
        approved = cleared.quantity > Decimal("0")
        verdict = (
            GateVerdict.APPROVE
            if approved and decision is not None and decision.verdict is GateVerdict.APPROVE
            else GateVerdict.MODIFY
        )
        return RiskGateResult(
            approved=approved,
            verdict=verdict,
            approved_quantity=cleared.quantity if approved else None,
            reasons=tuple(decision.reasons) if decision else (),
            warnings=tuple(decision.warnings) if decision else (),
            evaluated_exposure_pct=(
                decision.evaluated_exposure_pct if decision is not None else None
            ),
        )


__all__ = ["AiRiskGate"]
