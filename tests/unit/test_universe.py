"""Unit tests for point-in-time universe selection."""

from __future__ import annotations

from datetime import UTC, date, datetime

from qtrader.application.services.universe import (
    listing_date_from_first_bar,
    point_in_time_universe,
)

LISTINGS = {
    "AAPL": date(2021, 9, 1),
    "GEV": date(2024, 3, 27),
    "SOLV": date(2024, 3, 26),
    "Q": date(2025, 10, 27),
    "FDXF": date(2026, 5, 27),
}


def test_returns_only_symbols_listed_by_as_of() -> None:
    assert point_in_time_universe(LISTINGS, date(2022, 1, 1)) == ["AAPL"]
    assert point_in_time_universe(LISTINGS, date(2024, 6, 1)) == ["AAPL", "GEV", "SOLV"]
    assert point_in_time_universe(LISTINGS, date(2026, 8, 6)) == [
        "AAPL", "FDXF", "GEV", "Q", "SOLV",
    ]


def test_includes_symbol_listed_exactly_on_as_of() -> None:
    assert point_in_time_universe(LISTINGS, date(2024, 3, 26)) == ["AAPL", "SOLV"]


def test_empty_input() -> None:
    assert point_in_time_universe({}, date(2026, 1, 1)) == []


def test_listing_date_from_first_bar() -> None:
    ts = datetime(2024, 3, 27, 13, 30, tzinfo=UTC)
    assert listing_date_from_first_bar(ts) == date(2024, 3, 27)
