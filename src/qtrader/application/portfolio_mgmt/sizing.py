"""Position sizing — configurable methods that decide trade size.

The strategy's raw signal never determines position size. The Risk Engine
chooses a method (fixed allocation, volatility-based, risk-budget-based, or
max-exposure) from the ``SizingPolicy`` and caps the result by portfolio
constraints (cash, max weight, leverage).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal

from qtrader.application.portfolio_mgmt.models import (
    PortfolioConstraints,
    PortfolioSnapshot,
    PositionSize,
    PositionSizingMethod,
    ProposedTrade,
    SizingPolicy,
)

_QTY_QUANT = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class SizeInput:
    """Everything a sizer needs about the proposed trade."""

    trade: ProposedTrade
    snapshot: PortfolioSnapshot
    policy: SizingPolicy
    constraints: PortfolioConstraints


class PositionSizer(ABC):
    """Strategy-independent position sizing."""

    method: PositionSizingMethod

    @abstractmethod
    def size(self, inputs: SizeInput) -> PositionSize: ...


class FixedAllocationSizer(PositionSizer):
    """Size to a fixed fraction of portfolio equity (cash-capped)."""

    method = PositionSizingMethod.FIXED_ALLOCATION

    def size(self, inputs: SizeInput) -> PositionSize:
        policy = inputs.policy
        weight = policy.fixed_allocation_pct
        notional = inputs.snapshot.equity * Decimal(str(weight))
        notional = _cap_to_cash(notional, inputs)
        return _position_size(inputs, notional, weight, self.method, ())


class VolatilitySizer(PositionSizer):
    """Inverse-volatility sizing: weight = vol_target / annualized volatility.

    Assets with higher volatility receive smaller allocations. Falls back to
    the fixed-allocation weight when volatility is unknown/zero (with a
    warning) so data gaps cannot produce an oversized position.
    """

    method = PositionSizingMethod.VOLATILITY

    def size(self, inputs: SizeInput) -> PositionSize:
        policy = inputs.policy
        vol = inputs.trade.annualized_vol_pct
        warnings: list[str] = []
        if vol is None or vol <= 0.0:
            weight = min(policy.fixed_allocation_pct, policy.max_weight_pct)
            warnings.append("no annualized volatility for sizing; fell back to fixed allocation")
        else:
            weight = policy.vol_target_pct / vol
            weight = min(weight, policy.max_annualized_vol_pct / vol)
            weight = min(weight, policy.max_weight_pct)
            if weight < policy.fixed_allocation_pct * 0.1:
                warnings.append("volatility-based size reduced to near-zero")
        notional = inputs.snapshot.equity * Decimal(str(weight))
        notional = _cap_to_cash(notional, inputs)
        return _position_size(inputs, notional, weight, self.method, tuple(warnings))


class RiskBudgetSizer(PositionSizer):
    """Risk-budget sizing: risk capital = equity x risk_per_trade_pct.

    Quantity = risk budget / (price x ATR%). The trade risks at most the
    configured fraction of equity if stopped at one ATR. Requires ``atr_pct``;
    without it the sizer degrades to the fixed-allocation weight with a
    warning (the engine's data-quality gate already rejects missing ATR for
    new buys before this fallback is used in production).
    """

    method = PositionSizingMethod.RISK_BUDGET

    def size(self, inputs: SizeInput) -> PositionSize:
        policy = inputs.policy
        warnings: list[str] = []
        atr_pct = inputs.trade.atr_pct
        if atr_pct is None or atr_pct <= 0.0:
            weight = min(policy.fixed_allocation_pct, policy.max_weight_pct)
            warnings.append("no ATR for risk budgeting; fell back to fixed allocation")
            notional = inputs.snapshot.equity * Decimal(str(weight))
            notional = _cap_to_cash(notional, inputs)
            return _position_size(inputs, notional, weight, self.method, tuple(warnings))
        risk_dollar = inputs.snapshot.equity * Decimal(str(policy.risk_per_trade_pct))
        price = inputs.trade.reference_price
        if price <= 0:
            notional = Decimal(0)
            warnings.append("invalid reference price; sized to zero")
            return _position_size(inputs, notional, 0.0, self.method, tuple(warnings))
        stop_distance = price * Decimal(str(atr_pct))
        if stop_distance <= 0:
            notional = Decimal(0)
            warnings.append("zero stop distance; sized to zero")
            return _position_size(inputs, notional, 0.0, self.method, tuple(warnings))
        quantity = risk_dollar / stop_distance
        quantity = quantity.quantize(_QTY_QUANT)
        notional = quantity * price
        weight = float(notional / inputs.snapshot.equity) if inputs.snapshot.equity else 0.0
        if weight > policy.max_weight_pct:
            quantity = (inputs.snapshot.equity * Decimal(str(policy.max_weight_pct))) / price
            quantity = quantity.quantize(_QTY_QUANT)
            notional = quantity * price
            weight = policy.max_weight_pct
        notional = _cap_to_cash(notional, inputs)
        quantity = (notional / price).quantize(_QTY_QUANT) if price > 0 else Decimal(0)
        weight = float(notional / inputs.snapshot.equity) if inputs.snapshot.equity else 0.0
        return _position_size(inputs, notional, weight, self.method, tuple(warnings))


class MaxExposureSizer(PositionSizer):
    """Size to the maximum allowed single-position weight (cash-capped)."""

    method = PositionSizingMethod.MAX_EXPOSURE

    def size(self, inputs: SizeInput) -> PositionSize:
        weight = inputs.policy.max_weight_pct
        notional = inputs.snapshot.equity * Decimal(str(weight))
        notional = _cap_to_cash(notional, inputs)
        return _position_size(inputs, notional, weight, self.method, ())


def sizer_for(method: PositionSizingMethod) -> PositionSizer:
    """Dispatch a sizer instance for a sizing method."""
    if method is PositionSizingMethod.VOLATILITY:
        return VolatilitySizer()
    if method is PositionSizingMethod.RISK_BUDGET:
        return RiskBudgetSizer()
    if method is PositionSizingMethod.MAX_EXPOSURE:
        return MaxExposureSizer()
    return FixedAllocationSizer()


def _cap_to_cash(notional: Decimal, inputs: SizeInput) -> Decimal:
    if notional <= 0:
        return Decimal(0)
    cash = inputs.snapshot.cash
    if cash <= 0:
        return Decimal(0)
    return min(notional, cash).quantize(_QTY_QUANT)


def _position_size(
    inputs: SizeInput,
    notional: Decimal,
    weight: float,
    method: PositionSizingMethod,
    warnings: tuple[str, ...],
) -> PositionSize:
    price = inputs.trade.reference_price
    quantity = (notional / price).quantize(_QTY_QUANT) if price > 0 else Decimal(0)
    actual_weight = (
        float(notional / inputs.snapshot.equity) if inputs.snapshot.equity else 0.0
    )
    return PositionSize(
        symbol=inputs.trade.symbol,
        quantity=quantity,
        notional=notional.quantize(_QTY_QUANT),
        weight_pct=min(weight, actual_weight) if weight >= 0 else 0.0,
        method=method,
        warnings=warnings,
    )


def apply_control_weights(
    quantity: Decimal,
    strategy_id: str,
    weight_factor: float,
) -> Decimal:
    """Scale an approved quantity by a strategy status weight factor."""
    if weight_factor <= 0.0:
        return Decimal(0)
    scaled = quantity * Decimal(str(weight_factor))
    return scaled.quantize(_QTY_QUANT)


__all__ = [
    "FixedAllocationSizer",
    "MaxExposureSizer",
    "PositionSizer",
    "RiskBudgetSizer",
    "SizeInput",
    "VolatilitySizer",
    "apply_control_weights",
    "sizer_for",
]
