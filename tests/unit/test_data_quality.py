"""Unit tests for the DataQualityAuditor service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from qtrader.application.services.data_quality import DataQualityAuditor
from qtrader.application.services.market_hours import MarketHours
from qtrader.domain.ports import DataQualityRepository

NOW = datetime(2026, 8, 1, 20, 0, tzinfo=UTC)  # 16:00 ET, Saturday

# Last 5 completed sessions under always_open=True, now=Aug 1 16:00 ET:
# Aug 1, Jul 31, Jul 30, Jul 29, Jul 28.
SESSIONS = [
    datetime(2026, 8, 1).date(),
    datetime(2026, 7, 31).date(),
    datetime(2026, 7, 30).date(),
    datetime(2026, 7, 29).date(),
    datetime(2026, 7, 28).date(),
]


class FakeQualityRepo(DataQualityRepository):
    def __init__(self, raw: dict | None = None) -> None:
        self.raw = raw or _clean_raw()

    async def price_audit(self, *, watchlist: list[str]) -> dict:
        return self.raw


def _clean_raw() -> dict:
    return {
        "intervals": [
            {"interval": "5m", "rows": 100, "symbols": 6, "first_ts": NOW, "last_ts": NOW}
        ],
        "duplicates": 0,
        "invalid_ohlc": 0,
        "non_positive": 0,
        "zero_volume": 0,
        "misaligned_intraday": 0,
        "weekend_d1": 0,
        "off_session_intraday": 0,
        "future_bars": 0,
        "freshness": [],
        "m5_per_day": [
            {"symbol": "AAPL", "day": d, "bars": 78}
            for d in SESSIONS
        ],
        "d1_per_day": [
            {"symbol": "AAPL", "day": d}
            for d in SESSIONS
        ],
        "d1_m5_diff": None,
    }


def _hours() -> MarketHours:
    return MarketHours(always_open=True)


def _auditor(raw: dict) -> DataQualityAuditor:
    return DataQualityAuditor(FakeQualityRepo(raw), _hours())


async def test_all_clean_passes() -> None:
    report = await _auditor(_clean_raw()).audit(["AAPL"], now=NOW)
    assert report.verdict == "PASS"
    assert report.score == 1.0
    assert {c.name for c in report.checks} == {
        "duplicates",
        "invalid_ohlc",
        "non_positive_price",
        "zero_volume",
        "misaligned_intraday",
        "weekend_daily",
        "off_session_intraday",
        "future_bars",
        "m5_coverage",
        "daily_gaps",
        "d1_m5_consistency",
        "freshness",
    }


@pytest.mark.parametrize(
    "field,check",
    [
        ("duplicates", "duplicates"),
        ("invalid_ohlc", "invalid_ohlc"),
        ("non_positive", "non_positive_price"),
        ("zero_volume", "zero_volume"),
        ("misaligned_intraday", "misaligned_intraday"),
        ("weekend_d1", "weekend_daily"),
        ("off_session_intraday", "off_session_intraday"),
        ("future_bars", "future_bars"),
    ],
)
async def test_structural_violations_fail(field: str, check: str) -> None:
    raw = _clean_raw()
    raw[field] = 1
    report = await _auditor(raw).audit(["AAPL"], now=NOW)
    assert report.verdict == "FAIL"
    by_name = {c.name: c for c in report.checks}
    assert by_name[check].status == "fail"


async def test_missing_m5_bars_counted() -> None:
    raw = _clean_raw()
    raw["m5_per_day"] = [
        {"symbol": "AAPL", "day": SESSIONS[0], "bars": 78},
        {"symbol": "AAPL", "day": SESSIONS[1], "bars": 70},
        {"symbol": "AAPL", "day": SESSIONS[3], "bars": 0},
    ]
    report = await _auditor(raw).audit(["AAPL"], now=NOW)
    by_name = {c.name: c for c in report.checks}
    assert by_name["m5_coverage"].status == "fail"
    assert by_name["m5_coverage"].detail["missing_bars"] == 8 + 78 + 78 + 78


async def test_few_missing_m5_bars_tolerated() -> None:
    raw = _clean_raw()
    raw["m5_per_day"] = [
        {"symbol": "AAPL", "day": SESSIONS[0], "bars": 78},
        {"symbol": "AAPL", "day": SESSIONS[1], "bars": 77},
        {"symbol": "AAPL", "day": SESSIONS[2], "bars": 78},
        {"symbol": "AAPL", "day": SESSIONS[3], "bars": 78},
        {"symbol": "AAPL", "day": SESSIONS[4], "bars": 78},
    ]
    report = await _auditor(raw).audit(["AAPL"], now=NOW)
    by_name = {c.name: c for c in report.checks}
    assert by_name["m5_coverage"].status == "ok"
    assert by_name["m5_coverage"].detail["missing_bars"] == 1


async def test_daily_gap_threshold() -> None:
    raw = _clean_raw()
    raw["d1_per_day"] = [
        {"symbol": "AAPL", "day": SESSIONS[0]},
        {"symbol": "AAPL", "day": SESSIONS[0] - timedelta(days=14)},
    ]
    report = await _auditor(raw).audit(["AAPL"], now=NOW)
    assert {c.name for c in report.checks if not c.passed} == {"daily_gaps"}


async def test_d1_m5_drift_fails() -> None:
    raw = _clean_raw()
    raw["d1_m5_diff"] = {
        "symbol": "AAPL",
        "day": SESSIONS[0],
        "d1_close": 100.0,
        "m5_close": 98.0,
        "diff_pct": 2.0,
    }
    report = await _auditor(raw).audit(["AAPL"], now=NOW)
    by_name = {c.name: c for c in report.checks}
    assert by_name["d1_m5_consistency"].status == "fail"


async def test_freshness_enforced_during_session() -> None:
    raw = _clean_raw()
    raw["freshness"] = [
        {"symbol": "AAPL", "interval": "5m", "last_ts": NOW, "age_seconds": 60},
        {"symbol": "MSFT", "interval": "5m", "last_ts": NOW, "age_seconds": 7200},
    ]
    report = await _auditor(raw).audit(["AAPL", "MSFT"], now=NOW)
    by_name = {c.name: c for c in report.checks}
    assert by_name["freshness"].status == "fail"
    assert by_name["freshness"].detail["stale"][0]["symbol"] == "MSFT"


async def test_freshness_skipped_when_market_closed() -> None:
    raw = _clean_raw()
    raw["freshness"] = [
        {"symbol": "AAPL", "interval": "5m", "last_ts": NOW, "age_seconds": 999999}
    ]
    hours = MarketHours(always_open=False)
    auditor = DataQualityAuditor(FakeQualityRepo(raw), hours)
    report = await auditor.audit(["AAPL"], now=NOW)
    by_name = {c.name: c for c in report.checks}
    assert by_name["freshness"].passed


async def test_no_scope_skips_watchlist_checks() -> None:
    report = await _auditor(_clean_raw()).audit(now=NOW)
    assert report.verdict == "PASS"
    by_name = {c.name: c for c in report.checks}
    assert by_name["m5_coverage"].detail["missing_bars"] == 0
