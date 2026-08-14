"""Phase 5 — the central Risk Engine gate (kill switch, statuses, data quality,
constraints, execution constraints) plus stress scenarios."""

from __future__ import annotations

from decimal import Decimal

import pytest

from qtrader.application.execution.liquidity import LiquidityModel
from qtrader.application.portfolio_mgmt.drawdown import KillSwitch, control_state
from qtrader.application.portfolio_mgmt.engine import PortfolioRiskEngine, make_liquidity_checker
from qtrader.application.portfolio_mgmt.models import (
    DrawdownProtection,
    GateVerdict,
    PortfolioConstraints,
    PortfolioSnapshot,
    PositionSizingMethod,
    ProposedTrade,
    SizingPolicy,
    StrategyControlStatus,
    snapshot_from_state,
)
from qtrader.domain.value_objects import TradeSide


def _snapshot(**overrides: object) -> PortfolioSnapshot:
    params = dict(
        equity=Decimal("100000"),
        cash=Decimal("100000"),
        gross_exposure_pct=0.0,
    )
    params.update(overrides)
    return snapshot_from_state(**params)


def _trade(
    symbol: str = "AAPL",
    price: str = "100",
    quantity: str = "100",
    atr_pct: float | None = 0.02,
    vol: float | None = 0.30,
    sector: str = "Tech",
    strategy_id: str = "s1",
) -> ProposedTrade:
    return ProposedTrade(
        strategy_id=strategy_id,
        symbol=symbol,
        side=TradeSide.BUY,
        reference_price=Decimal(price),
        quantity=Decimal(quantity),
        sector=sector,
        atr_pct=atr_pct,
        annualized_vol_pct=vol,
    )


def _engine(**overrides: object) -> PortfolioRiskEngine:
    return PortfolioRiskEngine(
        constraints=overrides.pop("constraints", PortfolioConstraints()),
        drawdown_protection=overrides.pop("drawdown_protection", DrawdownProtection()),
        sizing_policy=overrides.pop("sizing_policy", SizingPolicy()),
        kill_switch=overrides.pop("kill_switch", KillSwitch()),
        correlation_provider=overrides.pop("correlation_provider", None),
        liquidity_checker=overrides.pop("liquidity_checker", None),
        control_states=overrides.pop("control_states", None),
    )


def test_approves_clean_trade() -> None:
    engine = _engine()
    decision = engine.gate(_trade(), _snapshot())
    assert decision.verdict is GateVerdict.APPROVE
    assert decision.approved_quantity is not None
    assert decision.approved_quantity > 0


def test_kill_switch_rejects_everything() -> None:
    switch = KillSwitch()
    switch.trip("black swan")
    engine = _engine(kill_switch=switch)
    decision = engine.gate(_trade(), _snapshot())
    assert decision.verdict is GateVerdict.REJECT
    assert any("KILL SWITCH" in r for r in decision.reasons)


def test_suspended_strategy_rejected() -> None:
    states = {"s1": control_state("s1", StrategyControlStatus.SUSPENDED)}
    engine = _engine(control_states=states)
    decision = engine.gate(_trade(), _snapshot())
    assert decision.verdict is GateVerdict.REJECT
    assert any("SUSPENDED" in r for r in decision.reasons)


def test_reduced_strategy_gets_capped_size() -> None:
    states = {"s1": control_state("s1", StrategyControlStatus.REDUCED)}
    engine = _engine(control_states=states)
    decision = engine.gate(_trade(), _snapshot())
    assert decision.verdict is GateVerdict.MODIFY
    assert decision.approved_quantity is not None
    assert decision.approved_quantity <= Decimal("100")


def test_missing_price_is_data_quality_reject() -> None:
    engine = _engine()
    trade = _trade(price="0")
    decision = engine.gate(trade, _snapshot())
    assert decision.verdict is GateVerdict.REJECT
    assert any("data quality" in r for r in decision.reasons)


def test_missing_atr_rejects_risk_budget_buy() -> None:
    sizing = SizingPolicy(method=PositionSizingMethod.RISK_BUDGET)
    engine = _engine(sizing_policy=sizing)
    trade = _trade(atr_pct=None)
    decision = engine.gate(trade, _snapshot())
    assert decision.verdict is GateVerdict.REJECT
    assert any("ATR" in r for r in decision.reasons)


