"""US equity trading-session calendar.

Decides whether a given instant falls inside a regular trading session for the
configured exchange: weekdays, between the open and close times in the
exchange's local time zone, excluding holidays. All comparisons happen in UTC;
DST is handled by ``zoneinfo``, so a 09:30 open resolves to 14:30 UTC in winter
and 13:30 UTC in summer without any manual adjustment.

The holiday calendar is the union of a curated set of NYSE/NASDAQ observed
holidays (2025-2027) and any dates supplied via ``holidays``. Set
``always_open`` to disable the calendar entirely (useful for backtests and
CI where time-of-day is irrelevant).

Example::

    hours = MarketHours()
    if hours.is_open():
        ...  # normal trading session in progress
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

# NYSE/NASDAQ observed market holidays, 2025-2027. Dates when the exchange is
# closed even though it is a weekday.
_DEFAULT_HOLIDAYS: frozenset[str] = frozenset(
    {
        "2025-01-01",
        "2025-01-09",  # national day of mourning (Carter)
        "2025-02-17",
        "2025-04-18",  # good friday
        "2025-05-26",
        "2025-06-19",
        "2025-07-04",
        "2025-09-01",
        "2025-11-27",
        "2025-12-25",
        "2026-01-01",
        "2026-01-19",
        "2026-02-16",
        "2026-04-03",  # good friday
        "2026-05-25",
        "2026-06-19",
        "2026-07-03",  # observed (jul 4 is a saturday)
        "2026-09-07",
        "2026-11-26",
        "2026-12-25",
        "2027-01-01",
        "2027-01-18",
        "2027-02-15",
        "2027-03-26",  # good friday
        "2027-05-31",
        "2027-06-18",  # observed (juneteenth is a saturday)
        "2027-07-05",  # observed (jul 4 is a sunday)
        "2027-09-06",
        "2027-11-25",
        "2027-12-24",  # observed (christmas is a saturday)
    }
)


def _to_iso(day: date | str) -> str:
    return day.isoformat() if isinstance(day, date) else str(day)


def _parse_time(value: str) -> time:
    try:
        hour, minute = value.split(":", 1)
        return time(int(hour), int(minute))
    except ValueError as exc:
        raise ValueError(f"invalid market time {value!r} (expected HH:MM)") from exc


class MarketHours:
    """Regular trading-session calendar for a single exchange."""

    def __init__(
        self,
        *,
        timezone: str = "America/New_York",
        open_time: str = "09:30",
        close_time: str = "16:00",
        holidays: Sequence[date | str] = (),
        always_open: bool = False,
    ) -> None:
        self._tz = ZoneInfo(timezone)
        self._open = _parse_time(open_time)
        self._close = _parse_time(close_time)
        if self._open >= self._close:
            raise ValueError(f"market close {close_time!r} must follow open {open_time!r}")
        self._holidays = _DEFAULT_HOLIDAYS | frozenset(_to_iso(d) for d in holidays)
        self._always_open = always_open

    @property
    def timezone_name(self) -> str:
        return str(self._tz)

    @property
    def open_time(self) -> time:
        return self._open

    @property
    def close_time(self) -> time:
        return self._close

    @property
    def holidays(self) -> frozenset[str]:
        return self._holidays

    @property
    def always_open(self) -> bool:
        return self._always_open

    def is_trading_day(self, day: date) -> bool:
        """True for a weekday that is not an observed holiday."""
        if self._always_open:
            return True
        return day.weekday() < 5 and day.isoformat() not in self._holidays

    def is_open(self, at: datetime | None = None) -> bool:
        """Whether ``at`` (UTC by default) falls inside a regular session."""
        now = at if at is not None else datetime.now(UTC)
        if self._always_open:
            return True
        local = now.astimezone(self._tz)
        if not self.is_trading_day(local.date()):
            return False
        return self._open <= local.time() < self._close

    def session_bounds(self, at: datetime | None = None) -> tuple[datetime, datetime] | None:
        """The day's open/close instants in UTC, or None on a non-trading day."""
        now = at if at is not None else datetime.now(UTC)
        if self._always_open:
            start = now
            return (start, start)
        local = now.astimezone(self._tz)
        if not self.is_trading_day(local.date()):
            return None
        open_at = local.replace(
            hour=self._open.hour, minute=self._open.minute, second=0, microsecond=0
        )
        close_at = local.replace(
            hour=self._close.hour, minute=self._close.minute, second=0, microsecond=0
        )
        return (open_at.astimezone(UTC), close_at.astimezone(UTC))

    def next_open(self, at: datetime | None = None) -> datetime:
        """The next instant the market will be open (UTC), strictly after ``at``."""
        now = at if at is not None else datetime.now(UTC)
        if self._always_open:
            return now
        local = now.astimezone(self._tz)
        candidate = local.date()
        if local.time() < self._open and self.is_trading_day(candidate):
            return self._session_open(candidate)
        for _ in range(370):
            candidate += timedelta(days=1)
            if self.is_trading_day(candidate):
                return self._session_open(candidate)
        raise RuntimeError("no future trading day found within one year")

    def _session_open(self, day: date) -> datetime:
        local = datetime.combine(
            day, self._open, tzinfo=self._tz
        ).astimezone(UTC)
        return local
