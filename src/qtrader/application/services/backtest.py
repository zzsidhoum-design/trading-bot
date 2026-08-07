"""Backtesting engine -- replay stored bars through the real agent logic.

Phase 6. The runner replays a chronologically-ordered bar history and feeds the
*exact* pure engines the live agents use (``IndicatorEngine`` for technicals,
``RiskCalculator`` for sizing/stops) so backtest behaviour matches production.
Fills happen at the next bar's open (with configurable slippage/commission) and
stops/take-profits are evaluated on intra-bar ranges, giving no-look-ahead
results. The whole loop is deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from qtrader.application.services.feature_store import price_features_from_bars
from qtrader.application.services.indicators import IndicatorEngine, IndicatorSnapshot
from qtrader.application.services.performance_metrics import PerformanceMetrics
from qtrader.application.services.prediction_model import LogisticModel
from qtrader.application.services.risk_calculator import RiskCalculator, RiskInputs
from qtrader.config.logging import get_logger
from qtrader.domain.entities import BacktestRun, PerformanceSummary, SystemLog
from qtrader.domain.ports import (
    BacktestRepository,
    PerformanceRepository,
    PriceRepository,
    SystemLogRepository,
)
from qtrader.domain.value_objects import (
    Decision,
    Interval,
    Money,
    PriceBar,
    TradeSide,
    TradingMode,
)

logger = get_logger("qtrader.backtest")

_BPS = Decimal("0.0001")
_PRICE_QUANT = Decimal("0.000001")


def _dec(value: Decimal | float) -> Decimal:
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class BacktestOrder:
    """A market order queued at ``signal_ts`` to fill at the next bar's open."""

    symbol: str
    side: TradeSide
    quantity: int
    signal_ts: datetime


@dataclass(frozen=True, slots=True)
class BacktestFill:
    """A single fill at a bar's open (already slippage/commission adjusted)."""

    symbol: str
    side: TradeSide
    quantity: int
    price: Decimal
    commission: Decimal
    ts: datetime


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """One round-trip (entry -> exit) recorded by the backtest."""

    symbol: str
    quantity: int
    entry_price: Decimal
    exit_price: Decimal
    pnl: Decimal
    pnl_pct: Decimal
    fees: Decimal
    entry_time: datetime
    exit_time: datetime
    outcome: str


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """What a completed run hands back: records + curves for analysis/gate."""

    run: BacktestRun
    summary: PerformanceSummary
    equity_curve: list[tuple[datetime, Decimal]]
    trades: list[ClosedTrade]


@dataclass(frozen=True, slots=True)
class BacktestParams:
    """Tunable execution assumptions for one run (kept in the run's JSON)."""

    interval: Interval = Interval.D1
    strategy: str = "ensemble"
    commission_bps: float = 0.0
    slippage_bps: float = 0.0
    warmup_bars: int = 30
    max_open_positions: int = 10
    stop_loss_pct: float = 0.03
    take_profit_pct: float = 0.06
    max_hold_bars: int = 0
    trailing_stop_pct: float = 0.0


@dataclass(slots=True)
class _OpenPosition:
    symbol: str
    quantity: int
    entry_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    entry_ts: datetime
    fees: Decimal
    entry_bar_index: int = 0
    peak: Decimal = Decimal(0)


@dataclass(slots=True)
class _Bars:
    """Per-symbol cursor used during simulation."""

    seen: list[PriceBar] = field(default_factory=list)
    last_close: Decimal = Decimal(0)


