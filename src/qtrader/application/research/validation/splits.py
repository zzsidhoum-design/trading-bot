"""Development / validation / out-of-sample data splits.

The Phase 3 discipline is that generated hypotheses are developed and filtered
on the **development** window, confirmed on the **validation** window, and only
touched on the **out-of-sample** window once the strategy has been finalized.
This module computes the three contiguous, non-overlapping calendar windows and
slices bar histories down to them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from qtrader.domain.value_objects import PriceBar


@dataclass(frozen=True, slots=True)
class DataWindow:
    """A half-open-on-calendar interval ``[start, end]`` (both dates inclusive)."""

    start: date
    end: date

    @property
    def label(self) -> str:
        return f"{self.start.isoformat()}/{self.end.isoformat()}"

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"window end {self.end} precedes start {self.start}")


def split_windows(
    start: date,
    end: date,
    dev_fraction: float,
    validation_fraction: float,
) -> tuple[DataWindow, DataWindow, DataWindow]:
    """Split ``[start, end]`` into dev / validation / out-of-sample windows.

    Windows are contiguous and disjoint in calendar days: ``dev`` covers the
    first ``dev_fraction`` of the span, ``validation`` the next
    ``validation_fraction``, and everything that remains is the untouched
    out-of-sample window.
    """
    if start >= end:
        raise ValueError("split requires end after start")
    if dev_fraction <= 0.0 or validation_fraction <= 0.0:
        raise ValueError("fractions must be positive")
    if dev_fraction + validation_fraction >= 1.0:
        raise ValueError("dev + validation fractions must leave an OOS window")

    total_days = (end - start).days
    dev_days = round(total_days * dev_fraction)
    validation_days = round(total_days * validation_fraction)
    dev_end = start + timedelta(days=dev_days)
    validation_end = dev_end + timedelta(days=validation_days)
    return (
        DataWindow(start, min(dev_end - timedelta(days=1), end)),
        DataWindow(dev_end, min(validation_end - timedelta(days=1), end)),
        DataWindow(validation_end, end),
    )


def slice_bars(bars: list[PriceBar], window: DataWindow) -> list[PriceBar]:
    """Return only the bars whose calendar date falls inside ``window``."""
    return [b for b in bars if window.start <= b.ts.date() <= window.end]


def slice_bars_by_symbol(
    bars_by_symbol: dict[str, list[PriceBar]], window: DataWindow
) -> dict[str, list[PriceBar]]:
    """Slice every symbol's bar history down to one :class:`DataWindow`."""
    return {
        symbol: slice_bars(bars, window)
        for symbol, bars in bars_by_symbol.items()
        if bars
    }


__all__ = ["DataWindow", "slice_bars", "slice_bars_by_symbol", "split_windows"]
