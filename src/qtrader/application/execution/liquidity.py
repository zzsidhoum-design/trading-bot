"""Liquidity model — ADV estimates and order-size/liquidity constraints.

Estimates average daily volume and average daily dollar volume straight from
the OHLCV bars (the only volume data available — nothing is fabricated), then
enforces the participation budget: a single order may not exceed a fraction of
the bar's volume and may not exceed a fraction of the symbol's ADV dollars.
Orders that break the dollar-volume budget are "unrealistic trade sizes" and
are rejected and flagged so researchers can see them.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from qtrader.application.execution.models import LiquidityAssessment, LiquidityAssumptions
from qtrader.domain.value_objects import PriceBar


class LiquidityModel:
    """Pure ADV estimation + order liquidity assessment."""

    def __init__(self, assumptions: LiquidityAssumptions | None = None) -> None:
        self._assumptions = assumptions or LiquidityAssumptions()

    @property
    def assumptions(self) -> LiquidityAssumptions:
        return self._assumptions

    def adv_for(self, bars: Sequence[PriceBar]) -> tuple[Decimal, Decimal]:
        """Average daily (bar) volume and dollar volume over the lookback."""
        window = bars[-self._assumptions.adv_window_bars :]
        if not window:
            return Decimal("0"), Decimal("0")
        total_volume = Decimal("0")
        total_dollar = Decimal("0")
        for bar in window:
            total_volume += bar.volume
            total_dollar += bar.volume * bar.close
        count = Decimal(len(window))
        return total_volume / count, total_dollar / count

    def check_size(
        self,
        *,
        order_notional: Decimal,
        adv_volume: Decimal,
        adv_dollar: Decimal,
    ) -> LiquidityAssessment:
        """Submit-time liquidity gate (floors + notional-vs-ADV budget)."""
        reasons: list[str] = []
        if adv_volume > 0 and adv_volume < self._assumptions.min_avg_volume:
            reasons.append(
                f"avg volume {adv_volume:.0f} below floor "
                f"{self._assumptions.min_avg_volume}"
            )
        if adv_dollar > 0 and adv_dollar < self._assumptions.min_avg_dollar_volume:
            reasons.append(
                f"avg dollar volume {adv_dollar:.0f} below floor "
                f"{self._assumptions.min_avg_dollar_volume}"
            )
        max_notional = (
            adv_dollar * Decimal(str(self._assumptions.max_notional_pct_adv))
            if adv_dollar > 0
            else Decimal("0")
        )
        if max_notional > 0 and order_notional > max_notional:
            reasons.append(
                f"order notional {order_notional:.2f} exceeds "
                f"{self._assumptions.max_notional_pct_adv:.1%} of ADV dollars "
                f"({max_notional:.2f}) — unrealistic trade size"
            )
        return LiquidityAssessment(approved=not reasons, reasons=tuple(reasons), max_fillable=0)

    def max_fillable(self, bar: PriceBar) -> int:
        """Max shares the bar can absorb (participation-rate cap)."""
        return int(bar.volume * Decimal(str(self._assumptions.max_participation_rate)))


__all__ = ["LiquidityModel"]
