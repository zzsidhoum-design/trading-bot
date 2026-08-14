"""Phase 5 — correlation & concentration monitoring."""

from __future__ import annotations

from decimal import Decimal

import pytest

from qtrader.application.portfolio_mgmt.correlation import (
    average_strategy_correlation,
    concentration_index,
    correlated_exposure,
    portfolio_concentration,
    proposed_correlated_exposure,
    proposed_sector_exposure,
    sector_exposures,
    strategy_correlation,
)
from qtrader.application.portfolio_mgmt.models import (
    Holding,
    PortfolioSnapshot,
    PositionSize,
    PositionSizingMethod,
    ProposedTrade,
    snapshot_from_state,
)
from qtrader.domain.value_objects import TradeSide


def _holdings() -> tuple[Holding, ...]:
    return (
        Holding(symbol="AAPL", market_value=Decimal("30000"), weight_pct=0.30, sector="Tech"),
        Holding(symbol="MSFT", market_value=Decimal("20000"), weight_pct=0.20, sector="Tech"),
        Holding(symbol="XOM", market_value=Decimal("10000"), weight_pct=0.10, sector="Energy"),
    )


def _snapshot() -> PortfolioSnapshot:
    return snapshot_from_state(
        equity=Decimal("100000"),
        cash=Decimal("40000"),
        gross_exposure_pct=0.60,
        positions=_holdings(),
    )


def _trade(symbol: str = "NVDA", sector: str = "Tech") -> ProposedTrade:
    return ProposedTrade(
        strategy_id="s1",
        symbol=symbol,
        side=TradeSide.BUY,
        reference_price=Decimal("100"),
        quantity=Decimal("100"),
        sector=sector,
    )


def _size(weight: float = 0.10) -> PositionSize:
    return PositionSize(
        symbol="NVDA",
        quantity=Decimal("100"),
        notional=Decimal("10000"),
        weight_pct=weight,
        method=PositionSizingMethod.FIXED_ALLOCATION,
    )


def test_concentration_index_uniform_is_zero() -> None:
    assert concentration_index([0.25, 0.25, 0.25, 0.25]) == pytest.approx(0.0, abs=1e-9)


def test_concentration_index_single_asset_is_one() -> None:
    assert concentration_index([1.0]) == pytest.approx(1.0)


def test_concentration_index_concentrated_book() -> None:
    assert concentration_index([0.9, 0.05, 0.05]) > concentration_index([0.34, 0.33, 0.33])


def test_sector_exposures_aggregates() -> None:
    exposures = sector_exposures(_snapshot())
    assert exposures["Tech"] == pytest.approx(0.50)
    assert exposures["Energy"] == pytest.approx(0.10)


def test_proposed_sector_exposure_adds_weight() -> None:
    exposures = proposed_sector_exposure(_snapshot(), _trade(sector="Tech"), _size(0.10))
    assert exposures["Tech"] == pytest.approx(0.60)


def test_correlated_exposure_high_correlation() -> None:
    def provider(_symbol: str, _others: object) -> dict[str, float]:
        return {"AAPL": 0.1, "MSFT": 0.1, "XOM": 0.1}

    assert correlated_exposure(_snapshot(), provider, threshold=0.7) == pytest.approx(0.0)


def test_correlated_exposure_counts_correlated_holdings() -> None:
    def provider(symbol: str, _others: object) -> dict[str, float]:
        if symbol == "AAPL":
            return {"MSFT": 0.95}
        if symbol == "MSFT":
            return {"AAPL": 0.95}
        return {"AAPL": 0.1}

    assert correlated_exposure(_snapshot(), provider, threshold=0.7) == pytest.approx(0.50)


def test_proposed_correlated_exposure_with_hint() -> None:
    def provider(_symbol: str, _others: object) -> dict[str, float]:
        return {"AAPL": 0.0}

    trade = _trade()
    # High explicit correlation hint -> counted against the correlated budget.
    from dataclasses import replace

    trade = replace(trade, correlation_to_portfolio=0.95)
    exposure = proposed_correlated_exposure(_snapshot(), trade, _size(0.10), provider, 0.7)
    assert exposure == pytest.approx(0.10)


def test_portfolio_concentration() -> None:
    assert 0.0 < portfolio_concentration(_snapshot()) < 1.0


def test_strategy_correlation_positive() -> None:
    returns = {"a": [0.01, -0.01] * 10, "b": [0.01, -0.01] * 10}
    assert strategy_correlation(returns, "a", "b") == pytest.approx(1.0, abs=1e-9)


def test_strategy_correlation_missing_series_is_none() -> None:
    assert strategy_correlation({"a": [0.01]}, "a", "missing") is None


def test_average_strategy_correlation() -> None:
    returns = {
        "a": [0.01, -0.01] * 10,
        "b": [0.01, -0.01] * 10,
        "c": [-0.01, 0.01] * 10,
    }
    # a correlates 1.0 with b and 1.0 (abs) with c -> average 1.0.
    assert average_strategy_correlation(returns, "a") == pytest.approx(1.0, abs=1e-9)


def test_average_strategy_correlation_single_is_zero() -> None:
    assert average_strategy_correlation({"a": [0.01] * 5}, "a") == 0.0
