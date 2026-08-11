"""Unit tests for the US trading-session calendar (MarketHours)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfoNotFoundError

import pytest

from qtrader.application.services.market_hours import MarketHours


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# 2026-08-10 is a Monday, 2026-08-08 a Saturday (EDT = UTC-4).
# 2026-01-05 is a Monday, 2026-01-01 a Thursday holiday (EST = UTC-5).


def test_weekday_during_session_is_open() -> None:
    hours = MarketHours()
    assert hours.is_open(_utc(2026, 8, 10, 14, 0))  # 10:00 EDT


def test_before_open_is_closed() -> None:
    hours = MarketHours()
    assert not hours.is_open(_utc(2026, 8, 10, 12, 0))  # 08:00 EDT
    assert not hours.is_open(_utc(2026, 8, 10, 13, 29))  # 09:29 EDT


def test_after_close_is_closed() -> None:
    hours = MarketHours()
    assert not hours.is_open(_utc(2026, 8, 10, 20, 1))  # 16:01 EDT
    assert not hours.is_open(_utc(2026, 8, 10, 23, 59))


def test_weekend_is_closed_all_day() -> None:
    hours = MarketHours()
    assert not hours.is_open(_utc(2026, 8, 8, 15, 0))  # saturday, mid-session
    assert not hours.is_open(_utc(2026, 8, 9, 15, 0))  # sunday


def test_default_holiday_is_closed() -> None:
    hours = MarketHours()
    assert not hours.is_open(_utc(2026, 1, 1, 15, 30))  # new year's day, 10:30 EST


def test_injected_holiday_closes_a_weekday() -> None:
    hours = MarketHours(holidays=[date(2026, 8, 10)])
    assert not hours.is_open(_utc(2026, 8, 10, 14, 0))


def test_always_open_disables_calendar() -> None:
    hours = MarketHours(always_open=True)
    assert hours.is_open(_utc(2026, 8, 8, 3, 0))  # saturday 3am UTC
    assert hours.is_open(_utc(2026, 1, 1, 15, 0))  # holiday


def test_dst_shifts_utc_session_bounds() -> None:
    hours = MarketHours()
    # Winter (EST = UTC-5): 09:30 local == 14:30 UTC.
    assert not hours.is_open(_utc(2026, 1, 5, 14, 29))
    assert hours.is_open(_utc(2026, 1, 5, 14, 30))
    # Summer (EDT = UTC-4): 09:30 local == 13:30 UTC.
    assert not hours.is_open(_utc(2026, 8, 10, 13, 29))
    assert hours.is_open(_utc(2026, 8, 10, 13, 30))


def test_session_bounds_return_utc_open_close() -> None:
    hours = MarketHours()
    bounds = hours.session_bounds(_utc(2026, 8, 10, 14, 0))
    assert bounds == (_utc(2026, 8, 10, 13, 30), _utc(2026, 8, 10, 20, 0))
    assert hours.session_bounds(_utc(2026, 8, 8, 14, 0)) is None


def test_next_open_after_weekend_is_monday_open() -> None:
    hours = MarketHours()
    assert hours.next_open(_utc(2026, 8, 8, 12, 0)) == _utc(2026, 8, 10, 13, 30)


def test_next_open_after_close_rolls_to_next_day() -> None:
    hours = MarketHours()
    # Friday 16:01 EDT already past close -> Monday 09:30 EDT.
    assert hours.next_open(_utc(2026, 8, 7, 20, 1)) == _utc(2026, 8, 10, 13, 30)


def test_next_open_skips_holiday_weekday() -> None:
    hours = MarketHours(holidays=[date(2026, 1, 1)])
    # Thursday Jan 1 2026 is a holiday -> Friday Jan 2 open (14:30 EST).
    assert hours.next_open(_utc(2026, 1, 1, 12, 0)) == _utc(2026, 1, 2, 14, 30)


def test_invalid_config_raises() -> None:
    with pytest.raises(ValueError):
        MarketHours(open_time="oops")
    with pytest.raises(ValueError):
        MarketHours(open_time="16:00", close_time="09:30")
    with pytest.raises(ZoneInfoNotFoundError):
        MarketHours(timezone="Not/AZone")


def test_custom_timezone_and_hours() -> None:
    hours = MarketHours(
        timezone="UTC", open_time="08:00", close_time="16:00"
    )
    assert hours.is_open(_utc(2026, 8, 10, 10, 0))
    assert not hours.is_open(_utc(2026, 8, 10, 7, 59))
