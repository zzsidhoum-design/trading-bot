"""Phase 5 — position sizing (fixed / volatility / risk-budget / max-exposure)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from qtrader.application.portfolio_mgmt.models import (
    PortfolioConstraints,
    PortfolioSnapshot,
    PositionSizingMethod,
    ProposedTrade,
    SizingPolicy,
    snapshot_from_state,
)
from qtrader.application.portfolio_mgmt.sizing import (
    FixedAllocationSizer,
    MaxExposureSizer,
    RiskBudgetSizer,
    SizeInput,
    VolatilitySizer,
    sizer_for,
)
from qtrader.domain.value_objects import TradeSide

_EQUITY = Decimal("100000")


def _snapshot(**overrides: object) -> PortfolioSnapshot:
    params = dict(
        equity=_EQUITY,
        cash=_EQUITY,
        gross_exposure_pct=0.0,
    )
    params.update(overrides)
    return snapshot_from_state(**params)


def _trade(
    symbol: str = "AAPL",
    price: str = "100",
    atr_pct: float | None = 0.02,
    vol: float | None = 0.30,
) -> ProposedTrade:
    return ProposedTrade(
        strategy_id="s1",
        symbol=symbol,
        side=TradeSide.BUY,
        reference_price=Decimal(price),
        quantity=Decimal("1000"),
        atr_pct=atr_pct,
        annualized_vol_pct=vol,
    )


def _inputs(sizing: SizingPolicy, trade: ProposedTrade | None = None) -> SizeInput:
    return SizeInput(
        trade=trade or _trade(),
        snapshot=_snapshot(),
        policy=sizing,
        constraints=PortfolioConstraints(),
    )


def test_fixed_allocation_sizes_to_fixed_weight() -> None:
    sizing = SizingPolicy(method=PositionSizingMethod.FIXED_ALLOCATION, fixed_allocation_pct=0.20)
    size = FixedAllocationSizer().size(_inputs(sizing))
    assert size.method is PositionSizingMethod.FIXED_ALLOCATION
    assert size.notional == pytest.approx(_EQUITY * Decimal("0.20"))
    assert size.quantity == pytest.approx(Decimal("200"))
    assert size.weight_pct == pytest.approx(0.20)


def test_fixed_allocation_capped_by_cash() -> None:
    sizing = SizingPolicy(method=PositionSizingMethod.FIXED_ALLOCATION, fixed_allocation_pct=0.80)
    inputs = SizeInput(
        trade=_trade(),
        snapshot=_snapshot(cash=Decimal("50000")),
        policy=sizing,
        constraints=PortfolioConstraints(),
    )
    size = FixedAllocationSizer().size(inputs)
    assert size.notional == pytest.approx(Decimal("50000"))


def test_fixed_allocation_zero_cash_is_zero() -> None:
    sizing = SizingPolicy(method=PositionSizingMethod.FIXED_ALLOCATION, fixed_allocation_pct=0.20)
    inputs = SizeInput(
        trade=_trade(),
        snapshot=_snapshot(cash=Decimal("0")),
        policy=sizing,
        constraints=PortfolioConstraints(),
    )
    assert FixedAllocationSizer().size(inputs).quantity == 0


def test_volatility_sizer_inverse_vol() -> None:
    sizing = SizingPolicy(method=PositionSizingMethod.VOLATILITY, vol_target_pct=0.10)
    low_vol = VolatilitySizer().size(_inputs(sizing, _trade(vol=0.50)))
    high_vol = VolatilitySizer().size(_inputs(sizing, _trade(vol=1.00)))
    assert low_vol.weight_pct > high_vol.weight_pct


def test_volatility_sizer_falls_back_with_warning() -> None:
    sizing = SizingPolicy(method=PositionSizingMethod.VOLATILITY, vol_target_pct=0.10)
    size = VolatilitySizer().size(_inputs(sizing, _trade(vol=None)))
    assert size.warnings
    assert size.weight_pct == pytest.approx(sizing.fixed_allocation_pct)


def test_risk_budget_sizes_to_risk_capital() -> None:
    sizing = SizingPolicy(
        method=PositionSizingMethod.RISK_BUDGET,
        risk_per_trade_pct=0.01,
        max_weight_pct=0.60,
    )
    # risk dollar = 1000; stop distance = 100 * 0.02 = 2 -> qty = 500.
    size = RiskBudgetSizer().size(_inputs(sizing, _trade(atr_pct=0.02)))
    assert size.quantity == pytest.approx(Decimal("500"))
    assert size.notional == pytest.approx(Decimal("50000"))


def test_risk_budget_respects_max_weight() -> None:
    sizing = SizingPolicy(
        method=PositionSizingMethod.RISK_BUDGET,
        risk_per_trade_pct=0.01,
        max_weight_pct=0.10,
    )
    size = RiskBudgetSizer().size(_inputs(sizing, _trade(atr_pct=0.005)))
    assert size.weight_pct == pytest.approx(0.10, abs=1e-6)


def test_risk_budget_missing_atr_falls_back_with_warning() -> None:
    sizing = SizingPolicy(method=PositionSizingMethod.RISK_BUDGET, risk_per_trade_pct=0.01)
    size = RiskBudgetSizer().size(_inputs(sizing, _trade(atr_pct=None)))
    assert size.warnings


def test_max_exposure_sizes_to_max_weight() -> None:
    sizing = SizingPolicy(method=PositionSizingMethod.MAX_EXPOSURE, max_weight_pct=0.25)
    size = MaxExposureSizer().size(_inputs(sizing))
    assert size.weight_pct == pytest.approx(0.25)


def test_sizer_for_dispatch() -> None:
    assert isinstance(sizer_for(PositionSizingMethod.VOLATILITY), VolatilitySizer)
    assert isinstance(sizer_for(PositionSizingMethod.RISK_BUDGET), RiskBudgetSizer)
    assert isinstance(sizer_for(PositionSizingMethod.MAX_EXPOSURE), MaxExposureSizer)
    assert isinstance(sizer_for(PositionSizingMethod.FIXED_ALLOCATION), FixedAllocationSizer)
