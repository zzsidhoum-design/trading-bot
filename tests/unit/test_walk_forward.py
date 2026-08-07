"""Unit tests for the walk-forward out-of-sample validator."""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from qtrader.application.services.indicators import IndicatorEngine
from qtrader.application.services.risk_calculator import RiskCalculator, RiskPolicy
from qtrader.application.services.walk_forward import (
    STRATEGY_LABEL,
    WalkForwardValidator,
)
from qtrader.domain.value_objects import TradingMode
from tests.unit.fakes_phase6 import (
    FakePerformanceRepository,
    FakePriceRepository,
    bar,
)


def _trend_bars(symbol: str, days: int = 160, start_price: float = 100.0, seed: int = 7) -> list:
    rng = random.Random(seed)
    start = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
    bars: list = []
    price = float(start_price)
    for i in range(days):
        drift = 0.0015 if i % 3 == 0 else -0.0015
        price *= 1.0 + drift + rng.gauss(0, 0.018)
        ts = start + timedelta(days=i)
        bars.append(
            bar(
                symbol,
                ts,
                open=f"{price:.2f}",
                high=f"{price * 1.01:.2f}",
                low=f"{price * 0.99:.2f}",
                close=f"{price:.2f}",
                volume="1000000",
            )
        )
    return bars


def _validator(bars: dict[str, list]) -> tuple[WalkForwardValidator, FakePerformanceRepository]:
    performance = FakePerformanceRepository()
    validator = WalkForwardValidator(
        prices=FakePriceRepository(bars),
        performance=performance,
        risk_calculator=RiskCalculator(RiskPolicy(risk_per_trade_pct=0.01)),
        indicator_engine=IndicatorEngine(),
    )
    return validator, performance


@pytest.mark.asyncio
async def test_walk_forward_produces_oos_summary() -> None:
    bars = {"AAPL": _trend_bars("AAPL", days=240)}
    validator, performance = _validator(bars)
    summary = await validator.validate(
        symbols=["AAPL"],
        start=date(2026, 1, 1),
        end=date(2026, 8, 28),
initial_capital=Decimal("100000"),
    )
    assert summary is not None
    assert summary.strategy == STRATEGY_LABEL
    assert summary.mode is TradingMode.BACKTEST
    assert summary.trades_count is not None
    assert summary.final_equity is not None


@pytest.mark.asyncio
async def test_walk_forward_persists_under_label() -> None:
    bars = {"AAPL": _trend_bars("AAPL", days=200)}
    validator, performance = _validator(bars)
    await validator.validate(
        symbols=["AAPL"],
        start=date(2026, 1, 1),
        end=date(2026, 7, 20),
initial_capital=Decimal("100000"),
    )
    latest = await performance.latest_for_strategy(STRATEGY_LABEL, TradingMode.BACKTEST)
    assert latest is not None
    assert latest.strategy == STRATEGY_LABEL


@pytest.mark.asyncio
async def test_walk_forward_returns_none_without_enough_bars() -> None:
    bars = {"AAPL": _trend_bars("AAPL", days=5)}
    validator, performance = _validator(bars)
    summary = await validator.validate(
        symbols=["AAPL"],
        start=date(2026, 1, 1),
        end=date(2026, 1, 5),
        initial_capital=Decimal("100000"),
    )
    assert summary is None


def test_chain_curve_compounds_across_folds() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    fold1 = [
        (start + timedelta(days=i), Decimal("1000") * (Decimal("1.10") if i else 1))
        for i in range(2)
    ]
    fold2 = [
        (start + timedelta(days=2 + i), Decimal("1000") * (Decimal("1.10") if i else 1))
        for i in range(2)
    ]
    curve, equity = WalkForwardValidator._chain_curve([], fold1, Decimal("1000"))
    assert equity == Decimal("1100")
    curve, equity = WalkForwardValidator._chain_curve(curve, fold2, equity)
    assert equity == Decimal("1210")  # 1000 * 1.10 * 1.10, not 1000 + 0.10 + 0.10
    assert curve[-1][0] == start + timedelta(days=3)
    assert curve[-1][1] == Decimal("1210")
