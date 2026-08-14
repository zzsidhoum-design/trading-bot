"""Portfolio Manager — the layer between strategy/AI decisions and the Risk
Engine (and from there to the Execution Simulator).

    Strategy / AI Decision
            ↓
    Portfolio Manager        <- this module
            ↓
    Risk Engine
            ↓
    Execution Simulator
            ↓
    Execution

The manager never decides risk itself: it packages proposals, forwards them to
the authoritative Risk Engine, and turns approved outcomes into
:class:`ClearedOrder` records for the Execution Simulator. It also runs the
periodic monitoring pass (drawdown/loss monitoring, failure controls, kill
switch) and risk-aware capital allocation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal

from qtrader.application.portfolio_mgmt.allocation import StrategyAllocator
from qtrader.application.portfolio_mgmt.drawdown import DrawdownGuard, control_state
from qtrader.application.portfolio_mgmt.engine import PortfolioRiskEngine
from qtrader.application.portfolio_mgmt.models import (
    AllocationReport,
    ClearedOrder,
    GateDecision,
    GateVerdict,
    MonitoringReport,
    PortfolioSnapshot,
    ProposedTrade,
    StrategyControlState,
    StrategyMonitoringUpdate,
)
from qtrader.application.research.validation.records import ValidationRecord
from qtrader.domain.value_objects import TradeSide


def _now() -> datetime:
    return datetime.now(UTC)


class PortfolioManager:
    """Coordinates proposals, risk gating, monitoring and allocation."""

    def __init__(
        self,
        engine: PortfolioRiskEngine,
        allocator: StrategyAllocator,
        drawdown_guard: DrawdownGuard,
    ) -> None:
        self._engine = engine
        self._allocator = allocator
        self._drawdown_guard = drawdown_guard

    @property
    def risk_engine(self) -> PortfolioRiskEngine:
        return self._engine

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
        """Package a strategy proposal, run it through the Risk Engine and
        return a risk-cleared order (or None when rejected)."""
        trade = ProposedTrade(
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
        )
        decision = self._engine.gate(trade, snapshot)
        if decision.verdict is GateVerdict.REJECT:
            return None
        return ClearedOrder(
            strategy_id=strategy_id,
            symbol=symbol,
            side=side,
            quantity=decision.approved_quantity or Decimal(0),
            limit_price=limit_price,
            stop_loss=stop_loss,
            signal_ts=signal_ts,
            decision=decision,
        )

    def gate(
        self,
        trade: ProposedTrade,
        snapshot: PortfolioSnapshot,
    ) -> GateDecision:
        """Direct access to the authoritative gate (for callers that already
        build :class:`ProposedTrade` records)."""
        return self._engine.gate(trade, snapshot)

    def allocate(
        self,
        strategies: Sequence[ValidationRecord],
        *,
        returns_by_strategy: Mapping[str, Sequence[float]] | None = None,
        regime_quality: Mapping[str, float] | None = None,
        control_states: Mapping[str, StrategyControlState] | None = None,
    ) -> AllocationReport:
        """Risk-aware allocation of capital across validated strategies."""
        return self._allocator.allocate(
            strategies,
            returns_by_strategy=returns_by_strategy,
            regime_quality=regime_quality,
            control_states=control_states,
            protection=self._drawdown_guard.protection,
        )

    def monitor(
        self,
        *,
        snapshot: PortfolioSnapshot,
        strategy_drawdowns: Mapping[str, float],
        strategy_losses: Mapping[str, int] | None = None,
        current_states: Mapping[str, StrategyControlState] | None = None,
    ) -> MonitoringReport:
        """Run the monitoring pass: update drawdown/loss state, apply failure
        controls, and record strategy status transitions."""
        current_states = current_states or {}
        tracker = self._drawdown_guard.tracker
        tracker.observe_equity(snapshot.equity)
        tracker.observe_daily_pnl(snapshot.daily_pnl_pct)

        portfolio_breaches = self._drawdown_guard.portfolio_breaches(snapshot)

        updates: list[StrategyMonitoringUpdate] = []
        for strategy_id, drawdown_pct in strategy_drawdowns.items():
            current = current_states.get(strategy_id) or control_state(strategy_id)
            losses = (strategy_losses or {}).get(strategy_id, 0)
            update = self._drawdown_guard.transition(
                strategy_id,
                strategy_drawdown_pct=drawdown_pct,
                consecutive_losses=losses,
                current=current,
            )
            updates.append(update)
            new_state = control_state(
                strategy_id,
                update.current,
                reasons=update.reasons,
                updated_at=_now(),
                suspended_until=update.suspended_until,
            )
            self._engine.update_control_state(new_state)

        return MonitoringReport(
            updates=tuple(updates),
            kill_switch=self._engine.kill_switch.record,
            portfolio_breaches=tuple(portfolio_breaches),
        )

    def trip_kill_switch(self, reason: str, triggered_by: str = "operator") -> None:
        self._engine.kill_switch.trip(reason, triggered_by)

    def rearm_kill_switch(self, triggered_by: str = "operator") -> None:
        self._engine.kill_switch.rearm(triggered_by)


__all__ = ["PortfolioManager"]
