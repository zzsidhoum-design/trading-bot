"""Unit tests for the pure risk engine (RiskCalculator)."""

from __future__ import annotations

from decimal import Decimal

from qtrader.application.services.risk_calculator import RiskCalculator, RiskInputs, RiskPolicy
from qtrader.domain.value_objects import Decision


def _inputs(**overrides) -> RiskInputs:
    base: dict = dict(
        decision=Decision.BUY,
        symbol="AAPL",
        entry_price=Decimal("100"),
        atr=Decimal("2"),
        equity=Decimal("100000"),
        current_exposure_pct=0.1,
        open_positions=2,
        sector_exposure_pct=0.1,
        adv_daily=Decimal("10000000"),
        cooldown_remaining_minutes=0,
        daily_pnl_pct=0.0,
        trades_today=1,
    )
    base.update(overrides)
    return RiskInputs(**base)


def test_buy_approved_sizing_and_brackets() -> None:
    assessment = RiskCalculator(RiskPolicy()).assess(_inputs())
    assert assessment.approved is True
    assert assessment.rejection_reasons == []
    assert assessment.position_size is not None and assessment.position_size > 0
    assert assessment.stop_loss is not None and assessment.stop_loss < Decimal("100")
    assert assessment.take_profit is not None and assessment.take_profit > Decimal("100")
    assert assessment.risk_per_trade_pct == Decimal("0.0100")


def test_buy_sizes_for_explicit_bracket_distance() -> None:
    # A 3% stop bracket on a $100 entry is a $3 stop distance. 1% risk on
    # $100k = $1000 -> 333.3333 shares (~$33.3k notional) with the stop at $97
    # and target at $106, exactly matching the 3%/6% bracket.
    assessment = RiskCalculator(RiskPolicy(risk_per_trade_pct=0.01)).assess(
        _inputs(entry_price=Decimal("100"), atr_stop_distance=Decimal("3"))
    )
    assert assessment.approved is True
    assert assessment.position_size == Decimal("333.3333")
    assert assessment.stop_loss == Decimal("97")
    assert assessment.take_profit == Decimal("106")
    assert assessment.risk_per_trade_pct == Decimal("0.0100")


def test_sell_without_position_rejected() -> None:
    assessment = RiskCalculator(RiskPolicy()).assess(_inputs(decision=Decision.SELL))
    assert assessment.approved is False
    assert "no open position to close" in assessment.rejection_reasons


def test_buy_with_existing_position_rejected() -> None:
    assessment = RiskCalculator(RiskPolicy()).assess(
        _inputs(position_quantity=Decimal("10"))
    )
    assert assessment.approved is False
    assert any("position already open" in r for r in assessment.rejection_reasons)


def test_buy_with_existing_position_allowed_by_policy() -> None:
    policy = RiskPolicy(allow_add_to_position=True)
    assessment = RiskCalculator(policy).assess(
        _inputs(position_quantity=Decimal("10"))
    )
    assert assessment.approved is True
    assert assessment.rejection_reasons == []


def test_sell_with_position_approved() -> None:
    assessment = RiskCalculator(RiskPolicy()).assess(
        _inputs(decision=Decision.SELL, position_quantity=Decimal("10"))
    )
    assert assessment.approved is True
    assert assessment.position_size == Decimal("10")


def test_max_positions_rejected() -> None:
    policy = RiskPolicy(max_positions=5)
    assessment = RiskCalculator(policy).assess(_inputs(open_positions=5))
    assert assessment.approved is False
    assert any("max positions" in r for r in assessment.rejection_reasons)


def test_projected_exposure_limit_rejected() -> None:
    assessment = RiskCalculator(RiskPolicy()).assess(_inputs(current_exposure_pct=0.79))
    assert assessment.approved is False
    assert any("exposure" in r for r in assessment.rejection_reasons)


def test_sector_limit_rejected() -> None:
    policy = RiskPolicy(per_sector_limit_pct=0.3)
    assessment = RiskCalculator(policy).assess(_inputs(sector_exposure_pct=0.4))
    assert assessment.approved is False
    assert any("sector" in r for r in assessment.rejection_reasons)


def test_adv_liquidity_rejected() -> None:
    policy = RiskPolicy(max_position_pct_adv=0.01)
    assessment = RiskCalculator(policy).assess(
        _inputs(adv_daily=Decimal("1000"), equity=Decimal("100000000"))
    )
    assert assessment.approved is False
    assert any("ADV" in r for r in assessment.rejection_reasons)


def test_cooldown_rejected() -> None:
    policy = RiskPolicy(min_cooldown_minutes=5)
    assessment = RiskCalculator(policy).assess(_inputs(cooldown_remaining_minutes=4))
    assert assessment.approved is False
    assert any("cooldown" in r for r in assessment.rejection_reasons)


def test_max_trades_per_day_rejected() -> None:
    policy = RiskPolicy(max_trades_per_day=10)
    assessment = RiskCalculator(policy).assess(_inputs(trades_today=10))
    assert assessment.approved is False
    assert any("trades per day" in r for r in assessment.rejection_reasons)


def test_daily_loss_limit_rejected() -> None:
    policy = RiskPolicy(max_daily_loss_pct=0.03)
    assessment = RiskCalculator(policy).assess(_inputs(daily_pnl_pct=-0.05))
    assert assessment.approved is False
    assert any("daily loss" in r for r in assessment.rejection_reasons)


def test_with_policy_override() -> None:
    calculator = RiskCalculator(RiskPolicy(max_positions=10)).with_policy(max_positions=3)
    assert calculator.policy.max_positions == 3
    assert calculator.policy.max_daily_loss_pct == 0.03
