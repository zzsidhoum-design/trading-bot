"""Capital allocation policies — pluggable sizing strategies for the Portfolio Agent."""

from __future__ import annotations

from decimal import Decimal

from qtrader.domain.ports import AllocationPolicy
from qtrader.domain.value_objects import Money, OrderPlan

_QTY_QUANT = Decimal("0.0001")


class EqualWeightAllocation(AllocationPolicy):
    """Size each new position to a target weight of available cash, capped by
    the risk-approved plan quantity and never exceeding available cash."""

    def __init__(self, weight_per_trade: float = 0.2) -> None:
        if not 0 < weight_per_trade <= 1:
            raise ValueError("weight_per_trade must be in (0, 1]")
        self._weight = Decimal(str(weight_per_trade))

    def quantity_for(self, plan: OrderPlan, cash: Money, open_positions: int) -> Decimal:
        if cash.amount <= 0:
            return Decimal(0)
        price = _estimate_price(plan)
        cash_qty = (cash.amount * self._weight) / price if price else Decimal(0)
        cash_qty = cash_qty.quantize(_QTY_QUANT)
        qty = min(Decimal(plan.quantity), cash_qty)
        return qty.quantize(_QTY_QUANT)


class MaxCashAllocation(AllocationPolicy):
    """Allocate up to the risk-approved size, capped by available cash."""

    def quantity_for(self, plan: OrderPlan, cash: Money, open_positions: int) -> Decimal:
        if cash.amount <= 0:
            return Decimal(0)
        price = _estimate_price(plan)
        cash_qty = (cash.amount / price) if price else Decimal(0)
        cash_qty = cash_qty.quantize(_QTY_QUANT)
        qty = min(Decimal(plan.quantity), cash_qty)
        return qty.quantize(_QTY_QUANT)


def _estimate_price(plan: OrderPlan) -> Decimal:
    """Reference price for notional → shares sizing."""
    if plan.limit_price is not None and plan.limit_price > 0:
        return plan.limit_price
    if plan.entry_price > 0:
        return plan.entry_price
    if plan.stop_loss > 0:
        return plan.stop_loss
    return Decimal(1)
