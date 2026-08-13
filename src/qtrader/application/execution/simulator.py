"""Execution simulator — the dedicated engine between Strategy and live execution.

Processes orders bar-by-bar exactly like the research backtest (an order queued
on a signal bar becomes actionable on the next bar) but models realistic
execution: market/limit/stop order semantics, spread+slippage+impact friction,
latency, participation-capped fills (a fill can be partial), rejection of
unrealistic order sizes, gap-through fills for stop orders, and an optional
trading-hours gate. The simulator is pure and deterministic (seedable) — it
performs no I/O and never fabricates bid/ask or order-book data; every
microstructure-shaped number comes from the explicit assumptions in
:class:`~qtrader.application.execution.models.SlippageAssumptions` /
:class:`~qtrader.application.execution.models.LiquidityAssumptions`.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal

from qtrader.application.execution.costs import TransactionCostModel
from qtrader.application.execution.liquidity import LiquidityModel
from qtrader.application.execution.models import (
    ExecutionFill,
    ExecutionOrder,
    ExecutionStats,
    LiquidityAssessment,
)
from qtrader.application.execution.slippage import SlippageModel
from qtrader.domain.value_objects import OrderType, PriceBar, TradeSide

_PRICE_QUANT = Decimal("0.000001")


@dataclass(slots=True)
class _WorkingOrder:
    """A submitted order plus its execution state (multi-bar fills)."""

    order: ExecutionOrder
    filled_qty: int = 0
    partial: bool = False


@dataclass(frozen=True, slots=True)
class _ExecutionStep:
    """One bar's outcome for one order."""

    fills: tuple[ExecutionFill, ...]
    keep_alive: bool


