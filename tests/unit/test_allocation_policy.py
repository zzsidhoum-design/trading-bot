"""Unit tests for the capital allocation policies."""

from __future__ import annotations

from decimal import Decimal

import pytest

from qtrader.application.services.allocation_policy import EqualWeightAllocation, MaxCashAllocation
from qtrader.domain.value_objects import (
    Money,
    OrderPlan,
    OrderType,
    Percentage,
    TradeSide,
)


def _plan(quantity: str = "10") -> OrderPlan:
    return OrderPlan(
        symbol="AAPL",
        side=TradeSide.BUY,
        quantity=Decimal(quantity),
        order_type=OrderType.MARKET,
        limit_price=None,
        stop_loss=Decimal("98"),
        take_profit=Decimal("104"),
        risk_per_trade=Percentage("0.01"),
        estimated_exposure=Percentage("0.05"),
        entry_price=Decimal("100"),
    )


def test_equal_weight_sizes_by_cash_cap() -> None:
    policy = EqualWeightAllocation(weight_per_trade=0.2)
    cash = Money("10000")
    quantity = policy.quantity_for(_plan(), cash, open_positions=2)
    # 20% of 10_000 = 2_000 notional / 100 = 20 shares, but plan caps at 10.
    assert quantity == Decimal("10.0000")


def test_equal_weight_never_exceeds_cash() -> None:
    policy = EqualWeightAllocation(weight_per_trade=0.5)
    cash = Money("1000")
    quantity = policy.quantity_for(_plan(quantity="100"), cash, open_positions=0)
    assert quantity == Decimal("5.0000")
    assert quantity * Decimal("100") <= cash.amount


def test_equal_weight_zero_when_no_cash() -> None:
    policy = EqualWeightAllocation()
    assert policy.quantity_for(_plan(), Money("0"), open_positions=0) == Decimal("0")


def test_max_cash_uses_plan_size_when_affordable() -> None:
    policy = MaxCashAllocation()
    quantity = policy.quantity_for(_plan(quantity="7"), Money("100000"), open_positions=0)
    assert quantity == Decimal("7.0000")


def test_weight_out_of_range_rejected() -> None:
    with pytest.raises(ValueError):
        EqualWeightAllocation(weight_per_trade=0)
    with pytest.raises(ValueError):
        EqualWeightAllocation(weight_per_trade=1.5)
