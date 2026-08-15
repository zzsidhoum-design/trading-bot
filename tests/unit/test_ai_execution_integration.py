"""Phase 6 — Simulated execution (research-only Phase 4 integration)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from qtrader.application.ai.execution_integration import SimulatedExecution
from qtrader.application.execution.models import ExecutionScenario
from qtrader.application.portfolio_mgmt.models import ClearedOrder
from qtrader.domain.value_objects import TradeSide
from tests.unit.fakes_ai import make_price_bars, rising_closes


def _order(quantity: str = "100") -> ClearedOrder:
    return ClearedOrder(
        strategy_id="s1",
        symbol="AAPL",
        side=TradeSide.BUY,
        quantity=Decimal(quantity),
        signal_ts=datetime(2024, 1, 1, tzinfo=UTC),
    )


def _bars() -> list[object]:
    return make_price_bars("AAPL", rising_closes(30, start=100.0, step=0.5))


def test_run_fills_order_and_reports_outcome() -> None:
    sim = SimulatedExecution()
    outcome = sim.run(
        _order(),
        _bars(),  # type: ignore[arg-type]
        adv_volume=Decimal("1000000"),
        adv_dollar=Decimal("100000000"),
    )
    assert outcome.filled is True
    assert 0.0 < outcome.fill_rate <= 1.0
    assert outcome.net_return is not None
    assert outcome.commission >= 0
    assert outcome.scenario == ExecutionScenario.BASELINE.value


def test_run_returns_clean_unfilled_for_no_bars() -> None:
    sim = SimulatedExecution()
    outcome = sim.run(
        _order(),
        [],
        adv_volume=Decimal("1000000"),
        adv_dollar=Decimal("100000000"),
    )
    assert outcome.filled is False
    assert outcome.fill_rate == 0.0
    assert outcome.rejected_rate == 1.0
    assert outcome.net_return is None


def test_run_returns_clean_unfilled_for_zero_quantity() -> None:
    sim = SimulatedExecution()
    outcome = sim.run(
        _order(quantity="0"),
        _bars(),  # type: ignore[arg-type]
        adv_volume=Decimal("1000000"),
        adv_dollar=Decimal("100000000"),
    )
    assert outcome.filled is False


def test_run_is_deterministic_across_calls() -> None:
    sim = SimulatedExecution(seed=7)
    kwargs = dict(
        adv_volume=Decimal("1000000"),
        adv_dollar=Decimal("100000000"),
    )
    first = sim.run(_order(), _bars(), **kwargs)  # type: ignore[arg-type]
    second = sim.run(_order(), _bars(), **kwargs)  # type: ignore[arg-type]
    assert first.net_return == second.net_return
    assert first.fill_rate == second.fill_rate


def test_scenario_flows_into_assumptions() -> None:
    sim = SimulatedExecution(
        scenario=ExecutionScenario.STRESS,
        commission_bps=25.0,
        max_participation_rate=0.05,
        seed=1,
    )
    outcome = sim.run(
        _order(),
        _bars(),  # type: ignore[arg-type]
        adv_volume=Decimal("1000000"),
        adv_dollar=Decimal("100000000"),
    )
    assert outcome.scenario == ExecutionScenario.STRESS.value
    assert outcome.assumptions.commission_bps == 25.0
    assert outcome.assumptions.max_participation_rate == 0.05
    assert outcome.assumptions.seed == 1