class BacktestBroker:
    """Accumulates market orders and fills them at the next bar's open.

    Slippage is applied against the open (buy high / sell low); commission is a
    basis-point rate on notional. Both are immutable per broker instance.
    """

    def __init__(self, commission_bps: float = 0.0, slippage_bps: float = 0.0) -> None:
        self._commission_rate = _dec(commission_bps) * _BPS
        self._slippage_rate = _dec(slippage_bps) * _BPS
        self._pending: list[BacktestOrder] = []

    def queue(self, order: BacktestOrder) -> None:
        self._pending.append(order)

    def fills_at(self, bar: PriceBar) -> list[BacktestFill]:
        """Consume every queued order for ``bar.symbol`` and fill at bar.open."""
        fills: list[BacktestFill] = []
        remaining: list[BacktestOrder] = []
        for order in self._pending:
            if order.symbol != bar.symbol:
                remaining.append(order)
                continue
            if order.side is TradeSide.BUY:
                price = bar.open * (Decimal(1) + self._slippage_rate)
            else:
                price = bar.open * (Decimal(1) - self._slippage_rate)
            price = price.quantize(_PRICE_QUANT)
            commission = (price * _dec(order.quantity) * self._commission_rate).quantize(
                Decimal("0.01")
            )
            fills.append(
                BacktestFill(
                    symbol=order.symbol,
                    side=order.side,
                    quantity=order.quantity,
                    price=price,
                    commission=commission,
                    ts=bar.ts,
                )
            )
        self._pending = remaining
        return fills

    @property
    def pending(self) -> list[BacktestOrder]:
        return list(self._pending)


class _SignalEngine:
    """Momentum entry/exit built strictly on data available at each bar.

    Uses the production ``IndicatorEngine`` per step; signals only become
    actionable after ``warmup_bars`` so EMAs/ATR have converged.
    """

    def __init__(
        self,
        indicator_engine: IndicatorEngine,
        warmup_bars: int = 30,
        model: LogisticModel | None = None,
        model_prob_buy: float = 0.52,
        model_prob_sell: float = 0.48,
        model_lookback: int = 120,
        model_outputs: dict[str, dict[Any, float]] | None = None,
        series: dict[str, list[IndicatorSnapshot]] | None = None,
    ) -> None:
        self._indicators = indicator_engine
        self._warmup = warmup_bars
        self._model = model
        self._prob_buy = model_prob_buy
        self._prob_sell = model_prob_sell
        self._model_lookback = model_lookback
        self._model_outputs = model_outputs
        self._series = series or {}
        self._prev_diff: dict[str, Decimal | None] = {}

    def evaluate(
        self, symbol: str, bars: list[PriceBar], interval: Interval
    ) -> tuple[Decision, IndicatorSnapshot | None]:
        """Return (BUY | SELL | HOLD, snapshot) for the latest bar."""
        if len(bars) < max(self._warmup, 3):
            return Decision.HOLD, None
        model = self._model
        if model is not None:
            prob_up = self._prob_up(symbol, bars, model)
            if prob_up < self._prob_buy and prob_up > self._prob_sell:
                return Decision.HOLD, None
            snapshot = self._snapshot(symbol, bars, interval)
            if prob_up >= self._prob_buy:
                return Decision.BUY, snapshot
            return Decision.SELL, snapshot
        snapshot = self._snapshot(symbol, bars, interval)
        return self._momentum_decision(symbol, snapshot)

    def _snapshot(
        self, symbol: str, bars: list[PriceBar], interval: Interval
    ) -> IndicatorSnapshot:
        cached = self._series.get(symbol)
        if cached:
            idx = len(bars) - 1
            if idx < len(cached):
                return cached[idx]
        return self._indicators.compute(bars, symbol, interval)

    def _prob_up(
        self, symbol: str, bars: list[PriceBar], model: LogisticModel
    ) -> float:
        if self._model_outputs is not None:
            return self._model_outputs.get(symbol, {}).get(bars[-1].ts, 0.5)
        feats = price_features_from_bars(bars[-self._model_lookback :])
        return model.predict(feats).prob_up

    def _momentum_decision(
        self, symbol: str, snapshot: IndicatorSnapshot | None
    ) -> tuple[Decision, IndicatorSnapshot | None]:
        if snapshot is None:
            return Decision.HOLD, None
        ema_fast, ema_slow = snapshot.ema_9, snapshot.ema_21
        if ema_fast is None or ema_slow is None:
            return Decision.HOLD, None
        diff = ema_fast - ema_slow
        prev = self._prev_diff.get(symbol)
        self._prev_diff[symbol] = diff

        rsi = snapshot.rsi
        decision = Decision.HOLD
        if prev is not None and diff > 0 and prev <= 0:
            decision = Decision.BUY
        elif prev is not None and diff < 0 and prev >= 0:
            decision = Decision.SELL
        if rsi is not None and rsi > 70 and decision is Decision.HOLD:
            decision = Decision.SELL
        return decision, snapshot


