"""Simulated execution — research-only integration with the Phase 4 simulator.

A cleared AI decision is submitted to the real :class:`ExecutionSimulator` and
processed bar-by-bar under explicit slippage/liquidity assumptions. The result
is a :class:`ExecutionOutcome` used for research (ablation, cost-adjusted
metrics) — never for live orders.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from qtrader.application.ai.models import ExecutionAssumptions, ExecutionOutcome
from qtrader.application.execution.costs import TransactionCostModel
from qtrader.application.execution.liquidity import LiquidityModel
from qtrader.application.execution.models import (
    ExecutionFill,
    ExecutionOrder,
    ExecutionScenario,
    LiquidityAssumptions,
    default_slippage_assumptions,
)
from qtrader.application.execution.simulator import ExecutionSimulator
from qtrader.application.execution.slippage import SlippageModel
from qtrader.application.portfolio_mgmt.models import ClearedOrder
from qtrader.domain.value_objects import OrderType, PriceBar, TradeSide


class SimulatedExecution:
    """Runs one cleared order through the Phase 4 simulator (deterministic)."""

    def __init__(
        self,
        *,
        scenario: ExecutionScenario = ExecutionScenario.BASELINE,
        commission_bps: float = 10.0,
        max_participation_rate: float = 0.10,
        seed: int = 42,
    ) -> None:
        self._scenario = scenario
        self._commission_bps = commission_bps
        self._participation = max_participation_rate
        self._seed = seed

    def run(
        self,
        order: ClearedOrder,
        bars: Sequence[PriceBar],
        *,
        adv_volume: Decimal,
        adv_dollar: Decimal,
        atr_pct: float | None = None,
    ) -> ExecutionOutcome:
        """Simulate the order across ``bars``; returns a filled outcome or a
        clean (unfilled) outcome — never raises."""
        assumptions = ExecutionAssumptions(
            scenario=self._scenario.value,
            commission_bps=self._commission_bps,
            slippage_bps=self._slippage_bps(),
            max_participation_rate=self._participation,
            seed=self._seed,
        )
        if order.quantity <= 0 or not bars:
            return ExecutionOutcome(
                filled=False,
                fill_rate=0.0,
                rejected_rate=1.0,
                net_return=None,
                avg_slippage_bps=None,
                commission=Decimal("0"),
                scenario=self._scenario.value,
                assumptions=assumptions,
            )
        if len(bars) < 2:
            return ExecutionOutcome(
                filled=False,
                fill_rate=0.0,
                rejected_rate=1.0,
                net_return=None,
                avg_slippage_bps=None,
                commission=Decimal("0"),
                scenario=self._scenario.value,
                assumptions=assumptions,
            )

        slippage = SlippageModel(default_slippage_assumptions()[self._scenario])
        liquidity = LiquidityModel(
            LiquidityAssumptions(max_participation_rate=self._participation)
        )
        costs = TransactionCostModel(commission_bps=self._commission_bps)
        simulator = ExecutionSimulator(slippage, liquidity, costs, seed=self._seed)

        ref_price = bars[0].open
        execution_order = ExecutionOrder(
            symbol=order.symbol,
            side=order.side,
            quantity=int(order.quantity),
            order_type=OrderType.MARKET,
            signal_ts=order.signal_ts or bars[0].ts,
        )
        accepted = simulator.submit(
            execution_order,
            ref_price=ref_price,
            adv_volume=adv_volume,
            adv_dollar=adv_dollar,
        )
        if not accepted:
            return ExecutionOutcome(
                filled=False,
                fill_rate=0.0,
                rejected_rate=1.0,
                net_return=None,
                avg_slippage_bps=None,
                commission=Decimal("0"),
                scenario=self._scenario.value,
                assumptions=assumptions,
            )

        fills: list[ExecutionFill] = []
        for bar in bars[1:]:
            fills.extend(
                simulator.process_bar(
                    bar,
                    adv_volume=adv_volume,
                    adv_dollar=adv_dollar,
                    atr_pct=atr_pct,
                )
            )
        # Any residual is exited at the final bar close under friction.
        for working in simulator.pending:
            if working.symbol != order.symbol:
                continue
            remaining = working.quantity - sum(f.quantity for f in fills)
            if remaining <= 0:
                continue
            price, bps = simulator.exit_quote(
                side=order.side,
                price=bars[-1].close,
                quantity=remaining,
                adv_dollar=adv_dollar,
                atr_pct=atr_pct,
            )
            commission = costs.commission_for(remaining, price)
            fills.append(
                ExecutionFill(
                    symbol=order.symbol,
                    side=order.side,
                    quantity=remaining,
                    price=price,
                    commission=commission,
                    slippage_bps=bps,
                    ts=bars[-1].ts,
                    partial=True,
                )
            )
            simulator.stats.filled += remaining

        stats = simulator.stats
        if not fills:
            return ExecutionOutcome(
                filled=False,
                fill_rate=0.0,
                rejected_rate=0.0,
                net_return=None,
                avg_slippage_bps=None,
                commission=Decimal("0"),
                scenario=self._scenario.value,
                assumptions=assumptions,
            )

        filled_qty = sum(f.quantity for f in fills)
        fill_rate = min(1.0, filled_qty / int(order.quantity))
        avg_price = self._avg_fill_price(fills)
        total_commission = sum((f.commission for f in fills), Decimal("0"))
        avg_slippage = sum(f.slippage_bps for f in fills) / len(fills)
        exit_price = self._exit_price(
            simulator,
            order.side,
            bars[-1].close,
            int(order.quantity),
            adv_dollar,
            atr_pct,
        )
        net_return = self._net_return(
            order.side, avg_price, exit_price, total_commission, filled_qty
        )

        return ExecutionOutcome(
            filled=fill_rate > 0.0,
            fill_rate=round(fill_rate, 6),
            rejected_rate=round(
                stats.rejected / stats.submitted if stats.submitted else 0.0, 6
            ),
            net_return=round(net_return, 6) if net_return is not None else None,
            avg_slippage_bps=round(avg_slippage, 4),
            commission=total_commission.quantize(Decimal("0.01")),
            scenario=self._scenario.value,
            assumptions=assumptions,
        )

    # ------------------------------------------------------------------ #
    def _slippage_bps(self) -> float:
        return default_slippage_assumptions()[self._scenario].base_slippage_bps

    def _avg_fill_price(self, fills: Sequence[ExecutionFill]) -> Decimal:
        notional = sum((f.price * Decimal(f.quantity) for f in fills), Decimal("0"))
        qty = sum(f.quantity for f in fills)
        if qty == 0:
            return Decimal("0")
        return notional / Decimal(qty)

    def _exit_price(
        self,
        simulator: ExecutionSimulator,
        side: TradeSide,
        close: Decimal,
        quantity: int,
        adv_dollar: Decimal,
        atr_pct: float | None,
    ) -> Decimal:
        price, _ = simulator.exit_quote(
            side=side,
            price=close,
            quantity=quantity,
            adv_dollar=adv_dollar,
            atr_pct=atr_pct,
        )
        return price

    def _net_return(
        self,
        side: TradeSide,
        avg_price: Decimal,
        exit_price: Decimal,
        commission: Decimal,
        qty: int,
    ) -> float | None:
        if avg_price <= 0:
            return None
        if side is TradeSide.BUY:
            pnl = (exit_price - avg_price) * Decimal(qty) - commission
            base = avg_price * Decimal(qty)
        else:
            pnl = (avg_price - exit_price) * Decimal(qty) - commission
            base = avg_price * Decimal(qty)
        if base <= 0:
            return None
        return float(pnl / base)


__all__ = ["SimulatedExecution"]
