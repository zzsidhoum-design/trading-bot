"""Slippage model — per-fill execution friction from explicit assumptions.

Only OHLCV bars are available, so the model combines four *documented
assumptions* (never fabricated bid/ask data):

* ``base_spread_bps`` — assumed half-spread captured per fill.
* ``base_slippage_bps`` — fixed per-fill friction (queueing / latency).
* market impact — proportional to the order's participation in the symbol's
  average daily dollar volume (larger orders move price more).
* adverse drift — an ATR%-scaled term that widens with execution latency
  (random-walk expected adverse move over the delay horizon).

Each scenario (optimistic / baseline / conservative / stress) fixes these
assumptions; the model is pure and unit-testable.
"""

from __future__ import annotations

from decimal import Decimal

from qtrader.application.execution.models import SlippageAssumptions
from qtrader.domain.value_objects import TradeSide

_PRICE_QUANT = Decimal("0.000001")
_REFERENCE_LATENCY_SECONDS = 300.0


class SlippageModel:
    """Computes total slippage in basis points for one fill."""

    def __init__(self, assumptions: SlippageAssumptions) -> None:
        if assumptions.scenario is None:
            raise ValueError("slippage assumptions require a scenario")
        self._assumptions = assumptions

    @property
    def assumptions(self) -> SlippageAssumptions:
        return self._assumptions

    def slippage_bps(
        self,
        *,
        order_notional: Decimal,
        adv_dollar: Decimal | None,
        atr_pct: float | None,
    ) -> float:
        """Total expected slippage in bps for an order of ``order_notional``."""
        base = (
            self._assumptions.base_spread_bps + self._assumptions.base_slippage_bps
        )
        impact = self._impact_bps(order_notional, adv_dollar)
        volatility = self._adverse_drift_bps(atr_pct)
        total = base + impact + volatility
        return min(total, self._assumptions.max_slippage_bps)

    def fill_price(
        self,
        *,
        side: TradeSide,
        reference_price: Decimal,
        order_notional: Decimal,
        adv_dollar: Decimal | None,
        atr_pct: float | None,
    ) -> tuple[Decimal, float]:
        """Adjusted fill price (buy high / sell low) plus the bps applied."""
        bps = self.slippage_bps(
            order_notional=order_notional,
            adv_dollar=adv_dollar,
            atr_pct=atr_pct,
        )
        factor = Decimal(1) + Decimal(str(bps)) / Decimal(10000)
        if side is TradeSide.SELL:
            factor = Decimal(2) - factor  # sell low: 1 - bps
        price = (reference_price * factor).quantize(_PRICE_QUANT)
        return price, bps

    def _impact_bps(
        self, order_notional: Decimal, adv_dollar: Decimal | None
    ) -> float:
        if adv_dollar is None or adv_dollar <= 0 or order_notional <= 0:
            return 0.0
        participation = float(order_notional / adv_dollar)
        return self._assumptions.impact_coefficient * participation * 10000.0

    def _adverse_drift_bps(self, atr_pct: float | None) -> float:
        """ATR%-scaled adverse drift that grows with execution latency."""
        if atr_pct is None or atr_pct < 0:
            return 0.0
        latency_ratio: float = min(
            max(self._assumptions.latency_seconds, 0.0) / _REFERENCE_LATENCY_SECONDS,
            1.0,
        )
        drift = (
            self._assumptions.volatility_multiplier
            * atr_pct
            * 100.0
            * (latency_ratio**0.5)
        )
        return float(drift)


__all__ = ["SlippageModel"]