def test_portfolio_drawdown_breach_rejects() -> None:
    protection = DrawdownProtection(max_portfolio_drawdown_pct=0.20)
    engine = _engine(drawdown_protection=protection)
    decision = engine.gate(_trade(), _snapshot(drawdown_pct=0.30))
    assert decision.verdict is GateVerdict.REJECT
    assert any("portfolio drawdown" in r for r in decision.reasons)


def test_daily_loss_breach_rejects() -> None:
    protection = DrawdownProtection(max_daily_loss_pct=0.03)
    engine = _engine(drawdown_protection=protection)
    decision = engine.gate(_trade(), _snapshot(daily_pnl_pct=-0.05))
    assert decision.verdict is GateVerdict.REJECT
    assert any("daily loss" in r for r in decision.reasons)


def test_max_positions_breach_rejects() -> None:
    constraints = PortfolioConstraints(max_positions=1)
    engine = _engine(constraints=constraints)
    decision = engine.gate(_trade(), _snapshot(positions_count=1))
    assert decision.verdict is GateVerdict.REJECT
    assert any("max positions" in r for r in decision.reasons)


def test_sector_concentration_rejects() -> None:
    from qtrader.application.portfolio_mgmt.models import Holding

    constraints = PortfolioConstraints(max_sector_exposure_pct=0.40)
    engine = _engine(constraints=constraints)
    snapshot = _snapshot(
        positions=(
            Holding(
                symbol="MSFT",
                market_value=Decimal("35000"),
                weight_pct=0.35,
                sector="Tech",
            ),
        ),
        positions_count=1,
        gross_exposure_pct=0.35,
    )
    decision = engine.gate(_trade(sector="Tech"), snapshot)
    assert decision.verdict is GateVerdict.REJECT
    assert any("sector exposure" in r for r in decision.reasons)


def test_correlated_exposure_rejects() -> None:
    from qtrader.application.portfolio_mgmt.models import Holding

    constraints = PortfolioConstraints(max_correlated_exposure_pct=0.10)
    engine = _engine(
        constraints=constraints,
        correlation_provider=lambda _symbol, _others: {"MSFT": 0.9, "AAPL": 0.9},
    )
    snapshot = _snapshot(
        positions=(
            Holding(
                symbol="MSFT",
                market_value=Decimal("40000"),
                weight_pct=0.40,
                sector="Tech",
            ),
        ),
        positions_count=1,
        gross_exposure_pct=0.40,
    )
    decision = engine.gate(_trade(), snapshot)
    assert decision.verdict is GateVerdict.REJECT
    assert any("correlated exposure" in r for r in decision.reasons)


def test_oversized_position_is_modified_not_rejected() -> None:
    constraints = PortfolioConstraints(max_position_weight_pct=0.10)
    engine = _engine(constraints=constraints)
    # 0.25 weight proposal vs 0.10 cap.
    decision = engine.gate(_trade(quantity="250"), _snapshot())
    assert decision.verdict is GateVerdict.MODIFY
    assert decision.approved_quantity == pytest.approx(Decimal("100"))
    assert decision.modifications


def test_execution_liquidity_caps_size() -> None:
    from qtrader.application.execution.models import LiquidityAssumptions
    from qtrader.domain.value_objects import Interval, PriceBar

    assumptions = LiquidityAssumptions(max_notional_pct_adv=0.001, adv_window_bars=5)
    liquidity = LiquidityModel(assumptions)
    bars = [
        PriceBar(
            symbol="AAPL",
            interval=Interval.D1,
            ts=__import__("datetime").datetime(2024, 1, 1, 9, 30),
            open=Decimal("100"),
            high=Decimal("100"),
            low=Decimal("100"),
            close=Decimal("100"),
            volume=Decimal("100000"),
        )
        for _ in range(5)
    ]
    checker = make_liquidity_checker(liquidity, {"AAPL": bars})
    engine = _engine(liquidity_checker=checker)
    # Sized to 200 shares (0.20 x 100k / 100); ADV dollar = 10M, 0.1% = 10k ->
    # max 100 shares by the ADV-dollar budget.
    decision = engine.gate(_trade(quantity="1000"), _snapshot())
    assert decision.verdict is GateVerdict.MODIFY
    assert decision.approved_quantity is not None
    assert float(decision.approved_quantity) * 100 <= 100_000


