"""BarValidator — data validation layer that REJECTS suspicious bars.

Where ``BarCleaner`` normalizes and drops clearly-invalid rows, ``BarValidator``
enforces the structural and cross-bar integrity rules that keep corrupted data
out of the agents and the models:

- OHLC containment (a bar's open/close must lie within its high/low),
- weekend bars on daily data,
- implausible single-bar moves vs. the previous close (split artifacts when
  prices are unadjusted),
- missing-bar (gap) reporting for the affected symbol/interval.

Rules are deterministic and I/O-free. Rejected bars are counted and logged by
reason; the remaining bars are returned. Gap detection is advisory (a gap is
not the bar's fault) and reported separately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from qtrader.domain.value_objects import Interval, PriceBar

_ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Result of a validation pass."""

    kept: list[PriceBar]
    rejected: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    gaps: list[tuple[str, str, str, int]] = field(default_factory=list)

    @property
    def rejected_total(self) -> int:
        return self.rejected


class BarValidator:
    """Deterministic integrity checks over raw/cleaned bars.

    ``prev_close_by_symbol`` seeds the previous-close baseline (e.g. from the
    last stored bar) so a single live bar can be checked against history;
    otherwise the baseline is inferred from the batch itself (sorted by ts).
    """

    def __init__(
        self,
        max_single_bar_move_pct: float = 0.5,
        reject_large_moves: bool = True,
        max_calendar_gap_days: int = 10,
    ) -> None:
        self._max_move = Decimal(str(max_single_bar_move_pct))
        self._reject_large_moves = reject_large_moves
        self._max_gap = timedelta(days=max_calendar_gap_days)

    def validate(
        self,
        bars: list[PriceBar],
        *,
        prev_close_by_symbol: dict[str, Decimal] | None = None,
        reject_weekends: bool = True,
    ) -> ValidationReport:
        kept: list[PriceBar] = []
        rejected = 0
        reasons: dict[str, int] = {}
        prev: dict[str, Decimal] = dict(prev_close_by_symbol or {})

        def _reject(reason: str) -> None:
            nonlocal rejected
            rejected += 1
            reasons[reason] = reasons.get(reason, 0) + 1

        ordered = sorted(bars, key=lambda b: (b.ts, b.symbol))
        for bar in ordered:
            if bar.interval is Interval.D1 and reject_weekends and bar.ts.weekday() >= 5:
                _reject("weekend-bar")
                continue
            lo = min(bar.open, bar.close)
            hi = max(bar.open, bar.close)
            if bar.low > lo:
                _reject("ohlc-low-above-open-close")
                continue
            if bar.high < hi:
                _reject("ohlc-high-below-open-close")
                continue

            if prev.get(bar.symbol) is not None and prev[bar.symbol] > _ZERO:
                move = abs((bar.close - prev[bar.symbol]) / prev[bar.symbol])
                if move > self._max_move:
                    if self._reject_large_moves:
                        _reject("large-single-bar-move")
                        continue
                    reasons["large-single-bar-move-flagged"] = (
                        reasons.get("large-single-bar-move-flagged", 0) + 1
                    )
            prev[bar.symbol] = bar.close
            kept.append(bar)

        gaps: list[tuple[str, str, str, int]] = []
        per_symbol: dict[str, list[PriceBar]] = {}
        for bar in kept:
            per_symbol.setdefault(bar.symbol, []).append(bar)
        for symbol, sym_bars in per_symbol.items():
            sym_bars.sort(key=lambda b: b.ts)
            for i in range(1, len(sym_bars)):
                gap_days = (sym_bars[i].ts - sym_bars[i - 1].ts).days
                if gap_days > self._max_gap.days:
                    gaps.append(
                        (
                            symbol,
                            sym_bars[i - 1].ts.isoformat(),
                            sym_bars[i].ts.isoformat(),
                            gap_days,
                        )
                    )

        return ValidationReport(kept=kept, rejected=rejected, reasons=reasons, gaps=gaps)


@dataclass(frozen=True, slots=True)
class DataGap:
    """A detected missing-candle window for one symbol."""

    symbol: str
    interval: Interval
    expected_after: datetime
    expected_before: datetime
    calendar_days: int
