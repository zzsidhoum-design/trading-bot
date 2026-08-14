"""Phase 5 — portfolio constraints (position/exposure/sector/correlation/leverage)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from qtrader.application.portfolio_mgmt.constraints import ConstraintEngine
from qtrader.application.portfolio_mgmt.models import (
    Holding,
    PortfolioConstraints,
    PortfolioSnapshot,
    PositionSize,
    PositionSizingMethod,
    ProposedTrade,
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
    sector: str = "Tech",
    price: str = "100",
    quantity: str = "100",
) -> ProposedTrade:
    return ProposedTrade(
        strategy_id="s1",
        symbol=symbol,
        side=TradeSide.BUY,
        reference_price=Decimal(price),
        quantity=Decimal(quantity),
        sector=sector,
    )


def _size(symbol: str = "AAPL", weight: float = 0.10, quantity: str = "100") -> PositionSize:
    return PositionSize(
        symbol=symbol,
        quantity=Decimal(quantity),
        notional=Decimal(quantity) * Decimal("100"),
        weight_pct=weight,
        method=PositionSizingMethod.FIXED_ALLOCATION,
    )


def test_approves_within_all_limits() -> None:
    engine = ConstraintEngine(PortfolioConstraints())
    verdict = engine.evaluate(_snapshot(), _trade(), _size(weight=0.10))
    assert verdict.approved
    assert verdict.violations == ()


def test_caps_oversized_position_to_max_weight() -> None:
    constraints = PortfolioConstraints(max_position_weight_pct=0.25)
    engine = ConstraintEngine(constraints)
    verdict = engine.evaluate(_snapshot(), _trade(sector=None), _size(weight=0.50, quantity="500"))
    assert verdict.approved
    assert verdict.cap_quantity == pytest.approx(Decimal("250"))


def test_rejects_breaching_portfolio_exposure() -> None:
    constraints = PortfolioConstraints(max_portfolio_exposure_pct=0.80)
    engine = ConstraintEngine(constraints)
    verdict = engine.evaluate(
        _snapshot(gross_exposure_pct=0.75),
        _trade(),
        _size(weight=0.10),
    )
    assert not verdict.approved
    assert any("exposure" in v for v in verdict.violations)


def test_rejects_sector_concentration() -> None:
    constraints = PortfolioConstraints(max_sector_exposure_pct=0.40)
    engine = ConstraintEngine(constraints)
    holdings = (
        Holding(
            symbol="MSFT",
            market_value=Decimal("30000"),
            weight_pct=0.30,
            sector="Tech",
        ),
    )
    verdict = engine.evaluate(
        _snapshot(positions=holdings, positions_count=1, gross_exposure_pct=0.30),
        _trade(sector="Tech"),
        _size(weight=0.20),
    )
    assert not verdict.approved
    assert any("sector exposure" in v for v in verdict.violations)


def test_rejects_correlated_exposure() -> None:
    constraints = PortfolioConstraints(max_correlated_exposure_pct=0.10, correlation_threshold=0.7)
    engine = ConstraintEngine(constraints)
    holdings = (
        Holding(
            symbol="MSFT",
            market_value=Decimal("40000"),
            weight_pct=0.40,
            sector="Tech",
        ),
    )
    snapshot = _snapshot(positions=holdings, positions_count=1, gross_exposure_pct=0.40)

    def high_corr(_symbol: str, _others: object) -> dict[str, float]:
        return {"MSFT": 0.95}

    verdict = engine.evaluate(snapshot, _trade(symbol="AAPL"), _size(weight=0.20), high_corr)
    assert not verdict.approved
    assert any("correlated exposure" in v for v in verdict.violations)


def test_rejects_when_max_positions_reached() -> None:
    constraints = PortfolioConstraints(max_positions=2)
    engine = ConstraintEngine(constraints)
    verdict = engine.evaluate(
        _snapshot(positions_count=2),
        _trade(),
        _size(weight=0.10),
    )
    assert not verdict.approved
    assert any("max positions" in v for v in verdict.violations)


def test_rejects_breaching_turnover() -> None:
    constraints = PortfolioConstraints(max_turnover_pct=0.30)
    engine = ConstraintEngine(constraints)
    verdict = engine.evaluate(
        _snapshot(turnover_30d_pct=0.25),
        _trade(),
        _size(weight=0.10),
    )
    assert not verdict.approved
    assert any("turnover" in v for v in verdict.violations)


def test_rejects_breaching_leverage() -> None:
    constraints = PortfolioConstraints(
        max_leverage_pct=0.0,
        max_portfolio_exposure_pct=2.0,
    )
    engine = ConstraintEngine(constraints)
    # Existing gross exposure at 95%; any new weight pushes it past 100% of
    # equity and there is no leverage budget left -> hard reject.
    verdict = engine.evaluate(
        _snapshot(gross_exposure_pct=0.95, leverage_pct=0.0),
        _trade(),
        _size(weight=0.10),
    )
    assert not verdict.approved
    assert any("leverage" in v for v in verdict.violations)


def test_rejects_zero_equity() -> None:
    engine = ConstraintEngine(PortfolioConstraints())
    verdict = engine.evaluate(_snapshot(equity=Decimal("0")), _trade(), _size(weight=0.10))
    assert not verdict.approved


def test_cap_respects_remaining_leverage_budget() -> None:
    constraints = PortfolioConstraints(
        max_leverage_pct=0.10,
        max_position_weight_pct=0.25,
        max_portfolio_exposure_pct=2.0,
    )
    engine = ConstraintEngine(constraints)
    # Existing gross exposure 105% (5% leveraged); max leverage 10% leaves 5%
    # of budget, so a 20% position is capped to the remaining 5% -> 50 shares.
    verdict = engine.evaluate(
        _snapshot(gross_exposure_pct=1.05, leverage_pct=0.05),
        _trade(),
        _size(weight=0.20, quantity="200"),
    )
    assert verdict.approved
    assert verdict.cap_quantity == pytest.approx(Decimal("50"))
