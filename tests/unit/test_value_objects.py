"""Unit tests for value objects — money, percentages, price bars, order plans."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from qtrader.domain.value_objects import (
    Interval,
    Money,
    OrderPlan,
    OrderType,
    Percentage,
    PriceBar,
    TradeSide,
)


class TestMoney:
    def test_accepts_numeric_inputs(self) -> None:
        assert Money("10.5").amount == Decimal("10.500000")
        assert Money(3).amount == Decimal("3.000000")
        assert Money(Decimal("7.25")).amount == Decimal("7.250000")

    def test_arithmetic(self) -> None:
        assert (Money("10") + Money("5")).amount == Decimal("15.000000")
        assert (Money("10") - Money("5")).amount == Decimal("5.000000")
        assert (Money("2") * 3).amount == Decimal("6.000000")
        assert (-Money("2")).amount == Decimal("-2.000000")

    def test_rejects_non_numeric(self) -> None:
        with pytest.raises(TypeError):
            Money("abc")  # type: ignore[arg-type]


class TestPercentage:
    def test_bounds(self) -> None:
        Percentage("0")
        Percentage("1")
        Percentage("0.02")

    def test_out_of_range(self) -> None:
        with pytest.raises(ValueError):
            Percentage("1.5")
        with pytest.raises(ValueError):
            Percentage("-0.1")

    def test_from_basis_points(self) -> None:
        assert Percentage.from_basis_points(200).value == Decimal("0.0200")


class TestPriceBar:
    def test_valid_bar(self) -> None:
        bar = PriceBar(
            symbol="AAPL",
            interval=Interval.M5,
            ts=datetime(2026, 8, 1, 12, 0),
            open=Decimal("100"),
            high=Decimal("105"),
            low=Decimal("99"),
            close=Decimal("104"),
            volume=Decimal("1000"),
        )
        assert bar.ts.tzinfo == UTC

    def test_invalid_ohlc(self) -> None:
        with pytest.raises(ValueError):
            PriceBar(
                symbol="AAPL",
                interval=Interval.M5,
                ts=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
                open=Decimal("100"),
                high=Decimal("90"),
                low=Decimal("99"),
                close=Decimal("104"),
                volume=Decimal("1000"),
            )

    def test_negative_volume(self) -> None:
        with pytest.raises(ValueError):
            PriceBar(
                symbol="AAPL",
                interval=Interval.M5,
                ts=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
                open=Decimal("100"),
                high=Decimal("105"),
                low=Decimal("99"),
                close=Decimal("104"),
                volume=Decimal("-1"),
            )


class TestOrderPlan:
    def test_requires_positive_quantity(self) -> None:
        with pytest.raises(ValueError):
            OrderPlan(
                symbol="AAPL",
                side=TradeSide.BUY,
                quantity=Decimal("0"),
                order_type=OrderType.MARKET,
                limit_price=None,
                stop_loss=Decimal("90"),
                take_profit=Decimal("120"),
                risk_per_trade=Percentage("0.01"),
                estimated_exposure=Percentage("0.05"),
            )
