"""Unit tests for the BarValidator data validation layer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from qtrader.application.services.bar_validator import BarValidator
from qtrader.domain.value_objects import Interval, PriceBar


def _bar(
    ts: datetime,
    *,
    symbol: str = "AAPL",
    open: str = "100",
    high: str = "105",
    low: str = "99",
    close: str = "103",
    volume: str = "1000",
) -> PriceBar:
    return PriceBar(
        symbol=symbol,
        interval=Interval.D1,
        ts=ts,
        open=Decimal(open),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
    )


def test_ohlc_low_above_open_close_rejected() -> None:
    day = datetime(2026, 8, 3, 13, 30, tzinfo=UTC)
    bad = _bar(day, open="100", close="102", high="108", low="105")
    good = _bar(day + timedelta(days=1))
    report = BarValidator().validate([bad, good])
    assert report.kept == [good]
    assert report.reasons["ohlc-low-above-open-close"] == 1


def test_large_single_bar_move_rejected_by_default() -> None:
    d0 = datetime(2026, 8, 3, 13, 30, tzinfo=UTC)
    normal = _bar(d0, close="100")
    split_artifact = _bar(d0 + timedelta(days=1), open="49", close="48", high="50", low="47")
    report = BarValidator().validate([normal, split_artifact])
    assert report.kept == [normal]
    assert report.reasons["large-single-bar-move"] == 1


def test_large_move_kept_when_reject_disabled() -> None:
    d0 = datetime(2026, 8, 3, 13, 30, tzinfo=UTC)
    normal = _bar(d0, close="100")
    crash = _bar(d0 + timedelta(days=1), open="49", close="48", high="50", low="47")
    validator = BarValidator(reject_large_moves=False)
    report = validator.validate([normal, crash])
    assert report.kept == [normal, crash]
    assert report.reasons["large-single-bar-move-flagged"] == 1


def test_prev_close_by_symbol_applies_to_single_bar() -> None:
    day = datetime(2026, 8, 3, 13, 30, tzinfo=UTC)
    bar = _bar(day, open="41", close="40", high="42", low="39")
    validator = BarValidator()
    report = validator.validate([bar], prev_close_by_symbol={"AAPL": Decimal("100")})
    assert report.kept == []
    assert report.reasons["large-single-bar-move"] == 1


def test_weekend_daily_bar_rejected() -> None:
    sat = datetime(2026, 8, 8, 13, 30, tzinfo=UTC)  # Saturday
    report = BarValidator().validate([_bar(sat)])
    assert report.kept == []
    assert report.reasons["weekend-bar"] == 1


def test_calendar_gap_detected_and_reported() -> None:
    d0 = datetime(2026, 8, 3, 13, 30, tzinfo=UTC)  # Monday
    bars = [
        _bar(d0),
        _bar(d0 + timedelta(days=2)),  # Wednesday
        _bar(d0 + timedelta(days=42)),  # Monday, ~6 weeks later
    ]
    report = BarValidator().validate(bars)
    assert len(report.gaps) == 1
    symbol, after, before, days = report.gaps[0]
    assert symbol == "AAPL"
    assert days == 40


def test_batch_infers_previous_close_chronologically() -> None:
    d0 = datetime(2026, 8, 3, 13, 30, tzinfo=UTC)
    normal = _bar(d0, close="100")
    normal_b = _bar(d0 + timedelta(days=1), close="101")
    spike = _bar(d0 + timedelta(days=2), open="202", close="203", high="205", low="99")
    report = BarValidator().validate([normal, normal_b, spike])
    assert report.kept == [normal, normal_b]
    assert report.reasons["large-single-bar-move"] == 1


def test_non_daily_weekend_not_rejected_by_default_interval() -> None:
    sat = datetime(2026, 8, 8, 13, 30, tzinfo=UTC)
    bar = PriceBar(
        symbol="AAPL",
        interval=Interval.M5,
        ts=sat,
        open=Decimal("100"),
        high=Decimal("105"),
        low=Decimal("99"),
        close=Decimal("103"),
        volume=Decimal("1000"),
    )
    report = BarValidator().validate([bar])
    assert report.kept == [bar]
