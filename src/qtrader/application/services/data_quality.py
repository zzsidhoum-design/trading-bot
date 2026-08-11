"""Data Quality Auditor — periodic verification of the persisted price universe.

The auditor runs read-only integrity checks against the price tables and turns
them into a single pass/fail report. It is policy-free: every threshold is an
explicit parameter so operators can tune it without touching SQL.

Checks are grouped into three layers:

* structural — impossible rows (duplicates, broken OHLC, non-positive prices,
  zero volume, off-grid timestamps, weekend/off-session bars, future bars);
* coverage — recent intraday sessions complete, daily series without gaps;
* consistency & freshness — daily/intraday closes agree, live data is current.

The verdict is intentionally strict: any failing check fails the report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from qtrader.application.services.market_hours import MarketHours
from qtrader.domain.ports import DataQualityRepository

DEFAULT_MAX_FRESHNESS_SECONDS = 3600
DEFAULT_MAX_D1_GAP_DAYS = 7
DEFAULT_MAX_D1_M5_DIFF_PCT = 1.0
DEFAULT_MAX_MISSING_M5_BARS = 5  # tolerated provider-side holes per audit
M5_BARS_PER_SESSION = 78  # 09:30..15:55 ET on the 5-minute grid


@dataclass(frozen=True, slots=True)
class QualityCheck:
    name: str
    status: str
    detail: dict[str, Any]

    @property
    def passed(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True, slots=True)
class DataQualityReport:
    generated_at: datetime
    scope: list[str]
    checks: list[QualityCheck] = field(default_factory=list)

    @property
    def score(self) -> float:
        if not self.checks:
            return 0.0
        return sum(1 for c in self.checks if c.passed) / len(self.checks)

    @property
    def verdict(self) -> str:
        return "PASS" if all(c.passed for c in self.checks) else "FAIL"


class DataQualityAuditor:
    """Assemble integrity, coverage and freshness checks into a report."""

    def __init__(
        self,
        repo: DataQualityRepository,
        market_hours: MarketHours,
        *,
        max_freshness_seconds: int = DEFAULT_MAX_FRESHNESS_SECONDS,
        max_d1_gap_days: int = DEFAULT_MAX_D1_GAP_DAYS,
        max_d1_m5_diff_pct: float = DEFAULT_MAX_D1_M5_DIFF_PCT,
        max_missing_m5_bars: int = DEFAULT_MAX_MISSING_M5_BARS,
    ) -> None:
        self._repo = repo
        self._hours = market_hours
        self._max_freshness = timedelta(seconds=max_freshness_seconds)
        self._max_d1_gap = timedelta(days=max_d1_gap_days)
        self._max_d1_m5_diff = max_d1_m5_diff_pct
        self._max_missing_m5 = max_missing_m5_bars

    async def audit(
        self, symbols: list[str] | None = None, now: datetime | None = None
    ) -> DataQualityReport:
        scope = [s.upper() for s in symbols or []]
        raw = await self._repo.price_audit(watchlist=scope)
        checks: list[QualityCheck] = []
        now = now or datetime.now(UTC)

        def check(name: str, ok: bool, detail: dict[str, Any]) -> None:
            checks.append(
                QualityCheck(name=name, status="ok" if ok else "fail", detail=detail)
            )

        check("duplicates", raw["duplicates"] == 0, {"rows": raw["duplicates"]})
        check("invalid_ohlc", raw["invalid_ohlc"] == 0, {"rows": raw["invalid_ohlc"]})
        check(
            "non_positive_price",
            raw["non_positive"] == 0,
            {"rows": raw["non_positive"]},
        )
        check("zero_volume", raw["zero_volume"] == 0, {"rows": raw["zero_volume"]})
        check(
            "misaligned_intraday",
            raw["misaligned_intraday"] == 0,
            {"rows": raw["misaligned_intraday"]},
        )
        check("weekend_daily", raw["weekend_d1"] == 0, {"rows": raw["weekend_d1"]})
        check(
            "off_session_intraday",
            raw["off_session_intraday"] == 0,
            {"rows": raw["off_session_intraday"]},
        )
        check("future_bars", raw["future_bars"] == 0, {"rows": raw["future_bars"]})

        missing = self._m5_missing(raw["m5_per_day"], scope, now)
        check(
            "m5_coverage",
            missing <= self._max_missing_m5,
            {"missing_bars": missing, "tolerance": self._max_missing_m5},
        )

        gaps = self._d1_gaps(raw["d1_per_day"], scope)
        ok = all(g <= self._max_d1_gap for g in gaps.values())
        check("daily_gaps", ok, {k: g.days for k, g in gaps.items()})

        diff = raw.get("d1_m5_diff")
        check(
            "d1_m5_consistency",
            diff is None
            or (diff["diff_pct"] is not None and diff["diff_pct"] <= self._max_d1_m5_diff),
            diff or {},
        )

        ok, detail = self._freshness(raw["freshness"], now)
        check("freshness", ok, detail)

        return DataQualityReport(generated_at=now, scope=scope, checks=checks)

    # ------------------------------------------------------------------ #
    # check helpers
    # ------------------------------------------------------------------ #

    def _m5_missing(
        self, per_day: list[dict[str, Any]], scope: list[str], now: datetime
    ) -> int:
        """Sum of missing 5-minute bars over the last 5 completed sessions."""
        if not scope:
            return 0
        sessions = self._last_sessions(now, 5)
        day_set = {d.isoformat() for d in sessions}
        counts: dict[tuple[str, str], int] = {}
        for row in per_day:
            if row["day"].isoformat() in day_set:
                counts[(row["symbol"], row["day"].isoformat())] = row["bars"]
        missing = 0
        for symbol in scope:
            for day in sessions:
                missing += max(0, M5_BARS_PER_SESSION - counts.get((symbol, day.isoformat()), 0))
        return missing

    def _d1_gaps(
        self, per_day: list[dict[str, Any]], scope: list[str]
    ) -> dict[str, timedelta]:
        days_by_symbol: dict[str, list[date]] = {}
        for row in per_day:
            days_by_symbol.setdefault(row["symbol"], []).append(row["day"])
        gaps: dict[str, timedelta] = {}
        for symbol in scope:
            days = sorted(days_by_symbol.get(symbol, []))
            if len(days) < 2:
                gaps[symbol] = timedelta(days=0)
                continue
            gaps[symbol] = max(
                (b - a for a, b in zip(days, days[1:], strict=False)),
                default=timedelta(days=0),
            )
        return gaps

    def _freshness(
        self, rows: list[dict[str, Any]], now: datetime
    ) -> tuple[bool, dict[str, Any]]:
        if not self._hours.is_open(now):
            return True, {"status": "market closed; not enforced"}
        stale = [
            {
                "symbol": r["symbol"],
                "age_seconds": int(r["age_seconds"]),
                "last_ts": r["last_ts"].isoformat(),
            }
            for r in rows
            if r["interval"] == "5m" and r["age_seconds"] > self._max_freshness.total_seconds()
        ]
        return (not stale), {"stale": stale}

    # ------------------------------------------------------------------ #
    # session helpers
    # ------------------------------------------------------------------ #

    def _last_sessions(self, now: datetime, count: int) -> list[date]:
        """The ``count`` most recently *completed* trading days (ET dates)."""
        tz = ZoneInfo(self._hours.timezone_name)
        last = self._last_completed_trading_day(now, tz)
        days: list[date] = []
        probe = last
        while len(days) < count:
            if self._hours.is_trading_day(probe):
                days.append(probe)
            probe -= timedelta(days=1)
        return list(reversed(days))

    def _last_completed_trading_day(self, now: datetime, tz: ZoneInfo) -> date:
        local = now.astimezone(tz)
        day = local.date()
        if not self._hours.is_trading_day(day):
            day -= timedelta(days=1)
            while not self._hours.is_trading_day(day):
                day -= timedelta(days=1)
            return day
        close_at = datetime.combine(day, self._hours.close_time, tzinfo=tz)
        if now < close_at.astimezone(UTC):
            day -= timedelta(days=1)
            while not self._hours.is_trading_day(day):
                day -= timedelta(days=1)
        return day