def test_sizing_method_switch_updates_engine() -> None:
    engine = _engine(constraints=PortfolioConstraints(max_position_weight_pct=0.10))
    engine.set_sizing_method(PositionSizingMethod.MAX_EXPOSURE)
    decision = engine.gate(_trade(quantity="250"), _snapshot())
    assert decision.verdict is GateVerdict.MODIFY  # capped by max weight
    assert engine.sizing_policy.method is PositionSizingMethod.MAX_EXPOSURE


def test_update_control_state_overrides_status() -> None:
    engine = _engine()
    engine.update_control_state(control_state("s1", StrategyControlStatus.SUSPENDED))
    decision = engine.gate(_trade(), _snapshot())
    assert decision.verdict is GateVerdict.REJECT


def test_strategy_without_state_is_untouched() -> None:
    engine = _engine()
    decision = engine.gate(_trade(strategy_id="other"), _snapshot())
    assert decision.verdict is GateVerdict.APPROVE


# --------------------------------------------------------------------------- #
# Stress scenarios
# --------------------------------------------------------------------------- #


def _stress_engine() -> PortfolioRiskEngine:
    return PortfolioRiskEngine(
        constraints=PortfolioConstraints(
            max_position_weight_pct=0.25,
            max_portfolio_exposure_pct=0.80,
        ),
        drawdown_protection=DrawdownProtection(
            max_portfolio_drawdown_pct=0.20,
            max_daily_loss_pct=0.03,
            max_consecutive_losses=5,
        ),
        sizing_policy=SizingPolicy(method=PositionSizingMethod.VOLATILITY, vol_target_pct=0.10),
        kill_switch=KillSwitch(),
    )


def test_stress_high_volatility_shrinks_position() -> None:
    engine = _stress_engine()
    calm = engine.gate(_trade(vol=0.15), _snapshot())
    storm = engine.gate(_trade(vol=0.60), _snapshot())
    assert calm.approved_quantity is not None
    assert storm.approved_quantity is not None
    assert storm.approved_quantity < calm.approved_quantity


def test_stress_large_drawdown_halts_trading() -> None:
    engine = _stress_engine()
    decision = engine.gate(_trade(), _snapshot(drawdown_pct=0.40))
    assert decision.verdict is GateVerdict.REJECT
    assert any("drawdown" in r for r in decision.reasons)


def test_stress_correlated_moves_reject_double_exposure() -> None:
    from qtrader.application.portfolio_mgmt.models import Holding

    engine = PortfolioRiskEngine(
        constraints=PortfolioConstraints(max_correlated_exposure_pct=0.10),
        drawdown_protection=DrawdownProtection(),
        sizing_policy=SizingPolicy(),
        kill_switch=KillSwitch(),
        correlation_provider=lambda _symbol, _others: {"MSFT": 0.98},
    )
    snapshot = _snapshot(
        positions=(
            Holding(
                symbol="MSFT",
                market_value=Decimal("45000"),
                weight_pct=0.45,
                sector="Tech",
            ),
        ),
        positions_count=1,
        gross_exposure_pct=0.45,
    )
    decision = engine.gate(_trade(), snapshot)
    assert decision.verdict is GateVerdict.REJECT


def test_stress_execution_degradation_caps_then_rejects() -> None:
    from qtrader.application.execution.models import LiquidityAssumptions
    from qtrader.domain.value_objects import Interval, PriceBar

    assumptions = LiquidityAssumptions(max_notional_pct_adv=0.001, adv_window_bars=5)
    liquidity = LiquidityModel(assumptions)
    bars = [
        PriceBar(
            symbol="AAPL",
            interval=Interval.D1,
            ts=__import__("datetime").datetime(2024, 1, 1, 9, 30),
            open=Decimal("100"),
            high=Decimal("100"),
            low=Decimal("100"),
            close=Decimal("100"),
            volume=Decimal("50000"),
        )
        for _ in range(5)
    ]
    checker = make_liquidity_checker(liquidity, {"AAPL": bars})
    engine = _engine(liquidity_checker=checker)
    # ADV dollar = 5M; 0.1% = 5k -> proposal of 10k notional capped to 50 shares.
    decision = engine.gate(_trade(quantity="100"), _snapshot())
    assert decision.verdict is GateVerdict.MODIFY
    assert float(decision.approved_quantity) <= 50


def test_stress_daily_loss_circuit_breaks_new_positions() -> None:
    engine = _stress_engine()
    decision = engine.gate(_trade(), _snapshot(daily_pnl_pct=-0.06))
    assert decision.verdict is GateVerdict.REJECT
    assert any("daily loss" in r for r in decision.reasons)
