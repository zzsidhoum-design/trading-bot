"""Execution-aware backtesting — routes the research fill loop through the simulator.

The :class:`ExecutionBroker` speaks the ``BacktestRunner._simulate`` fill
contract (``queue`` / ``fills_at`` / ``commission_for`` / ``exit_fill`` /
``pending``) while delegating every fill to the :class:`ExecutionSimulator`, so
the existing strategy research engine runs execution-aware with zero changes to
its logic. :class:`ExecutionAwareBacktestRunner` is a drop-in ``BacktestRunner``
that builds the broker (and per-run execution statistics) for one scenario.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from qtrader.application.execution.costs import TransactionCostModel
from qtrader.application.execution.liquidity import LiquidityModel
from qtrader.application.execution.models import (
    ExecutionOrder,
    ExecutionPlan,
    ExecutionScenario,
    ExecutionStats,
    LiquidityAssessment,
    TradingHoursPolicy,
)
from qtrader.application.execution.simulator import ExecutionSimulator
from qtrader.application.execution.slippage import SlippageModel
from qtrader.application.services.backtest import (
    BacktestBroker,
    BacktestFill,
    BacktestOrder,
    BacktestParams,
    BacktestResult,
    BacktestRunner,
)
from qtrader.domain.entities import BacktestRun, IndicatorSnapshot
from qtrader.domain.value_objects import Interval, OrderType, PriceBar, TradeSide

_PRICE_QUANT = Decimal("0.000001")


class ExecutionBroker(BacktestBroker):
    """Adapts the :class:`ExecutionSimulator` to the research fill contract."""

    def __init__(
        self,
        simulator: ExecutionSimulator,
        liquidity: LiquidityModel,
        costs: TransactionCostModel,
        adv: dict[str, tuple[Decimal, Decimal]],
        atr_by_symbol: dict[str, dict[datetime, float | None]],
        trading_hours: TradingHoursPolicy | None = None,
        *,
        entry_order_type: OrderType = OrderType.MARKET,
        exit_order_type: OrderType = OrderType.MARKET,
        limit_offset_bps: float = 0.0,
        stop_offset_bps: float = 0.0,
    ) -> None:
        super().__init__()
        self._simulator = simulator
        self._liquidity = liquidity
        self._costs = costs
        self._adv = adv
        self._atr = atr_by_symbol
        self._hours = trading_hours or TradingHoursPolicy()
        self._entry_order_type = entry_order_type
        self._exit_order_type = exit_order_type
        self._limit_offset_bps = limit_offset_bps
        self._stop_offset_bps = stop_offset_bps
        self._last_close: dict[str, Decimal] = {}

    @property
    def stats(self) -> ExecutionStats:
        return self._simulator.stats

    def queue(self, order: BacktestOrder) -> None:
        ref_price = self._last_close.get(order.symbol, Decimal("0"))
        adv_volume, adv_dollar = self._adv.get(order.symbol, (Decimal("0"), Decimal("0")))
        order_type = (
            self._entry_order_type
            if order.side is TradeSide.BUY
            else self._exit_order_type
        )
        limit_price = None
        stop_price = None
        if order_type is OrderType.LIMIT:
            offset = Decimal(str(self._limit_offset_bps)) / Decimal(10000)
            direction = Decimal(1) - offset if order.side is TradeSide.BUY else Decimal(1) + offset
            limit_price = (ref_price * direction).quantize(_PRICE_QUANT)
        elif order_type is OrderType.STOP:
            offset = Decimal(str(self._stop_offset_bps)) / Decimal(10000)
            direction = Decimal(1) + offset if order.side is TradeSide.BUY else Decimal(1) - offset
            stop_price = (ref_price * direction).quantize(_PRICE_QUANT)
        self._simulator.submit(
            ExecutionOrder(
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                order_type=order_type,
                signal_ts=order.signal_ts,
                limit_price=limit_price,
                stop_price=stop_price,
            ),
            ref_price=ref_price,
            adv_volume=adv_volume,
            adv_dollar=adv_dollar,
        )

    def fills_at(self, bar: PriceBar) -> list[BacktestFill]:
        self._last_close[bar.symbol] = bar.close
        adv_volume, adv_dollar = self._adv.get(bar.symbol, (Decimal("0"), Decimal("0")))
        atr_pct = self._atr.get(bar.symbol, {}).get(bar.ts)
        fills = self._simulator.process_bar(
            bar,
            adv_volume=adv_volume,
            adv_dollar=adv_dollar,
            atr_pct=atr_pct,
            tradable=self._hours.tradable(bar.ts),
        )
        return [
            BacktestFill(
                symbol=fill.symbol,
                side=fill.side,
                quantity=fill.quantity,
                price=fill.price,
                commission=fill.commission,
                ts=fill.ts,
            )
            for fill in fills
        ]

    def commission_for(self, quantity: int, price: Decimal) -> Decimal:
        return self._costs.commission_for(quantity, price)

    def exit_fill(
        self,
        symbol: str,
        side: TradeSide,
        quantity: int,
        price: Decimal,
        ts: datetime,
    ) -> BacktestFill:
        """Bracket/end-of-test exit: cancel working orders, then fill with friction."""
        self._simulator.cancel_side(symbol, side)
        adv_dollar = self._adv.get(symbol, (Decimal("0"), Decimal("0")))[1]
        atr_pct = self._atr.get(symbol, {}).get(ts)
        adjusted, _bps = self._simulator.exit_quote(
            side=side,
            price=price,
            quantity=quantity,
            adv_dollar=adv_dollar,
            atr_pct=atr_pct,
        )
        return BacktestFill(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=adjusted,
            commission=self._costs.commission_for(quantity, adjusted),
            ts=ts,
        )

    @property
    def pending(self) -> list[BacktestOrder]:
        return [
            BacktestOrder(
                symbol=working.symbol,
                side=working.side,
                quantity=working.quantity,
                signal_ts=working.signal_ts,
            )
            for working in self._simulator.pending
        ]


class ExecutionAwareBacktestRunner(BacktestRunner):
    """``BacktestRunner`` whose fills go through the execution simulator.

    One runner instance executes one scenario (fresh simulator per run). After a
    run, :meth:`last_stats` exposes the execution statistics for metrics.
    """

    def __init__(
        self,
        *,
        scenario: ExecutionScenario,
        plan: ExecutionPlan,
        trading_hours: TradingHoursPolicy | None = None,
        entry_order_type: OrderType = OrderType.MARKET,
        exit_order_type: OrderType = OrderType.MARKET,
        limit_offset_bps: float = 0.0,
        stop_offset_bps: float = 0.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._scenario = scenario
        self._plan = plan
        self._hours = trading_hours
        self._entry_order_type = entry_order_type
        self._exit_order_type = exit_order_type
        self._limit_offset_bps = limit_offset_bps
        self._stop_offset_bps = stop_offset_bps
        self._last_stats: ExecutionStats | None = None
        self._last_assessments: dict[str, LiquidityAssessment] = {}
        self._last_adv_seen: dict[str, tuple[Decimal, Decimal]] = {}

    @property
    def scenario(self) -> ExecutionScenario:
        return self._scenario

    def last_stats(self) -> ExecutionStats | None:
        return self._last_stats

    def last_assessments(self) -> dict[str, LiquidityAssessment]:
        """Latest liquidity assessments (per symbol that submitted) from the last run."""
        return self._last_assessments or {}

    def last_adv_seen(self) -> dict[str, tuple[Decimal, Decimal]]:
        """(avg volume, avg dollar volume) per submitted symbol from the last run."""
        return self._last_adv_seen or {}

    def _simulate(
        self,
        run: BacktestRun,
        bars_by_symbol: dict[str, list[PriceBar]],
        initial_capital: Decimal,
        params: BacktestParams,
        model_outputs: dict[str, dict[Any, float]] | None = None,
        series: dict[str, list[IndicatorSnapshot]] | None = None,
        sectors: dict[str, str] | None = None,
        broker: BacktestBroker | None = None,
    ) -> BacktestResult:
        liquidity = LiquidityModel(self._plan.liquidity)
        slippage = SlippageModel(self._plan.slippage_for(self._scenario))
        costs = TransactionCostModel(
            self._plan.commission_bps, self._plan.min_commission
        )
        simulator = ExecutionSimulator(slippage, liquidity, costs, seed=self._plan.seed)
        adv: dict[str, tuple[Decimal, Decimal]] = {}
        atr_by_symbol: dict[str, dict[datetime, float | None]] = {}
        for symbol, bars in bars_by_symbol.items():
            if not bars:
                continue
            adv[symbol] = liquidity.adv_for(bars)
            atr_by_symbol[symbol] = self._atr_pct_series(bars, params.interval)
        broker = ExecutionBroker(
            simulator,
            liquidity,
            costs,
            adv,
            atr_by_symbol,
            self._hours,
            entry_order_type=self._entry_order_type,
            exit_order_type=self._exit_order_type,
            limit_offset_bps=self._limit_offset_bps,
            stop_offset_bps=self._stop_offset_bps,
        )
        try:
            result = super()._simulate(
                run,
                bars_by_symbol,
                initial_capital,
                params,
                model_outputs=model_outputs,
                series=series,
                sectors=sectors,
                broker=broker,
            )
        finally:
            self._last_stats = simulator.stats
            self._last_assessments = simulator.assessments
            self._last_adv_seen = simulator.adv_seen
        return result

    def _atr_pct_series(
        self, bars: list[PriceBar], interval: Interval
    ) -> dict[datetime, float | None]:
        """per-bar ATR% (relative to close) for the volatility term."""
        try:
            snapshots = self._indicator_engine.compute_series(bars, bars[0].symbol, interval)
        except Exception:
            return {}
        out: dict[datetime, float | None] = {}
        for index, bar in enumerate(bars):
            snapshot = snapshots[index] if index < len(snapshots) else None
            atr = getattr(snapshot, "atr", None) if snapshot is not None else None
            if atr is None or bar.close <= 0:
                out[bar.ts] = None
            else:
                out[bar.ts] = float(atr / bar.close * 100)
        return out


__all__ = ["ExecutionAwareBacktestRunner", "ExecutionBroker"]
