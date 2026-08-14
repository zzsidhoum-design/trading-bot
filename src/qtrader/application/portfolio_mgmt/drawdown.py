"""Drawdown protection, consecutive-loss monitoring and the kill switch.

The controls here are independent of any agent: they observe equity, P/L and
strategy outcomes and produce hard gates plus status transitions
(ACTIVE -> MONITORED -> REDUCED -> SUSPENDED, never permanent deletion).
The kill switch is a separate emergency shutdown that only an explicit
operator action can re-arm.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from qtrader.application.portfolio_mgmt.models import (
    DrawdownProtection,
    KillSwitchRecord,
    KillSwitchState,
    PortfolioSnapshot,
    StrategyControlState,
    StrategyControlStatus,
    StrategyMonitoringUpdate,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _dec(value: Decimal | int | float | str) -> Decimal:
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class DrawdownState:
    """Computed drawdown/daily-loss/consecutive-loss state."""

    drawdown_pct: float = 0.0
    daily_pnl_pct: float = 0.0
    consecutive_losses: int = 0
    peak_equity: Decimal | None = None


class DrawdownTracker:
    """Tracks the equity curve and daily P/L for drawdown/loss accounting."""

    def __init__(self) -> None:
        self._peak: Decimal = Decimal(0)
        self._equity: Decimal = Decimal(0)
        self._consecutive_losses: int = 0
        self._has_equity: bool = False

    @property
    def peak_equity(self) -> Decimal:
        return self._peak

    def observe_equity(self, equity: Decimal | int | float) -> DrawdownState:
        equity = _dec(equity)
        if not self._has_equity:
            self._peak = equity
            self._equity = equity
            self._has_equity = True
            return DrawdownState(0.0, 0.0, 0, self._peak)
        self._equity = equity
        if equity > self._peak:
            self._peak = equity
        drawdown = float((self._peak - equity) / self._peak) if self._peak > 0 else 0.0
        return DrawdownState(drawdown, 0.0, self._consecutive_losses, self._peak)

    def observe_daily_pnl(self, pnl_pct: float) -> DrawdownState:
        """Feed a single-day P/L (fraction) and update the loss streak."""
        if pnl_pct < 0.0:
            self._consecutive_losses += 1
        elif pnl_pct > 0.0:
            self._consecutive_losses = 0
        return DrawdownState(
            float((self._peak - self._equity) / self._peak)
            if self._peak > 0
            else 0.0,
            pnl_pct,
            self._consecutive_losses,
            self._peak,
        )

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses


class DrawdownGuard:
    """Enforces drawdown / daily-loss / consecutive-loss thresholds and the
    strategy status machine."""

    def __init__(self, protection: DrawdownProtection) -> None:
        self._protection = protection
        self._tracker = DrawdownTracker()

    @property
    def protection(self) -> DrawdownProtection:
        return self._protection

    @property
    def tracker(self) -> DrawdownTracker:
        return self._tracker

    def portfolio_breaches(
        self,
        snapshot: PortfolioSnapshot,
    ) -> tuple[str, ...]:
        """Portfolio-level hard violations (drawdown / daily loss)."""
        breaches: list[str] = []
        protection = self._protection
        if snapshot.drawdown_pct >= protection.max_portfolio_drawdown_pct + 1e-9:
            breaches.append(
                f"portfolio drawdown {snapshot.drawdown_pct * 100:.1f}% exceeds "
                f"limit {protection.max_portfolio_drawdown_pct * 100:.1f}%"
            )
        if snapshot.daily_pnl_pct < -protection.max_daily_loss_pct:
            breaches.append(
                f"daily loss {snapshot.daily_pnl_pct * 100:.1f}% exceeds "
                f"limit {protection.max_daily_loss_pct * 100:.1f}%"
            )
        return tuple(breaches)

    def strategy_recommendation(
        self,
        strategy_id: str,
        *,
        strategy_drawdown_pct: float,
        consecutive_losses: int,
        current: StrategyControlState,
    ) -> tuple[StrategyControlStatus, tuple[str, ...]]:
        """Next status for a strategy given its current drawdown/loss state.

        Never deletes the strategy. SUSPENDED strategies may return to REDUCED
        after ``suspension_cooldown_days`` without a new breach.
        """
        protection = self._protection
        reasons: list[str] = []
        status = current.status

        if status is StrategyControlStatus.SUSPENDED:
            if current.suspended_until is not None and current.suspended_until <= _now().date():
                if strategy_drawdown_pct < protection.reduce_drawdown_pct:
                    status = StrategyControlStatus.REDUCED
                    reasons.append("suspension cooldown elapsed; reinstated at reduced weight")
                else:
                    status = StrategyControlStatus.SUSPENDED
                    reasons.append("still in drawdown; suspension continues")
            else:
                reasons.append("strategy suspended")
            return status, tuple(reasons)

        if consecutive_losses >= protection.max_consecutive_losses:
            status = StrategyControlStatus.SUSPENDED
            reasons.append(
                f"{consecutive_losses} consecutive losses exceed "
                f"limit {protection.max_consecutive_losses}"
            )
        elif strategy_drawdown_pct >= protection.max_strategy_drawdown_pct + 1e-9:
            status = StrategyControlStatus.SUSPENDED
            reasons.append(
                f"strategy drawdown {strategy_drawdown_pct * 100:.1f}% exceeds "
                f"limit {protection.max_strategy_drawdown_pct * 100:.1f}%"
            )
        elif strategy_drawdown_pct >= protection.reduce_drawdown_pct + 1e-9:
            status = StrategyControlStatus.REDUCED
            reasons.append(
                f"strategy drawdown {strategy_drawdown_pct * 100:.1f}% exceeds "
                f"reduce threshold {protection.reduce_drawdown_pct * 100:.1f}%"
            )
        elif strategy_drawdown_pct >= protection.monitor_drawdown_pct + 1e-9:
            status = StrategyControlStatus.MONITORED
            reasons.append(
                f"strategy drawdown {strategy_drawdown_pct * 100:.1f}% exceeds "
                f"monitor threshold {protection.monitor_drawdown_pct * 100:.1f}%"
            )
        else:
            status = StrategyControlStatus.ACTIVE
            reasons.append("within drawdown thresholds")

        if (
            status is StrategyControlStatus.ACTIVE
            and current.status is not StrategyControlStatus.ACTIVE
        ):
            reasons.append("recovered; reinstated to active")
        if status is StrategyControlStatus.MONITORED and current.status in (
            StrategyControlStatus.ACTIVE,
        ):
            reasons.append("degraded to monitored")
        return status, tuple(reasons)

    def transition(
        self,
        strategy_id: str,
        *,
        strategy_drawdown_pct: float,
        consecutive_losses: int,
        current: StrategyControlState,
    ) -> StrategyMonitoringUpdate:
        next_status, reasons = self.strategy_recommendation(
            strategy_id,
            strategy_drawdown_pct=strategy_drawdown_pct,
            consecutive_losses=consecutive_losses,
            current=current,
        )
        suspended_until = current.suspended_until
        if (
            next_status is StrategyControlStatus.SUSPENDED
            and current.status is not StrategyControlStatus.SUSPENDED
        ):
            suspended_until = _now().date() + timedelta(
                days=self._protection.suspension_cooldown_days
            )
        if (
            next_status is not StrategyControlStatus.SUSPENDED
            and current.status is StrategyControlStatus.SUSPENDED
        ):
            suspended_until = None
        return StrategyMonitoringUpdate(
            strategy_id=strategy_id,
            previous=current.status,
            current=next_status,
            reasons=reasons,
            suspended_until=suspended_until,
        )


class KillSwitch:
    """Independent emergency shutdown. Only an explicit re-arm re-enables."""

    def __init__(self, record: KillSwitchRecord | None = None) -> None:
        self._record = record or KillSwitchRecord()

    @property
    def state(self) -> KillSwitchState:
        return self._record.state

    @property
    def record(self) -> KillSwitchRecord:
        return self._record

    def trip(self, reason: str, triggered_by: str = "operator") -> KillSwitchRecord:
        if self._record.state is KillSwitchState.TRIPPED:
            return self._record
        self._record = KillSwitchRecord(
            state=KillSwitchState.TRIPPED,
            triggered_at=_now(),
            triggered_by=triggered_by,
            reason=reason,
        )
        return self._record

    def rearm(self, triggered_by: str = "operator") -> KillSwitchRecord:
        if self._record.state is KillSwitchState.TRIPPED:
            self._record = KillSwitchRecord(
                state=KillSwitchState.ARMED,
                triggered_by=triggered_by,
                reason=self._record.reason,
            )
        return self._record

    @property
    def is_tripped(self) -> bool:
        return self._record.state is KillSwitchState.TRIPPED


def control_state(
    strategy_id: str,
    status: StrategyControlStatus = StrategyControlStatus.ACTIVE,
    *,
    reasons: tuple[str, ...] = (),
    updated_at: datetime | None = None,
    suspended_until: date | None = None,
) -> StrategyControlState:
    return StrategyControlState(
        strategy_id=strategy_id,
        status=status,
        reasons=reasons,
        updated_at=updated_at or _now(),
        suspended_until=suspended_until,
    )


__all__ = [
    "DrawdownGuard",
    "DrawdownState",
    "DrawdownTracker",
    "KillSwitch",
    "control_state",
]
