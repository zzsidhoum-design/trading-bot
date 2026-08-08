"""Point-in-time universe selection.

A plain, dependency-free helper so historical backtests only trade symbols that
were actually listed at the time. ``listing_dates`` is typically derived from
each symbol's first stored bar; ``as_of`` is the (UTC) date of the test window.

Without this filter a backtest can trade a symbol in 2022 that only listed in
2024 (look-ahead from a current-membership universe) — see docs/audit/09-phase3-universe.md.
"""

from __future__ import annotations

from datetime import date


def point_in_time_universe(
    listing_dates: dict[str, date], as_of: date
) -> list[str]:
    """Symbols whose listing date is on or before ``as_of``.

    Returns the sorted list of tradeable symbols at ``as_of``.
    """
    return sorted(sym for sym, first in listing_dates.items() if first <= as_of)


def listing_date_from_first_bar(first_bar_ts) -> date:
    """Extract a listing date from a first-bar timestamp."""
    return first_bar_ts.date()
