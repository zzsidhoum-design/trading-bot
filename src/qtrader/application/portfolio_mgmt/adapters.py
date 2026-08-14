"""Phase 5 adapter — a thin, dependency-injected seam over the risk engine and
portfolio manager (no agent imports, no I/O)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from qtrader.application.portfolio_mgmt.manager import PortfolioManager
from qtrader.application.portfolio_mgmt.models import (
    AllocationReport,
    ClearedOrder,
    GateDecision,
    KillSwitchRecord,
    MonitoringReport,
    PortfolioSnapshot,
    ProposedTrade,
    StrategyControlState,
)
from qtrader.application.research.validation.records import ValidationRecord
from qtrader.domain.value_objects import TradeSide

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from qtrader.application.portfolio_mgmt.engine import PortfolioRiskEngine


@dataclass(frozen=True, slots=True)
class PortfolioRiskAdapter:
    """Application seam: portfolio risk gating, allocation and monitoring."""

    manager: PortfolioManager
    engine: PortfolioRiskEngine

    def gate(self, trade: ProposedTrade, snapshot: PortfolioSnapshot) -> GateDecision:
        return self.manager.gate(trade, snapshot)

    def propose(
        self,
        *,
        strategy_id: str,
        symbol: str,
        side: TradeSide,
        reference_price: Decimal,
        quantity: Decimal,
        sector: str | None = None,
        atr_pct: float | None = None,
        annualized_vol_pct: float | None = None,
        limit_price: Decimal | None = None,
        stop_loss: Decimal | None = None,
        signal_ts: datetime | None = None,
        confidence: float | None = None,
        correlation_to_portfolio: float | None = None,
        snapshot: PortfolioSnapshot,
    ) -> ClearedOrder | None:
        return self.manager.propose(
            strategy_id=strategy_id,
            symbol=symbol,
            side=side,
            reference_price=reference_price,
            quantity=quantity,
            sector=sector,
            atr_pct=atr_pct,
            annualized_vol_pct=annualized_vol_pct,
            limit_price=limit_price,
            stop_loss=stop_loss,
            signal_ts=signal_ts,
            confidence=confidence,
            correlation_to_portfolio=correlation_to_portfolio,
            snapshot=snapshot,
        )

    def allocate(
        self,
        strategies: Sequence[ValidationRecord],
        *,
        returns_by_strategy: Mapping[str, Sequence[float]] | None = None,
        regime_quality: Mapping[str, float] | None = None,
        control_states: Mapping[str, StrategyControlState] | None = None,
    ) -> AllocationReport:
        return self.manager.allocate(
            strategies,
            returns_by_strategy=returns_by_strategy,
            regime_quality=regime_quality,
            control_states=control_states,
        )

    def monitor(
        self,
        *,
        snapshot: PortfolioSnapshot,
        strategy_drawdowns: Mapping[str, float],
        strategy_losses: Mapping[str, int] | None = None,
        current_states: Mapping[str, StrategyControlState] | None = None,
    ) -> MonitoringReport:
        return self.manager.monitor(
            snapshot=snapshot,
            strategy_drawdowns=strategy_drawdowns,
            strategy_losses=strategy_losses,
            current_states=current_states,
        )

    def trip_kill_switch(self, reason: str, triggered_by: str = "operator") -> None:
        self.manager.trip_kill_switch(reason, triggered_by)

    def rearm_kill_switch(self, triggered_by: str = "operator") -> None:
        self.manager.rearm_kill_switch(triggered_by)

    @property
    def kill_switch(self) -> KillSwitchRecord:
        return self.engine.kill_switch.record

    @property
    def control_states(self) -> Mapping[str, StrategyControlState]:
        return self.engine.control_states


__all__ = ["PortfolioRiskAdapter"]
