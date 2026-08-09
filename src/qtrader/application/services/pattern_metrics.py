"""Forward statistics for pattern events.

For every :class:`~qtrader.application.services.pattern_events.PatternEvent`
we measure the window that starts at the next bar's open (matching the
engine's next-open fills): forward return, max favorable excursion (MFE),
max adverse excursion (MAE), and the same return net of a round-trip cost
assumption. Aggregated per pattern this tells us whether the event has any
statistical edge.

A conservative stance: the harness never trades on these; it only measures,
so the audit can decide which (if any) events carry a stable edge.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from qtrader.application.services.pattern_events import PatternEvent
from qtrader.domain.value_objects import PriceBar


@dataclass(frozen=True, slots=True)
class PatternStat:
    """Aggregated forward statistics for one pattern."""

    pattern: str
    occurrences: int
    success_rate: Decimal | None
    avg_return: Decimal | None
    avg_return_net: Decimal | None
    avg_mfe: Decimal | None
    avg_mae: Decimal | None
    edge_ratio: Decimal | None


@dataclass(slots=True)
class _Forward:
    ret: Decimal
    ret_net: Decimal
    mfe: Decimal
    mae: Decimal


def _entry_and_window(
    bars: Sequence[PriceBar], ts: datetime, horizon_bars: int
) -> tuple[Decimal, list[PriceBar]] | None:
    """Entry = open of first bar strictly after ``ts``; window = up to ``horizon`` bars."""
    after = [b for b in bars if b.ts > ts]
    if not after:
        return None
    return after[0].open, after[:horizon_bars]


def analyze_forward(
    bars_by_symbol: Mapping[str, Sequence[PriceBar]],
    events: Sequence[PatternEvent],
    *,
    horizon_bars: int = 12,
    round_trip_bps: Decimal = Decimal("60"),
) -> list[PatternStat]:
    """Aggregate forward stats per pattern over all (symbol, event) pairs.

    Events with no bar strictly after ``ts`` are skipped (nothing to measure).
    The forward window is capped at ``horizon_bars`` bars after the entry bar.
    """
    per_pattern: dict[str, list[_Forward]] = defaultdict(list)
    by_symbol: dict[str, Sequence[PriceBar]] = dict(bars_by_symbol)

    for ev in events:
        bars = by_symbol.get(ev.symbol)
        if not bars:
            continue
        found = _entry_and_window(bars, ev.ts, horizon_bars)
        if found is None:
            continue
        entry, window = found
        if not window:
            continue
        last_close = window[-1].close
        ret = last_close / entry - 1
        mfe = max(float(b.high) for b in window) / float(entry) - 1
        mae = min(float(b.low) for b in window) / float(entry) - 1
        ret_net = ret - round_trip_bps / 10000
        per_pattern[ev.pattern].append(
            _Forward(ret=ret, ret_net=ret_net, mfe=Decimal(str(mfe)), mae=Decimal(str(mae)))
        )

    stats: list[PatternStat] = []
    for pattern, rows in sorted(per_pattern.items()):
        occurrences = len(rows)
        wins = [r for r in rows if r.ret > 0]
        success_rate = Decimal(len(wins)) / Decimal(occurrences)
        avg_return = sum((r.ret for r in rows), Decimal(0)) / Decimal(occurrences)
        avg_return_net = sum((r.ret_net for r in rows), Decimal(0)) / Decimal(occurrences)
        avg_mfe = sum((r.mfe for r in rows), Decimal(0)) / Decimal(occurrences)
        avg_mae = sum((r.mae for r in rows), Decimal(0)) / Decimal(occurrences)
        edge_ratio = (avg_mfe / abs(avg_mae)) if avg_mae != 0 else None
        stats.append(
            PatternStat(
                pattern=pattern,
                occurrences=occurrences,
                success_rate=success_rate,
                avg_return=avg_return,
                avg_return_net=avg_return_net,
                avg_mfe=avg_mfe,
                avg_mae=avg_mae,
                edge_ratio=edge_ratio,
            )
        )
    return stats


def edge_signals(stats: Sequence[PatternStat], *, min_occurrences: int = 20) -> list[PatternStat]:
    """Patterns that look non-random: net edge positive with enough samples."""
    return [
        s
        for s in stats
        if s.occurrences >= min_occurrences
        and s.avg_return_net is not None
        and s.avg_return_net > 0
    ]
