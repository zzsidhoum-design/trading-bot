"""BarCleaning — pure, deterministic price-bar cleaning.

No I/O, no network. The Data Agent feeds raw provider bars here before they
are persisted or published. Invalid bars are dropped (never raised) and
counted in the report.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from statistics import median

from qtrader.domain.value_objects import Interval, PriceBar

PRICE_ZERO = Decimal("0")
VOLUME_ZERO = Decimal("0")

# Intraday intervals snap to a fixed clock grid in the exchange's local time.
# Completed bars from Yahoo always carry a :00 second and a minute aligned to
# the interval step; anything else is an in-progress bar or a bad timestamp.
_INTRADAY_GRID_MINUTES: dict[Interval, int] = {
    Interval.M1: 1,
    Interval.M5: 5,
    Interval.M15: 15,
    Interval.M30: 30,
    Interval.H1: 60,
    Interval.H4: 240,
}


@dataclass(frozen=True, slots=True)
class CleaningReport:
    kept: list[PriceBar]
    dropped: int
    reasons: dict[str, int]

    @property
    def dropped_total(self) -> int:
        return self.dropped


class BarCleaner:
    """Normalize, dedup and sanity-check bars.

    All rules are applied independently so a single bad field drops only that
    bar; the rest of the batch is preserved.
    """

    def __init__(
        self,
        max_lateness_seconds: int = 600,
        max_future_seconds: int = 60,
        max_volume_spike_factor: Decimal = Decimal("100"),
        reject_zero_volume: bool = True,
        align_intraday: bool = True,
    ) -> None:
        self._max_lateness = timedelta(seconds=max_lateness_seconds)
        self._max_future = timedelta(seconds=max_future_seconds)
        self._max_volume_spike = max_volume_spike_factor
        self._reject_zero_volume = reject_zero_volume
        self._align_intraday = align_intraday

    def clean(
        self,
        bars: list[PriceBar],
        *,
        now: datetime | None = None,
        reject_stale: bool = True,
    ) -> CleaningReport:
        """Return a report of cleaned bars plus the drop count.

        ``reject_stale=True`` enforces lateness/future windows (live path);
        backfills pass ``reject_stale=False`` to keep historical bars intact.
        """
        now = now or datetime.now(UTC)
        seen: dict[tuple[str, Interval, datetime], PriceBar] = {}
        reasons: dict[str, int] = {}

        def _drop(reason: str) -> None:
            reasons[reason] = reasons.get(reason, 0) + 1

        # 1) normalize UTC + dedup by (symbol, interval, ts) keeping the last.
        for bar in bars:
            ts = bar.ts
            ts = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
            key = (bar.symbol, bar.interval, ts)
            if key in seen:
                _drop("duplicate")
            seen[key] = PriceBar(
                symbol=bar.symbol,
                interval=bar.interval,
                ts=ts,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )

        # 2) range checks. PriceBar construction already rejects negative /
        #    malformed OHLC; here we catch what slips past it: zero prices,
        #    zero / NaN / negative volume, and intraday bars whose timestamp is
        #    off the interval grid (in-progress or junk bars).
        valid: list[PriceBar] = []
        for bar in seen.values():
            if (
                bar.open <= PRICE_ZERO
                or bar.high <= PRICE_ZERO
                or bar.low <= PRICE_ZERO
                or bar.close <= PRICE_ZERO
            ):
                _drop("non-positive-price")
                continue
            if bar.volume != bar.volume or bar.volume < VOLUME_ZERO:
                _drop("invalid-volume")
                continue
            if self._reject_zero_volume and bar.volume == VOLUME_ZERO:
                _drop("zero-volume")
                continue
            if self._align_intraday and not self._on_grid(bar.ts, bar.interval):
                _drop("misaligned-timestamp")
                continue
            valid.append(bar)
        del seen

        # 3) volume spike sanity — a single bar wildly above the batch median
        #    is most likely a bad tick. Needs enough bars for the median to be
        #    robust against the outlier itself.
        if len(valid) >= 3:
            baseline = median(b.volume for b in valid)
            if baseline > VOLUME_ZERO:
                limit = self._max_volume_spike * baseline
                normal = [b for b in valid if b.volume <= limit]
                spike_drops = len(valid) - len(normal)
                if spike_drops:
                    reasons["volume-spike"] = reasons.get("volume-spike", 0) + spike_drops
                valid = normal

        # 4) staleness / future rejection (live path only).
        if reject_stale:
            kept: list[PriceBar] = []
            for bar in valid:
                if bar.ts > now + self._max_future:
                    _drop("future-bar")
                    continue
                if bar.ts < now - self._max_lateness:
                    _drop("stale-bar")
                    continue
                kept.append(bar)
            valid = kept

        valid.sort(key=lambda b: b.ts)
        return CleaningReport(kept=valid, dropped=sum(reasons.values()), reasons=reasons)

    @staticmethod
    def _on_grid(ts: datetime, interval: Interval) -> bool:
        """Whether an intraday timestamp falls on the interval's clock grid.

        Grid alignment is timezone-invariant for US-equity offsets (whole
        hours from UTC), so the UTC timestamp is a faithful proxy for the
        exchange-local wall clock. Daily bars are exempt.
        """
        step = _INTRADAY_GRID_MINUTES.get(interval)
        if step is None:
            return True
        if ts.second != 0:
            return False
        if step >= 60:
            return ts.minute == 0
        return ts.minute % step == 0