class ExecutionSimulator:
    """Deterministic bar-driven order execution engine."""

    def __init__(
        self,
        slippage: SlippageModel,
        liquidity: LiquidityModel,
        costs: TransactionCostModel,
        *,
        seed: int | None = None,
    ) -> None:
        self._slippage = slippage
        self._liquidity = liquidity
        self._costs = costs
        self._rng = random.Random(seed)
        self._pending: list[_WorkingOrder] = []
        self._stats = ExecutionStats()
        self._assessments: dict[str, LiquidityAssessment] = {}
        self._adv_seen: dict[str, tuple[Decimal, Decimal]] = {}
        self._next_order_id = 1

    @property
    def stats(self) -> ExecutionStats:
        return self._stats

    @property
    def assessments(self) -> dict[str, LiquidityAssessment]:
        """Latest liquidity assessment per symbol that submitted an order."""
        return dict(self._assessments)

    @property
    def adv_seen(self) -> dict[str, tuple[Decimal, Decimal]]:
        """(avg volume, avg dollar volume) per symbol that submitted an order."""
        return dict(self._adv_seen)

    @property
    def pending(self) -> list[ExecutionOrder]:
        return [working.order for working in self._pending]

    def submit(
        self,
        order: ExecutionOrder,
        *,
        ref_price: Decimal,
        adv_volume: Decimal,
        adv_dollar: Decimal,
    ) -> bool:
        """Submit an order; returns False when it is rejected outright.

        Rejection happens only for unrealistic sizes / illiquid symbols — a
        submitted-but-unfilled order stays working and may fill on a later bar.
        A new order for the same symbol+side replaces any still-working one
        (last signal wins), so stale exits never double-sell.
        """
        self._stats.submitted += 1
        if order.quantity <= 0:
            self._stats.rejected += 1
            return False
        notional = ref_price * Decimal(order.quantity)
        assessment = self._liquidity.check_size(
            order_notional=notional,
            adv_volume=adv_volume,
            adv_dollar=adv_dollar,
        )
        self._assessments[order.symbol] = assessment
        self._adv_seen[order.symbol] = (adv_volume, adv_dollar)
        if not assessment.approved:
            self._stats.rejected += 1
            if "unrealistic trade size" in " ".join(assessment.reasons):
                self._stats.unrealistic_orders += 1
            return False
        self._replace_pending(order)
        return True

    def process_bar(
        self,
        bar: PriceBar,
        *,
        adv_volume: Decimal,
        adv_dollar: Decimal,
        atr_pct: float | None = None,
        tradable: bool = True,
    ) -> list[ExecutionFill]:
        """Advance the simulator one bar; returns fills for this symbol.

        Fills are priced at the bar's open (next-bar execution convention),
        adjusted for the order type and the slippage model. A fill may be
        partial (participation-capped) and remain working for later bars.
        """
        fills: list[ExecutionFill] = []
        if not tradable:
            return fills
        remaining: list[_WorkingOrder] = []
        for working in self._pending:
            if working.order.symbol != bar.symbol:
                remaining.append(working)
                continue
            step = self._step(working, bar, adv_dollar, atr_pct)
            fills.extend(step.fills)
            if step.keep_alive:
                remaining.append(working)
        self._pending = remaining
        return fills

    def cancel_side(self, symbol: str, side: TradeSide) -> None:
        """Cancel working orders for a symbol+side (e.g. stop fired first)."""
        remaining: list[_WorkingOrder] = []
        for working in self._pending:
            if working.order.symbol == symbol and working.order.side is side:
                if working.filled_qty < working.order.quantity:
                    self._stats.canceled += 1
                continue
            remaining.append(working)
        self._pending = remaining

    def exit_quote(
        self,
        *,
        side: TradeSide,
        price: Decimal,
        quantity: int,
        adv_dollar: Decimal,
        atr_pct: float | None,
    ) -> tuple[Decimal, float]:
        """Friction-adjusted price for a bracket/end-of-test exit fill."""
        notional = price * Decimal(quantity)
        return self._slippage.fill_price(
            side=side,
            reference_price=price,
            order_notional=notional,
            adv_dollar=adv_dollar,
            atr_pct=atr_pct,
        )

    def cancel_all(self) -> None:
        for working in self._pending:
            if working.filled_qty < working.order.quantity:
                self._stats.canceled += 1
        self._pending = []

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    def _replace_pending(self, order: ExecutionOrder) -> None:
        self._pending = [
            working
            for working in self._pending
            if not (
                working.order.symbol == order.symbol
                and working.order.side is order.side
            )
        ]
        self._pending.append(_WorkingOrder(order=order))

    def _step(
        self,
        working: _WorkingOrder,
        bar: PriceBar,
        adv_dollar: Decimal,
        atr_pct: float | None,
    ) -> _ExecutionStep:
        order = working.order
        remaining_qty = order.quantity - working.filled_qty
        if remaining_qty <= 0:
            return _ExecutionStep(fills=(), keep_alive=False)
        max_fillable = self._liquidity.max_fillable(bar)
        if max_fillable <= 0:
            return _ExecutionStep(fills=(), keep_alive=True)
        fill_qty, fill_price, slippage_bps = self._quote(
            order, bar, adv_dollar, atr_pct, max_fillable
        )
        if fill_price is None or fill_qty <= 0:
            return _ExecutionStep(fills=(), keep_alive=True)
        fill_qty = min(fill_qty, remaining_qty)
        commission = self._costs.commission_for(fill_qty, fill_price)
        partial = fill_qty < remaining_qty
        working.filled_qty += fill_qty
        working.partial = working.partial or partial
        self._record_fill(
            fill_price=fill_price,
            fill_qty=fill_qty,
            commission=commission,
            slippage_bps=slippage_bps,
            reference=bar.open,
            side=order.side,
        )
        complete = working.filled_qty >= order.quantity
        if complete:
            self._stats.filled += 1
            if working.partial:
                self._stats.partial_fills += 1
        fill = ExecutionFill(
            symbol=order.symbol,
            side=order.side,
            quantity=fill_qty,
            price=fill_price,
            commission=commission,
            slippage_bps=slippage_bps,
            ts=bar.ts,
            partial=partial,
        )
        return _ExecutionStep(fills=(fill,), keep_alive=not complete)

    def _quote(
        self,
        order: ExecutionOrder,
        bar: PriceBar,
        adv_dollar: Decimal,
        atr_pct: float | None,
        max_fillable: int,
    ) -> tuple[int, Decimal | None, float]:
        """Order-type-aware fill price for this bar.

        Returns ``(fill_qty, fill_price_or_None, slippage_bps)``; a ``None``
        price means the order is not touched this bar (e.g. a limit that never
        trades or a stop that never triggers).
        """
        fill_qty = min(order.quantity, max_fillable)
        notional = bar.open * Decimal(fill_qty)
        if order.order_type is OrderType.MARKET:
            price, bps = self._slippage.fill_price(
                side=order.side,
                reference_price=bar.open,
                order_notional=notional,
                adv_dollar=adv_dollar,
                atr_pct=atr_pct,
            )
            return fill_qty, price, bps

        if order.order_type is OrderType.LIMIT:
            limit = order.limit_price
            if limit is None:
                return fill_qty, bar.open, 0.0
            if order.side is TradeSide.BUY:
                if bar.low > limit:
                    return 0, None, 0.0
                price = min(bar.open, limit)
            else:
                if bar.high < limit:
                    return 0, None, 0.0
                price = max(bar.open, limit)
            return fill_qty, price.quantize(_PRICE_QUANT), 0.0

        # STOP -> market once triggered; gaps fill at the (worse) open.
        stop = order.stop_price
        if stop is None:
            return fill_qty, bar.open, 0.0
        if order.side is TradeSide.BUY:
            if bar.high < stop:
                return 0, None, 0.0
            reference = bar.open if bar.open >= stop else stop
        else:
            if bar.low > stop:
                return 0, None, 0.0
            reference = bar.open if bar.open <= stop else stop
        price, bps = self._slippage.fill_price(
            side=order.side,
            reference_price=reference,
            order_notional=notional,
            adv_dollar=adv_dollar,
            atr_pct=atr_pct,
        )
        return fill_qty, price, bps

    def _record_fill(
        self,
        *,
        fill_price: Decimal,
        fill_qty: int,
        commission: Decimal,
        slippage_bps: float,
        reference: Decimal,
        side: TradeSide,
    ) -> None:
        self._stats.total_commission += commission
        self._stats.total_slippage += (
            Decimal(str(slippage_bps)) / Decimal(10000) * fill_price * Decimal(fill_qty)
        )
        self._stats.slippage_bps_values.append(slippage_bps)
        if reference > 0:
            deviation = abs(fill_price - reference) / reference * Decimal(10000)
            self._stats.deviation_bps_values.append(float(deviation))


__all__ = ["ExecutionSimulator"]
