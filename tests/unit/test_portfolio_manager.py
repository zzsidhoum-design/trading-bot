"""Phase 5 — Portfolio Manager (propose -> gate -> ClearedOrder, monitoring,
allocation delegation, kill-switch control)."""

from __future__ import annotations

from decimal import Decimal

from qtrader.application.portfolio_mgmt.allocation import StrategyAllocator
from qtrader.application.portfolio_mgmt.drawdown import DrawdownGuard, KillSwitch, control_state
from qtrader.application.portfolio_mgmt.engine import PortfolioRiskEngine
from qtrader.application.portfolio_mgmt.manager import PortfolioManager
from qtrader.application.portfolio_mgmt.models import (
    AllocationPolicyConfig,
    ClearedOrder,
    DrawdownProtection,
    GateVerdict,
    MonitoringReport,
    PortfolioConstraints,
    PortfolioSnapshot,
    ProposedTrade,
    SizingPolicy,
    StrategyControlStatus,
    snapshot_from_state,
)
from qtrader.domain.value_objects import TradeSide
from tests.unit.fakes_portfolio_mgmt import make_validation_record


def _snapshot(**overrides: object) -> PortfolioSnapshot:
    params = dict(
        equity=Decimal("100000"),
        cash=Decimal("100000"),
        gross_exposure_pct=0.0,
    )
    params.update(overrides)
    return snapshot_from_state(**params)


def _manager(
    *,
    engine: PortfolioRiskEngine | None = None,
    allocator: StrategyAllocator | None = None,
    guard: DrawdownGuard | None = None,
) -> PortfolioManager:
    return PortfolioManager(
        engine=engine
        or PortfolioRiskEngine(
            constraints=PortfolioConstraints(),
            drawdown_protection=DrawdownProtection(),
            sizing_policy=SizingPolicy(),
            kill_switch=KillSwitch(),
        ),
        allocator=allocator or StrategyAllocator(AllocationPolicyConfig()),
        drawdown_guard=guard or DrawdownGuard(DrawdownProtection()),
    )


def test_propose_returns_cleared_order_for_approved_trade() -> None:
    manager = _manager()
    order = manager.propose(
        strategy_id="s1",
        symbol="AAPL",
        side=TradeSide.BUY,
        reference_price=Decimal("100"),
        quantity=Decimal("100"),
        sector="Tech",
        snapshot=_snapshot(),
    )
    assert isinstance(order, ClearedOrder)
    assert order.decision is not None
    assert order.decision.verdict is not GateVerdict.REJECT
    assert order.quantity > 0


def test_propose_returns_none_for_rejected_trade() -> None:
    engine = PortfolioRiskEngine(
        constraints=PortfolioConstraints(),
        drawdown_protection=DrawdownProtection(),
        sizing_policy=SizingPolicy(),
        kill_switch=KillSwitch(),
    )
    engine.kill_switch.trip("emergency halt")
    manager = _manager(engine=engine)
    order = manager.propose(
        strategy_id="s1",
        symbol="AAPL",
        side=TradeSide.BUY,
        reference_price=Decimal("100"),
        quantity=Decimal("100"),
        snapshot=_snapshot(),
    )
    assert order is None


def test_gate_forwards_to_risk_engine() -> None:
    manager = _manager()
    decision = manager.gate(
        ProposedTrade(
            strategy_id="s1",
            symbol="AAPL",
            side=TradeSide.BUY,
            reference_price=Decimal("100"),
            quantity=Decimal("100"),
        ),
        _snapshot(),
    )
    assert decision.verdict is GateVerdict.APPROVE


def test_allocate_delegates_to_allocator() -> None:
    from qtrader.application.research.validation.records import FinalStatus

    manager = _manager()
    good = make_validation_record("good")
    bad = make_validation_record("bad", final_status=FinalStatus.REJECTED)
    report = manager.allocate([good, bad])
    ids = [s.strategy_id for s in report.strategies]
    assert "good" in ids
    assert "bad" not in ids
    assert report.total_weight_pct > 0.0


def test_monitor_produces_report_and_updates_engine_state() -> None:
    manager = _manager()
    report = manager.monitor(
        snapshot=_snapshot(equity=Decimal("90000"), drawdown_pct=0.10),
        strategy_drawdowns={"s1": 0.16},
        strategy_losses={"s1": 2},
        current_states={"s1": control_state("s1")},
    )
    assert isinstance(report, MonitoringReport)
    assert report.kill_switch.state.name == "ARMED"
    assert report.updates


def test_monitor_downgrades_strategy_status() -> None:
    manager = _manager()
    report = manager.monitor(
        snapshot=_snapshot(equity=Decimal("100000")),
        strategy_drawdowns={"s1": 0.40},
        strategy_losses={"s1": 0},
        current_states={"s1": control_state("s1")},
    )
    update = next(u for u in report.updates if u.strategy_id == "s1")
    assert update.current is StrategyControlStatus.SUSPENDED
    assert manager.risk_engine.control_states["s1"].status is StrategyControlStatus.SUSPENDED


def test_trip_and_rearm_kill_switch_via_manager() -> None:
    manager = _manager()
    manager.trip_kill_switch("manual override")
    assert manager.risk_engine.kill_switch.is_tripped
    order = manager.propose(
        strategy_id="s1",
        symbol="AAPL",
        side=TradeSide.BUY,
        reference_price=Decimal("100"),
        quantity=Decimal("100"),
        snapshot=_snapshot(),
    )
    assert order is None
    manager.rearm_kill_switch()
    assert not manager.risk_engine.kill_switch.is_tripped
    order = manager.propose(
        strategy_id="s1",
        symbol="AAPL",
        side=TradeSide.BUY,
        reference_price=Decimal("100"),
        quantity=Decimal("100"),
        snapshot=_snapshot(),
    )
    assert order is not None
