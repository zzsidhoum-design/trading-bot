"""Phase 5 — drawdown protection, consecutive-loss monitoring, kill switch."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from qtrader.application.portfolio_mgmt.drawdown import (
    DrawdownGuard,
    DrawdownTracker,
    KillSwitch,
    control_state,
)
from qtrader.application.portfolio_mgmt.models import (
    DrawdownProtection,
    KillSwitchState,
    StrategyControlStatus,
    snapshot_from_state,
)


def test_drawdown_tracker_tracks_peak() -> None:
    tracker = DrawdownTracker()
    tracker.observe_equity(Decimal("100000"))
    state = tracker.observe_equity(Decimal("80000"))
    assert state.drawdown_pct == pytest.approx(0.20)
    assert state.peak_equity == Decimal("100000")


def test_drawdown_tracker_resets_peak_on_new_high() -> None:
    tracker = DrawdownTracker()
    tracker.observe_equity(Decimal("100000"))
    tracker.observe_equity(Decimal("80000"))
    state = tracker.observe_equity(Decimal("120000"))
    assert state.drawdown_pct == 0.0
    assert state.peak_equity == Decimal("120000")


def test_drawdown_tracker_first_observe_is_flat() -> None:
    tracker = DrawdownTracker()
    state = tracker.observe_equity(Decimal("50000"))
    assert state.drawdown_pct == 0.0


def test_consecutive_losses_reset_on_win() -> None:
    tracker = DrawdownTracker()
    tracker.observe_daily_pnl(-0.01)
    tracker.observe_daily_pnl(-0.02)
    assert tracker.consecutive_losses == 2
    tracker.observe_daily_pnl(0.005)
    assert tracker.consecutive_losses == 0


def test_portfolio_breaches_drawdown() -> None:
    guard = DrawdownGuard(DrawdownProtection(max_portfolio_drawdown_pct=0.20))
    snapshot = snapshot_from_state(
        equity=Decimal("100000"),
        cash=Decimal("100000"),
        gross_exposure_pct=0.0,
        drawdown_pct=0.25,
    )
    breaches = guard.portfolio_breaches(snapshot)
    assert any("portfolio drawdown" in b for b in breaches)


def test_portfolio_breaches_daily_loss() -> None:
    guard = DrawdownGuard(DrawdownProtection(max_daily_loss_pct=0.03))
    snapshot = snapshot_from_state(
        equity=Decimal("100000"),
        cash=Decimal("100000"),
        gross_exposure_pct=0.0,
        daily_pnl_pct=-0.05,
    )
    breaches = guard.portfolio_breaches(snapshot)
    assert any("daily loss" in b for b in breaches)


def test_strategy_recommendation_monitored_then_reduced_then_suspended() -> None:
    guard = DrawdownGuard(
        DrawdownProtection(
            monitor_drawdown_pct=0.15,
            reduce_drawdown_pct=0.20,
            max_strategy_drawdown_pct=0.25,
        )
    )
    current = control_state("s1")
    status, _ = guard.strategy_recommendation(
        "s1", strategy_drawdown_pct=0.16, consecutive_losses=0, current=current
    )
    assert status is StrategyControlStatus.MONITORED
    status, _ = guard.strategy_recommendation(
        "s1", strategy_drawdown_pct=0.21, consecutive_losses=0, current=current
    )
    assert status is StrategyControlStatus.REDUCED
    status, _ = guard.strategy_recommendation(
        "s1", strategy_drawdown_pct=0.26, consecutive_losses=0, current=current
    )
    assert status is StrategyControlStatus.SUSPENDED


def test_consecutive_losses_suspend_strategy() -> None:
    guard = DrawdownGuard(DrawdownProtection(max_consecutive_losses=3))
    current = control_state("s1")
    status, reasons = guard.strategy_recommendation(
        "s1", strategy_drawdown_pct=0.05, consecutive_losses=3, current=current
    )
    assert status is StrategyControlStatus.SUSPENDED
    assert any("consecutive losses" in r for r in reasons)


def test_suspended_strategy_rearmed_after_cooldown() -> None:
    guard = DrawdownGuard(DrawdownProtection(suspension_cooldown_days=30, reduce_drawdown_pct=0.20))
    suspended = control_state(
        "s1",
        StrategyControlStatus.SUSPENDED,
        suspended_until=date.today() - timedelta(days=1),
    )
    status, reasons = guard.strategy_recommendation(
        "s1", strategy_drawdown_pct=0.10, consecutive_losses=0, current=suspended
    )
    assert status is StrategyControlStatus.REDUCED
    assert any("cooldown" in r for r in reasons)


def test_suspended_strategy_stays_suspended_in_drawdown() -> None:
    guard = DrawdownGuard(DrawdownProtection(suspension_cooldown_days=30))
    suspended = control_state(
        "s1",
        StrategyControlStatus.SUSPENDED,
        suspended_until=date.today() - timedelta(days=1),
    )
    status, _ = guard.strategy_recommendation(
        "s1", strategy_drawdown_pct=0.40, consecutive_losses=0, current=suspended
    )
    assert status is StrategyControlStatus.SUSPENDED


def test_transition_sets_suspension_deadline() -> None:
    guard = DrawdownGuard(DrawdownProtection(suspension_cooldown_days=30))
    current = control_state("s1")
    update = guard.transition(
        "s1", strategy_drawdown_pct=0.40, consecutive_losses=0, current=current
    )
    assert update.current is StrategyControlStatus.SUSPENDED
    assert update.suspended_until is not None
    days = (update.suspended_until - date.today()).days
    assert 29 <= days <= 31  # UTC-vs-local date boundary can shift a day


def test_weight_factor_by_status() -> None:
    protection = DrawdownProtection()
    assert control_state("s1").weight_factor(protection) == 1.0
    monitored = control_state("s1", StrategyControlStatus.MONITORED)
    assert monitored.weight_factor(protection) == protection.monitored_weight_factor
    reduced = control_state("s1", StrategyControlStatus.REDUCED)
    assert reduced.weight_factor(protection) == protection.reduced_weight_factor
    assert control_state("s1", StrategyControlStatus.SUSPENDED).weight_factor(protection) == 0.0


def test_kill_switch_trip_and_rearm() -> None:
    switch = KillSwitch()
    assert switch.state is KillSwitchState.ARMED
    record = switch.trip("market crash", "operator")
    assert record.state is KillSwitchState.TRIPPED
    assert switch.is_tripped
    assert switch.record.reason == "market crash"
    switch.rearm()
    assert switch.state is KillSwitchState.ARMED
    assert not switch.is_tripped


def test_kill_switch_trip_is_idempotent() -> None:
    switch = KillSwitch()
    switch.trip("first")
    switch.trip("second")
    assert switch.record.reason == "first"


def test_kill_switch_tracked_by_manager_monitoring() -> None:
    guard = DrawdownGuard(DrawdownProtection())
    from qtrader.application.portfolio_mgmt.allocation import StrategyAllocator
    from qtrader.application.portfolio_mgmt.engine import PortfolioRiskEngine
    from qtrader.application.portfolio_mgmt.manager import PortfolioManager
    from qtrader.application.portfolio_mgmt.models import (
        AllocationPolicyConfig,
        PortfolioConstraints,
        SizingPolicy,
    )

    switch = KillSwitch()
    engine = PortfolioRiskEngine(
        constraints=PortfolioConstraints(),
        drawdown_protection=DrawdownProtection(),
        sizing_policy=SizingPolicy(),
        kill_switch=switch,
    )
    manager = PortfolioManager(
        engine=engine,
        allocator=StrategyAllocator(AllocationPolicyConfig()),
        drawdown_guard=guard,
    )
    manager.trip_kill_switch("emergency")
    assert manager.risk_engine.kill_switch.is_tripped
    manager.rearm_kill_switch()
    assert not manager.risk_engine.kill_switch.is_tripped
