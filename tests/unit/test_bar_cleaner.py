"""Unit tests for the BarCleaner service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from qtrader.application.services.bar_cleaner import BarCleaner
from qtrader.domain.value_objects import Interval, PriceBar

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _bar(
    ts: datetime,
    *,
    open: str = "100",
    high: str = "105",
    low: str = "99",
    close: str = "103",
    volume: str = "1000",
) -> PriceBar:
    return PriceBar(
        symbol="AAPL",
        interval=Interval.M5,
        ts=ts,
        open=Decimal(open),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
    )


def test_dedup_keeps_last_and_sorts() -> None:
    earlier = _bar(NOW - timedelta(minutes=10))
    later = _bar(NOW - timedelta(minutes=5))
    duplicate = _bar(NOW - timedelta(minutes=10), close="101")
    report = BarCleaner().clean([later, earlier, duplicate], now=NOW, reject_stale=False)
    assert len(report.kept) == 2
    assert report.dropped == 1
    assert [b.ts for b in report.kept] == sorted(b.ts for b in report.kept)
    kept = {b.ts: b for b in report.kept}
    assert kept[earlier.ts].close == Decimal("101")


def test_zero_price_dropped() -> None:
    bad_zero = _bar(NOW - timedelta(minutes=10), open="0")
    good = _bar(NOW - timedelta(minutes=1))
    report = BarCleaner().clean([bad_zero, good], now=NOW, reject_stale=False)
    assert report.kept == [good]
    assert report.dropped == 1
    assert report.reasons["non-positive-price"] == 1


def test_stale_and_future_bars_dropped_live_path() -> None:
    stale = _bar(NOW - timedelta(minutes=30))
    future = _bar(NOW + timedelta(minutes=10))
    fresh = _bar(NOW - timedelta(minutes=1))
    report = BarCleaner().clean([stale, future, fresh], now=NOW, reject_stale=True)
    assert report.kept == [fresh]
    assert report.reasons["stale-bar"] == 1
    assert report.reasons["future-bar"] == 1


def test_backfill_keeps_stale_history() -> None:
    old = _bar(NOW - timedelta(days=2))
    report = BarCleaner().clean([old], now=NOW, reject_stale=False)
    assert report.kept == [old]


def test_volume_spike_dropped() -> None:
    normal_a = _bar(NOW - timedelta(minutes=10), volume="1000")
    normal_b = _bar(NOW - timedelta(minutes=8), volume="1500")
    spike = _bar(NOW - timedelta(minutes=5), volume="500000")  # ~400x median
    cleaner = BarCleaner(max_volume_spike_factor=Decimal("100"))
    report = cleaner.clean([normal_a, normal_b, spike], now=NOW, reject_stale=False)
    assert set(report.kept) == {normal_a, normal_b}
    assert report.reasons["volume-spike"] == 1


def test_naive_ts_normalized_to_utc() -> None:
    naive = _bar(NOW - timedelta(minutes=5))
    naive = PriceBar(
        symbol=naive.symbol,
        interval=naive.interval,
        ts=naive.ts.replace(tzinfo=None),
        open=naive.open,
        high=naive.high,
        low=naive.low,
        close=naive.close,
        volume=naive.volume,
    )
    report = BarCleaner().clean([naive], now=NOW, reject_stale=False)
    assert report.kept[0].ts.tzinfo is not None