class BacktestRunner:
    """Replays bars through the agent pipeline and persists the outcome."""

    def __init__(
        self,
        prices: PriceRepository,
        backtests: BacktestRepository,
        performance: PerformanceRepository,
        risk_calculator: RiskCalculator,
        indicator_engine: IndicatorEngine | None = None,
        logs: SystemLogRepository | None = None,
        model: LogisticModel | None = None,
        model_prob_buy: float = 0.52,
        model_prob_sell: float = 0.48,
    ) -> None:
        self._prices = prices
        self._backtests = backtests
        self._performance = performance
        self._risk = risk_calculator
        self._indicator_engine = indicator_engine or IndicatorEngine()
        self._logs = logs
        self._model = model
        self._model_prob_buy = model_prob_buy
        self._model_prob_sell = model_prob_sell

    async def run(
        self,
        name: str,
        symbols: list[str],
        start: date,
        end: date,
        initial_capital: Decimal,
        params: BacktestParams | None = None,
        precompute_series: bool = False,
    ) -> BacktestResult:
        params = params or BacktestParams()
        run = await self._backtests.create(
            BacktestRun(
                name=name,
                universe=symbols,
                start=start,
                end=end,
                initial_capital=Money(initial_capital),
                interval=params.interval,
                strategy=params.strategy,
                commission_bps=_dec(params.commission_bps),
                slippage_bps=_dec(params.slippage_bps),
            )
        )
        await self._log("INFO", "backtest", "run started", {"run_id": run.run_id, "name": name})
        try:
            bars_by_symbol = await self._load_bars(symbols, params.interval, start, end)
            series = None
            if precompute_series:
                series = {
                    symbol: self._indicator_engine.compute_series(bars, symbol, params.interval)
                    for symbol, bars in bars_by_symbol.items()
                    if bars
                }
            result = self._simulate(run, bars_by_symbol, initial_capital, params, series=series)
            await self._persist(result)
            await self._log(
                "INFO",
                "backtest",
                "run completed",
                {
                    "run_id": run.run_id,
                    "final_capital": float(result.run.final_capital.amount)
                    if result.run.final_capital
                    else None,
                    "trades": result.summary.trades_count,
                },
            )
            return result
        except Exception:
            await self._backtests.save(self._mark_failed(run))
            await self._log("ERROR", "backtest", "run failed", {"run_id": run.run_id})
            raise

    async def _load_bars(
        self, symbols: list[str], interval: Interval, start: date, end: date
    ) -> dict[str, list[PriceBar]]:
        start_dt = datetime.combine(start, time.min, tzinfo=UTC)
        end_dt = datetime.combine(end, time.max, tzinfo=UTC)
        by_symbol: dict[str, list[PriceBar]] = {}
        for symbol in symbols:
            bars = await self._prices.history(
                symbol, interval, start_dt, end_dt, limit=50_000
            )
            by_symbol[symbol] = sorted(bars, key=lambda b: b.ts)
        return by_symbol

    def _simulate(
        self,
        run: BacktestRun,
        bars_by_symbol: dict[str, list[PriceBar]],
        initial_capital: Decimal,
        params: BacktestParams,
        model_outputs: dict[str, dict[Any, float]] | None = None,
        series: dict[str, list[IndicatorSnapshot]] | None = None,
    ) -> BacktestResult:
        broker = BacktestBroker(
            commission_bps=params.commission_bps, slippage_bps=params.slippage_bps
        )
        signals = _SignalEngine(
            self._indicator_engine,
            warmup_bars=params.warmup_bars,
            model=self._model,
            model_prob_buy=self._model_prob_buy,
            model_prob_sell=self._model_prob_sell,
            model_outputs=model_outputs,
            series=series,
        )
        cash = _dec(initial_capital)
        positions: dict[str, _OpenPosition] = {}
        cursors: dict[str, _Bars] = {}
        trades: list[ClosedTrade] = []
        equity_curve: list[tuple[datetime, Decimal]] = []
        last_ts = datetime.combine(run.end, time.min, tzinfo=UTC)

        for ts, bars in self._group_bars(bars_by_symbol):
            for bar in bars:
                cursor = cursors.setdefault(bar.symbol, _Bars())
                cursor.seen.append(bar)
                cursor.last_close = bar.close

                for fill in broker.fills_at(bar):
                    if fill.side is TradeSide.BUY:
                        cash = self._open_position(
                            fill, cash, positions, params, bar_index=len(cursor.seen)
                        )
                    else:
                        cash = self._close_position(fill, cash, positions, trades, outcome="signal")

                pos = positions.get(bar.symbol)
                if pos is not None and pos.entry_ts < bar.ts:
                    pos.peak = max(pos.peak, bar.high)
                    exit_price, outcome = self._intrabar_exit(pos, bar, params)
                    if (
                        exit_price is None
                        and params.max_hold_bars > 0
                        and len(cursor.seen) - pos.entry_bar_index >= params.max_hold_bars
                    ):
                        exit_price, outcome = bar.close, "time"
                    if exit_price is not None:
                        fill = BacktestFill(
                            symbol=pos.symbol,
                            side=TradeSide.SELL,
                            quantity=pos.quantity,
                            price=exit_price,
                            commission=Decimal(0),
                            ts=bar.ts,
                        )
                        cash = self._close_position(fill, cash, positions, trades, outcome=outcome)

                decision, snapshot = signals.evaluate(bar.symbol, cursor.seen, params.interval)
                if decision is Decision.BUY and bar.symbol not in positions:
                    cash = self._queue_buy(
                        broker, bar, snapshot, params, cash, positions, cursors
                    )
                elif decision is Decision.SELL and bar.symbol in positions:
                    broker.queue(
                        BacktestOrder(
                            symbol=bar.symbol,
                            side=TradeSide.SELL,
                            quantity=positions[bar.symbol].quantity,
                            signal_ts=bar.ts,
                        )
                    )

            equity_curve.append((ts, self._equity(cash, positions, cursors)))
            last_ts = ts

        for symbol, pos in list(positions.items()):
            end_cursor = cursors.get(symbol)
            if end_cursor is None:
                continue
            fill = BacktestFill(
                symbol=symbol,
                side=TradeSide.SELL,
                quantity=pos.quantity,
                price=end_cursor.last_close,
                commission=Decimal(0),
                ts=last_ts,
            )
            cash = self._close_position(fill, cash, positions, trades, outcome="end_of_test")

        final_capital = self._equity(cash, positions, cursors)
        summary = PerformanceMetrics.from_series(
            strategy=params.strategy,
            mode=TradingMode.BACKTEST,
            period_start=run.start,
            period_end=run.end,
            equity_curve=equity_curve,
            trade_pnl_pcts=[t.pnl_pct for t in trades],
            interval=params.interval,
        )
        finished = self._complete(run, final_capital, summary)
        return BacktestResult(
            run=finished,
            summary=summary,
            equity_curve=equity_curve,
            trades=trades,
        )

    def _queue_buy(
        self,
        broker: BacktestBroker,
        bar: PriceBar,
        snapshot: IndicatorSnapshot | None,
        params: BacktestParams,
        cash: Decimal,
        positions: dict[str, _OpenPosition],
        cursors: dict[str, _Bars],
    ) -> Decimal:
        if snapshot is None or snapshot.atr is None:
            return cash
        equity = self._equity(cash, positions, cursors)
        inputs = RiskInputs(
            decision=Decision.BUY,
            symbol=bar.symbol,
            entry_price=bar.close,
            atr=snapshot.atr,
            equity=equity,
            current_exposure_pct=0.0,
            open_positions=len(positions),
            sector_exposure_pct=0.0,
            adv_daily=bar.volume * bar.close,
            cooldown_remaining_minutes=0.0,
            daily_pnl_pct=0.0,
            trades_today=0,
        )
        assessment = self._risk.assess(inputs)
        if not assessment.approved or not assessment.position_size:
            return cash
        quantity = int(assessment.position_size)
        if quantity <= 0:
            return cash
        max_affordable = int(
            cash / (bar.close * (Decimal(1) + _BPS * _dec(params.slippage_bps)))
        )
        quantity = min(quantity, max_affordable)
        if quantity <= 0:
            return cash
        broker.queue(
            BacktestOrder(
                symbol=bar.symbol,
                side=TradeSide.BUY,
                quantity=quantity,
                signal_ts=bar.ts,
            )
        )
        return cash

    def _open_position(
        self,
        fill: BacktestFill,
        cash: Decimal,
        positions: dict[str, _OpenPosition],
        params: BacktestParams,
        bar_index: int,
    ) -> Decimal:
        cost = fill.price * _dec(fill.quantity)
        if cost + fill.commission > cash:
            return cash
        stop = fill.price * (Decimal(1) - _dec(params.stop_loss_pct))
        take = fill.price * (Decimal(1) + _dec(params.take_profit_pct))
        positions[fill.symbol] = _OpenPosition(
            symbol=fill.symbol,
            quantity=fill.quantity,
            entry_price=fill.price,
            stop_loss=stop,
            take_profit=take,
            entry_ts=fill.ts,
            fees=fill.commission,
            entry_bar_index=bar_index,
            peak=fill.price,
        )
        return cash - cost - fill.commission

    def _close_position(
        self,
        fill: BacktestFill,
        cash: Decimal,
        positions: dict[str, _OpenPosition],
        trades: list[ClosedTrade],
        outcome: str,
    ) -> Decimal:
        pos = positions.pop(fill.symbol, None)
        if pos is None:
            return cash
        proceeds = fill.price * _dec(fill.quantity) - fill.commission
        pnl = proceeds - pos.entry_price * _dec(pos.quantity) - pos.fees
        entry_cost = pos.entry_price * _dec(pos.quantity) + pos.fees
        pnl_pct = pnl / entry_cost if entry_cost else Decimal(0)
        trades.append(
            ClosedTrade(
                symbol=pos.symbol,
                quantity=pos.quantity,
                entry_price=pos.entry_price,
                exit_price=fill.price,
                pnl=pnl,
                pnl_pct=pnl_pct,
                fees=pos.fees + fill.commission,
                entry_time=pos.entry_ts,
                exit_time=fill.ts,
                outcome=outcome,
            )
        )
        return cash + proceeds

    @staticmethod
    def _intrabar_exit(
        pos: _OpenPosition, bar: PriceBar, params: BacktestParams
    ) -> tuple[Decimal | None, str]:
        if bar.low <= pos.stop_loss and bar.high >= pos.take_profit:
            return pos.stop_loss, "stop"
        if bar.low <= pos.stop_loss:
            return pos.stop_loss, "stop"
        if bar.high >= pos.take_profit:
            return pos.take_profit, "take_profit"
        if params.trailing_stop_pct > 0 and pos.peak > 0:
            trail_level = pos.peak * (Decimal(1) - _dec(params.trailing_stop_pct))
            if bar.low <= trail_level:
                return trail_level, "trailing"
        return None, ""

    @staticmethod
    def _group_bars(
        bars_by_symbol: dict[str, list[PriceBar]],
    ) -> list[tuple[datetime, list[PriceBar]]]:
        grouped: dict[datetime, list[PriceBar]] = {}
        for bars in bars_by_symbol.values():
            for bar in bars:
                grouped.setdefault(bar.ts, []).append(bar)
        return sorted(grouped.items(), key=lambda item: item[0])

    @staticmethod
    def _equity(
        cash: Decimal,
        positions: dict[str, _OpenPosition],
        cursors: dict[str, _Bars],
    ) -> Decimal:
        total = cash
        for symbol, pos in positions.items():
            cursor = cursors.get(symbol)
            if cursor is not None:
                total += cursor.last_close * _dec(pos.quantity)
        return total

    async def _persist(self, result: BacktestResult) -> None:
        await self._backtests.save(result.run)
        await self._performance.upsert(result.summary)

    @staticmethod
    def _complete(
        run: BacktestRun, final_capital: Decimal, summary: PerformanceSummary
    ) -> BacktestRun:
        return replace(
            run,
            final_capital=Money(final_capital),
            metrics=summary,
            status="completed",
        )

    @staticmethod
    def _mark_failed(run: BacktestRun) -> BacktestRun:
        return replace(run, status="failed")

    async def _log(self, level: str, component: str, message: str, context: dict) -> None:
        if self._logs is None:
            return
        await self._logs.record(
            SystemLog(level=level, component=component, message=message, context=context)
        )


__all__ = [
    "BacktestBroker",
    "BacktestFill",
    "BacktestOrder",
    "BacktestParams",
    "BacktestResult",
    "BacktestRunner",
    "ClosedTrade",
]
