"""Walk-forward out-of-sample validator (Phase 6 gate hardening).

Each fold trains the logistic model on *past* bars only, then replays the exact
``BacktestRunner`` engine (next-bar fills, intra-bar stops/targets, ``RiskCalculator``
sizing, real costs) on the *held-out* next fold. Every aggregate metric is therefore
purely out-of-sample — the numbers the ``SystemGate`` should trade against, instead
of an in-sample backtest that leaks the future.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from qtrader.application.services.backtest import BacktestParams, BacktestResult, BacktestRunner
from qtrader.application.services.feature_store import FEATURE_NAMES, price_features_from_bars
from qtrader.application.services.indicators import IndicatorEngine
from qtrader.application.services.model_trainer import (
    fit_logistic,
    fit_platt_calibration,
    logits_for_fit,
    split_calibration_samples,
)
from qtrader.application.services.performance_metrics import PerformanceMetrics
from qtrader.application.services.prediction_model import LogisticModel
from qtrader.application.services.risk_calculator import RiskCalculator
from qtrader.config.logging import get_logger
from qtrader.domain.entities import BacktestRun, PerformanceSummary, SystemLog
from qtrader.domain.ports import (
    BacktestRepository,
    PerformanceRepository,
    PriceRepository,
    SystemLogRepository,
)
from qtrader.domain.value_objects import Interval, Money, TradingMode

logger = get_logger("qtrader.walk_forward")

STRATEGY_LABEL = "walk-forward"


class WalkForwardValidator:
    """Train-on-past, validate-on-heldout, net-of-cost performance gate.

    Reuses ``BacktestRunner`` so backtests and the walk-forward gate share the
    exact same execution model — the only difference is which model drives the
    signals (a fold-trained ``LogisticModel`` vs. the live registered one).
    """

    def __init__(
        self,
        prices: PriceRepository,
        performance: PerformanceRepository,
        risk_calculator: RiskCalculator,
        indicator_engine: IndicatorEngine | None = None,
        logs: SystemLogRepository | None = None,
        min_train_samples: int = 50,
        folds: int = 5,
        lookback_bars: int = 60,
        horizon_bars: int = 12,
        prob_buy: float = 0.52,
        prob_sell: float = 0.48,
    ) -> None:
        self._prices = prices
        self._performance = performance
        self._risk = risk_calculator
        self._indicator_engine = indicator_engine or IndicatorEngine()
        self._logs = logs
        self._min_train_samples = min_train_samples
        self._folds = folds
        self._lookback_bars = lookback_bars
        self._horizon_bars = horizon_bars
        self._prob_buy = prob_buy
        self._prob_sell = prob_sell

    async def validate(
        self,
        symbols: list[str],
        start: date,
        end: date,
        initial_capital: Decimal,
        *,
        interval: Interval = Interval.D1,
        commission_bps: float = 1.0,
        slippage_bps: float = 5.0,
    ) -> PerformanceSummary | None:
        bars_by_symbol = await self._load_bars(symbols, interval, start, end)
        if not bars_by_symbol:
            await self._log("WARN", "walk-forward: no price history")
            return None
        folds = self._make_folds(bars_by_symbol)
        if not folds:
            await self._log("WARN", "walk-forward: not enough bars for folds")
            return None

        all_trades: list[Decimal] = []
        curve: list[tuple[datetime, Decimal]] = []
        equity = initial_capital
        trained_any = False
        for index, (train, full, ts, te) in enumerate(folds):
            model = self._fit_model(train)
            if model is None:
                logger.warning(
                    "walk_forward.fold_skipped",
                    fold=index,
                    reason="insufficient training data",
                )
                continue
            trained_any = True
            result = self._simulate_fold(
                symbols,
                interval,
                model,
                full,
                ts,
                te,
                initial_capital,
                commission_bps,
                slippage_bps,
            )
            all_trades.extend(t.pnl_pct for t in result.trades)
            curve, equity = self._chain_curve(curve, result.equity_curve, equity)
            logger.info(
                "walk_forward.fold",
                fold=index,
                trades=result.summary.trades_count,
                sharpe=_optional_float(result.summary.sharpe),
                ret=_optional_float(result.summary.total_return),
            )
        if not trained_any:
            await self._log("WARN", "walk-forward: no fold yielded a trainable model")
            return None

        aggregate = PerformanceMetrics.from_series(
            strategy=STRATEGY_LABEL,
            mode=TradingMode.BACKTEST,
            period_start=start,
            period_end=end,
            equity_curve=curve,
            trade_pnl_pcts=all_trades,
            interval=interval,
        )
        await self._performance.upsert(aggregate)
        await self._log(
            "INFO",
            "walk-forward OOS validated",
            trades=aggregate.trades_count,
            sharpe=_optional_float(aggregate.sharpe),
            total_return=_optional_float(aggregate.total_return),
            profit_factor=_optional_float(aggregate.profit_factor),
        )
        return aggregate

    async def _load_bars(
        self, symbols: list[str], interval: Interval, start: date, end: date
    ) -> dict[str, list[Any]]:
        start_dt = datetime.combine(start, time.min, tzinfo=UTC)
        end_dt = datetime.combine(end, time.max, tzinfo=UTC)
        by_symbol: dict[str, list[Any]] = {}
        for symbol in symbols:
            bars = await self._prices.history(symbol, interval, start_dt, end_dt, limit=50_000)
            by_symbol[symbol] = sorted(bars, key=lambda b: b.ts)
        return by_symbol

    def _make_folds(
        self, bars_by_symbol: dict[str, list[Any]]
    ) -> list[tuple[dict[str, list[Any]], dict[str, list[Any]], int, int]]:
        """Expanding-train / forward-test folds.

        Returns ``(train, full, ts, te)`` per fold where ``train`` is the data the
        model fits on, ``full`` is the entire history up to the end of the held-out
        window (so feature windows have complete history), and ``(ts, te)`` are the
        [start, end) *global* indices of the out-of-sample window the fold trades.
        Guard bars at the front give the earliest model enough history to train, and
        windows are sized so each held-out block yields real predictions.
        """
        min_len = min(len(bars) for bars in bars_by_symbol.values())
        guard = self._lookback_bars + self._horizon_bars
        block = max(self._lookback_bars, self._horizon_bars)
        usable = min_len - guard
        if usable < 2 * block:
            return []
        num_blocks = max(1, usable // block)
        num_blocks = min(num_blocks, self._folds)
        block_len = usable // num_blocks
        folds: list[tuple[dict[str, list[Any]], dict[str, list[Any]], int, int]] = []
        for k in range(num_blocks):
            ts = guard + k * block_len
            te = ts + block_len
            train = {s: bars[:ts] for s, bars in bars_by_symbol.items()}
            full = {s: bars[:te] for s, bars in bars_by_symbol.items()}
            folds.append((train, full, ts, te))
        return folds

    def _fit_model(self, train: dict[str, list[Any]]) -> LogisticModel | None:
        lb = self._lookback_bars
        hb = self._horizon_bars
        per_symbol: list[list[tuple[int, list[float], int]]] = []
        for bars in train.values():
            sym_samples: list[tuple[int, list[float], int]] = []
            for i in range(lb, len(bars) - hb):
                window = bars[i - lb : i]
                feats = price_features_from_bars(window)
                entry = float(bars[i].close)
                exit_ = float(bars[i + hb].close)
                forward = (exit_ - entry) / entry if entry else 0.0
                sym_samples.append(
                    (
                        i,
                        [feats.get(name, 0.0) for name in FEATURE_NAMES],
                        1 if forward > 0 else 0,
                    )
                )
            per_symbol.append(sym_samples)
        fit_x, fit_y, cal_x, cal_y = split_calibration_samples(per_symbol)
        fit = fit_logistic(fit_x, fit_y)
        if fit is None or fit["samples"] < self._min_train_samples:
            return None
        calib_a, calib_b = 1.0, 0.0
        if cal_x:
            cal = fit_platt_calibration(logits_for_fit(cal_x, fit), cal_y)
            if cal is not None:
                calib_a, calib_b = cal
        return LogisticModel(
            feature_names=fit["feature_names"],
            coef=fit["coef"],
            intercept=fit["intercept"],
            mean=fit["mean"],
            std=fit["std"],
            calib_a=calib_a,
            calib_b=calib_b,
        )

    def _simulate_fold(
        self,
        symbols: list[str],
        interval: Interval,
        model: LogisticModel,
        full: dict[str, list[Any]],
        ts: int,
        te: int,
        initial_capital: Decimal,
        commission_bps: float,
        slippage_bps: float,
    ) -> BacktestResult:
        all_dates = [
            bar.ts.date() for bars in full.values() for bar in bars[ts:te]
        ]
        fold_start = min(all_dates) if all_dates else datetime.now(UTC).date()
        fold_end = max(all_dates) if all_dates else fold_start
        run = BacktestRun(
            name=f"walk-forward-{fold_start.isoformat()}",
            universe=symbols,
            start=fold_start,
            end=fold_end,
            initial_capital=Money(initial_capital),
            interval=interval,
            strategy=STRATEGY_LABEL,
            commission_bps=Decimal(str(commission_bps)),
            slippage_bps=Decimal(str(slippage_bps)),
        )
        runner = BacktestRunner(
            prices=self._prices,
            backtests=_NoopBacktestRepository(),
            performance=self._performance,
            risk_calculator=self._risk,
            indicator_engine=self._indicator_engine,
            model=model,
            model_prob_buy=self._prob_buy,
            model_prob_sell=self._prob_sell,
        )
        params = BacktestParams(
            interval=interval,
            strategy=STRATEGY_LABEL,
            commission_bps=commission_bps,
            slippage_bps=slippage_bps,
        )
        # Bars outside the held-out window get prob 0.5 -> always HOLD, so they
        # contribute feature history but never trade. Only [ts, te) can open trades.
        probs = self._precompute_probs(model, full, ts, te)
        # Precompute the indicator series once per symbol (O(n) total) instead of
        # re-running the whole frame per bar; the model crosses its thresholds on
        # most bars, so the on-signal-only shortcut rarely skips work.
        series = {
            symbol: self._indicator_engine.compute_series(bars, symbol, interval)
            for symbol, bars in full.items()
            if bars
        }
        return runner._simulate(
            run, full, initial_capital, params, model_outputs=probs, series=series
        )

    def _precompute_probs(
        self,
        model: LogisticModel,
        full: dict[str, list[Any]],
        ts: int,
        te: int,
    ) -> dict[str, dict[Any, float]]:
        """Model prob_up per (symbol, bar ts) for the held-out window, O(n).

        Mirrors the live engine: features end at the current bar (decision at
        close, fill at next open), window is the model's training lookback.
        """
        out: dict[str, dict[Any, float]] = {}
        lb = self._lookback_bars
        for symbol, bars in full.items():
            probs: dict[Any, float] = {}
            for i in range(max(ts, 0), te):
                window = bars[max(0, i - lb + 1) : i + 1]
                if len(window) < lb:
                    probs[bars[i].ts] = 0.5
                    continue
                feats = price_features_from_bars(window)
                probs[bars[i].ts] = model.predict(feats).prob_up
            out[symbol] = probs
        return out

    @staticmethod
    def _chain_curve(
        curve: list[tuple[datetime, Decimal]],
        fold_curve: list[tuple[datetime, Decimal]],
        equity: Decimal,
    ) -> tuple[list[tuple[datetime, Decimal]], Decimal]:
        """Append a fold's real mark-to-market curve, compounding across folds.

        Each fold's simulation restarts at ``initial_capital``, so its curve is
        rescaled by the aggregate-equity ratio before appending. This preserves
        true portfolio compounding (position sizing included) instead of applying
        each trade's own P/L percent to the whole portfolio. Points already
        covered by earlier folds are skipped.
        """
        if not fold_curve:
            return curve, equity
        base = fold_curve[0][1]
        scale = equity / base if base > 0 else Decimal(1)
        last_ts = curve[-1][0] if curve else fold_curve[0][0]
        chained: list[tuple[datetime, Decimal]] = []
        agg = equity
        for ts, eq in fold_curve:
            if curve and ts <= last_ts:
                continue
            scaled = eq * scale
            chained.append((ts, scaled))
            agg = scaled
            last_ts = ts
        return curve + chained, agg

    async def _log(self, level: str, message: str, **context: Any) -> None:
        if self._logs is None:
            return
        await self._logs.record(
            SystemLog(
                level=level,
                component="walk-forward",
                message=message,
                context=context,
            )
        )


class _NoopBacktestRepository(BacktestRepository):
    """Backtrack runs are ephemeral; only the aggregate summary is persisted."""

    async def create(self, run: BacktestRun) -> BacktestRun:
        return replace(run, run_id=run.run_id)

    async def save(self, run: BacktestRun) -> BacktestRun:
        return run

    async def get(self, run_id: int) -> BacktestRun | None:
        return None

    async def latest(self, name: str | None = None, limit: int = 5) -> list[BacktestRun]:
        return []


def _optional_float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)
