"""Phase 5 — risk-aware strategy allocation (not historical-returns only)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from qtrader.application.portfolio_mgmt.allocation import StrategyAllocator
from qtrader.application.portfolio_mgmt.drawdown import control_state
from qtrader.application.portfolio_mgmt.models import (
    AllocationPolicyConfig,
    DrawdownProtection,
    StrategyControlStatus,
)
from qtrader.application.research.validation.records import FinalStatus
from tests.unit.fakes_portfolio_mgmt import make_validation_record, with_final_status


def _config(**overrides: float) -> AllocationPolicyConfig:
    return AllocationPolicyConfig(**overrides)


def test_only_eligible_strategies_receive_capital() -> None:
    allocator = StrategyAllocator(_config())
    robust = make_validation_record("good")
    rejected = make_validation_record("bad", final_status=FinalStatus.REJECTED)
    report = allocator.allocate([robust, rejected])
    ids = [s.strategy_id for s in report.strategies]
    assert ids == ["good"]
    assert report.total_weight_pct > 0.0


def test_no_eligible_strategies_empty_report() -> None:
    allocator = StrategyAllocator(_config())
    report = allocator.allocate([make_validation_record("bad", final_status=FinalStatus.REJECTED)])
    assert report.strategies == ()
    assert report.total_weight_pct == 0.0
    assert report.notes


def test_better_risk_adjusted_strategy_gets_more_weight() -> None:
    allocator = StrategyAllocator(_config())
    good = make_validation_record(
        "good",
        sharpe=Decimal("1.8"),
        total_return=Decimal("0.40"),
        max_drawdown=Decimal("0.05"),
    )
    weak = make_validation_record(
        "weak",
        sharpe=Decimal("0.2"),
        total_return=Decimal("0.02"),
        max_drawdown=Decimal("0.45"),
    )
    report = allocator.allocate([good, weak])
    scores = {s.strategy_id: s.score for s in report.strategies}
    assert scores["good"] > scores["weak"]


def test_high_correlation_penalizes_allocation() -> None:
    allocator = StrategyAllocator(_config(correlation_weight=1.0))
    a = make_validation_record("a", sharpe=Decimal("1.5"))
    b = make_validation_record("b", sharpe=Decimal("1.5"))
    returns = {
        "a": [0.01, -0.01] * 10,
        "b": [0.01, -0.01] * 10,
    }
    report = allocator.allocate([a, b], returns_by_strategy=returns)
    weights = {s.strategy_id: s.weight_pct for s in report.strategies}
    # Two perfectly correlated equal strategies share the total budget.
    assert weights["a"] == pytest.approx(weights["b"], abs=1e-9)
    assert sum(weights.values()) == pytest.approx(report.total_weight_pct)


def test_suspended_strategy_gets_zero_weight() -> None:
    allocator = StrategyAllocator(_config())
    suspended = make_validation_record("s")
    active = make_validation_record("a")
    states = {
        "s": control_state("s", StrategyControlStatus.SUSPENDED),
        "a": control_state("a"),
    }
    report = allocator.allocate([suspended, active], control_states=states)
    weights = {s.strategy_id: s.weight_pct for s in report.strategies}
    assert weights["s"] == 0.0
    assert weights["a"] > 0.0


def test_reduced_strategy_gets_less_weight() -> None:
    allocator = StrategyAllocator(_config())
    reduced = make_validation_record("r", sharpe=Decimal("1.5"))
    active = make_validation_record("a", sharpe=Decimal("1.5"))
    other = make_validation_record("o", sharpe=Decimal("1.5"))
    states = {
        "r": control_state("r", StrategyControlStatus.REDUCED),
        "a": control_state("a"),
        "o": control_state("o"),
    }
    report = allocator.allocate([reduced, active, other], control_states=states)
    weights = {s.strategy_id: s.weight_pct for s in report.strategies}
    assert weights["r"] < weights["a"]


def test_weights_capped_by_max_weight() -> None:
    allocator = StrategyAllocator(_config(max_weight_pct=0.40))
    records = [make_validation_record(f"s{i}", sharpe=Decimal("1.8")) for i in range(3)]
    report = allocator.allocate(records)
    for strategy in report.strategies:
        assert strategy.weight_pct <= 0.40 + 1e-9
    assert report.total_weight_pct <= 1.0


def test_regime_quality_affects_allocation() -> None:
    allocator = StrategyAllocator(_config(regime_weight=2.0))
    a = make_validation_record("a", sharpe=Decimal("1.0"))
    b = make_validation_record("b", sharpe=Decimal("1.0"))
    report = allocator.allocate(
        [a, b],
        regime_quality={"a": 1.0, "b": 0.0},
    )
    scores = {s.strategy_id: s.score for s in report.strategies}
    assert scores["a"] > scores["b"]


def test_allocation_includes_rationale_and_risk() -> None:
    allocator = StrategyAllocator(_config())
    records = [
        make_validation_record("s1", sharpe=Decimal("1.2")),
        make_validation_record("s2", sharpe=Decimal("1.0")),
    ]
    returns = {
        "s1": [0.01 if i % 3 else -0.005 for i in range(60)],
        "s2": [0.008 if i % 4 else -0.004 for i in range(60)],
    }
    report = allocator.allocate(records, returns_by_strategy=returns)
    assert report.strategies[0].rationale
    assert report.risk is not None
    assert report.risk.sharpe != 0.0 or report.risk.volatility_pct > 0.0
    assert any("not historical returns alone" in n for n in report.notes)


def test_execution_robust_outscores_plain_validated() -> None:
    allocator = StrategyAllocator(_config())
    robust = make_validation_record("robust", sharpe=Decimal("1.0"))
    validated = make_validation_record("valid", sharpe=Decimal("1.0"))
    validated = with_final_status(validated, FinalStatus.VALIDATED)
    report = allocator.allocate([robust, validated])
    scores = {s.strategy_id: s.score for s in report.strategies}
    assert scores["robust"] > scores["valid"]


def test_default_drawdown_protection_uses_status_factors() -> None:
    protection = DrawdownProtection()
    assert protection.monitored_weight_factor == 0.75
    assert protection.reduced_weight_factor == 0.50
