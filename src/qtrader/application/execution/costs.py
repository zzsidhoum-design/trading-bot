"""Transaction cost model — commission on notional with optional minimum.

Mirrors the research backtest's commission convention (basis points on
notional, rounded to cents) so execution-aware results remain comparable.
"""

from __future__ import annotations

from decimal import Decimal

_BPS = Decimal("0.0001")


class TransactionCostModel:
    def __init__(self, commission_bps: float = 10.0, min_commission: Decimal | None = None) -> None:
        if commission_bps < 0.0:
            raise ValueError("commission_bps must be >= 0")
        self._rate = Decimal(str(commission_bps)) * _BPS
        self._min_commission = min_commission

    def commission_for(self, quantity: int, price: Decimal) -> Decimal:
        """Commission on a (quantity, price) fill, rounded to cents."""
        amount = (price * Decimal(quantity) * self._rate).quantize(Decimal("0.01"))
        if self._min_commission is not None:
            amount = max(amount, self._min_commission)
        return amount


__all__ = ["TransactionCostModel"]
