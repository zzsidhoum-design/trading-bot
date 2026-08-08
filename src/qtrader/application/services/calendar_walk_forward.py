"""Calendar-aligned, point-in-time walk-forward validator (Phase 19 fix).

The original ``WalkForwardValidator`` aligns folds by *bar index across the
universe* (``walk_forward._make_folds``), so the same index window maps to
different calendar periods for different symbols (e.g. full-history names are
OOS-tested only in 2022-2023 while newly-listed names are OOS-tested in
2024-2026). It also trains and trades symbols that were not listed yet at the
fold's decision time (look-ahead from a current-membership universe).

This validator fixes both flaws:
- folds split the *calendar* timeline into contiguous blocks; each symbol is
  OOS-tested only inside its block's calendar dates;
- the point-in-time universe filter (``universe.listing_date_from_first_bar``)
  makes a symbol eligible only if it was listed *before* the fold's OOS window,
  so no future listing is ever trained or traded on.

Reuses the exact ``BacktestRunner._simulate`` engine (same fills, stops,
targets, sizing, costs), so A/B against the aligned validator is apples-to-
apples apart from the fold/universe construction.
"""

from __future__ import annotations

from dataclasses import dataclass
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
from qtrader.application.services.prediction_model import LogisticModel
from qtrader.application.services.risk_calculator import RiskCalculator
from qtrader.application.services.universe import listing_date_from_first_bar
from qtrader.config.logging import get_logger
from qtrader.domain.entities import BacktestRun, PerformanceSummary
from qtrader.domain.ports import BacktestRepository, PerformanceRepository, PriceRepository
from qtrader.domain.value_objects import Interval, Money, TradingMode

logger = get_logger("qtrader.calendar_walk_forward")

CALENDAR_STRATEGY_LABEL = "calendar-walk-forward"


@dataclass(frozen=True, slots=True)
class CalendarFold:
    """One calendar-aligned out-of-sample block.

    ``train`` holds only bars strictly before ``fold_start`` for point-in-time
    eligible symbols; ``full`` extends each eligible symbol to the block end so
    feature windows have complete history; ``oos`` is the per-symbol list of
    bars inside ``[fold_start, fold_end)`` — the only bars that may trade.
    """

    train: dict[str, list[Any]]
    full: dict[str, list[Any]]
    oos: dict[str, list[Any]]
    fold_start: datetime
    fold_end: datetime


class CalendarWalkForwardValidator:
    """Train-on-past / test-on-future with calendar folds and PIT universe."""

    def __init__(
        self,
        prices: PriceRepository,
        risk_calculator: RiskCalculator,
        indicator_engine: IndicatorEngine | None = None,
        min_train_samples: int = 50,
        lookback_bars: int = 60,
        horizon_bars: int = 12,
        prob_buy: float = 0.60,
        prob_sell: float = 0.40,
        sectors: dict[str, str] | None = None,
    ) -> None:
        self._prices = prices
        self._risk = risk_calculator
        self._indicator_engine = indicator_engine or IndicatorEngine()
        self._min_train_samples = min_train_samples
        self._lookback_bars = lookback_bars
        self._horizon_bars = horizon_bars
        self._prob_buy = prob_buy
        self._prob_sell = prob_sell
        self._sectors = sectors

    # ------------------------------------------------------------------ folds

    def make_folds(
        self,
        bars_by_symbol: dict[str, list[Any]],
        start: date,
        end: date,
        n_folds: int = 5,
    ) -> list[CalendarFold]:
        """Calendar blocks; PIT eligibility = listed strictly before block start."""
        start_dt = datetime.combine(start, time.min, tzinfo=UTC)
        end_dt = datetime.combine(end, time.max, tzinfo=UTC)
        block = (end_dt - start_dt) / max(n_folds, 1)
        listing = {
            s: listing_date_from_first_bar(bars[0].ts)
            for s, bars in bars_by_symbol.items()
            if bars
        }
        folds: list[CalendarFold] = []
        for k in range(n_folds):
            b_start = start_dt + k * block
            b_end = start_dt + (k + 1) * block
            eligible = [
                s
                for s, bars in bars_by_symbol.items()
                if listing.get(s) is not None and listing[s] < b_start.date() and bars
            ]
            train = {s: [b for b in bars_by_symbol[s] if b.ts < b_start] for s in eligible}
            full = {s: [b for b in bars_by_symbol[s] if b.ts <= b_end] for s in eligible}
            oos = {
                s: [b for b in full[s] if b_start <= b.ts < b_end]
                for s in eligible
            }
            folds.append(CalendarFold(train=train, full=full, oos=oos,
                                      fold_start=b_start, fold_end=b_end))
        return folds

    # ------------------------------------------------------------------ model

    def fit_model(self, train: dict[str, list[Any]]) -> LogisticModel | None:
        """Identical training procedure to ``WalkForwardValidator._fit_model``."""
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

    # ------------------------------------------------------- probs & simulate

    def precompute_probs(
        self, model: LogisticModel, fold: CalendarFold
    ) -> dict[str, dict[Any, float]]:
        """prob_up per (symbol, bar ts) for OOS bars only; elsewhere 0.5 -> HOLD."""
        lb = self._lookback_bars
        out: dict[str, dict[Any, float]] = {}
        for symbol, bars in fold.full.items():
            idx_of = {b.ts: i for i, b in enumerate(bars)}
            probs: dict[Any, float] = {}
            for bar in fold.oos.get(symbol, []):
                i = idx_of[bar.ts]
                window = bars[max(0, i - lb + 1) : i + 1]
                if len(window) < lb:
                    probs[bar.ts] = 0.5
                    continue
                feats = price_features_from_bars(window)
                probs[bar.ts] = model.predict(feats).prob_up
            out[symbol] = probs
        return out

    def simulate_fold(
        self,
        prices: PriceRepository,
        backtests: BacktestRepository,
        fold: CalendarFold,
        model: LogisticModel,
        initial_capital: Decimal,
        *,
        interval: Interval = Interval.D1,
        commission_bps: float = 1.0,
        slippage_bps: float = 5.0,
        exit_cfg: dict[str, float] | None = None,
    ) -> BacktestResult:
        runner = BacktestRunner(
            prices=prices,
            backtests=backtests,
            performance=_NoopPerformance(),
            risk_calculator=self._risk,
            indicator_engine=self._indicator_engine,
            model=model,
            model_prob_buy=self._prob_buy,
            model_prob_sell=self._prob_sell,
            sectors=self._sectors,
        )
        oos_bars = [b for bars in fold.oos.values() for b in bars]
        if oos_bars:
            fold_start = min(b.ts.date() for b in oos_bars)
            fold_end = max(b.ts.date() for b in oos_bars)
        else:
            fold_start = fold_end = fold.fold_start.date()
        run = BacktestRun(
            name=f"calendar-wf-{fold_start.isoformat()}",
            universe=list(fold.oos.keys()),
            start=fold_start,
            end=fold_end,
            initial_capital=Money(initial_capital),
            interval=interval,
            strategy=CALENDAR_STRATEGY_LABEL,
            commission_bps=Decimal(str(commission_bps)),
            slippage_bps=Decimal(str(slippage_bps)),
        )
        params = BacktestParams(
            interval=interval,
            strategy=CALENDAR_STRATEGY_LABEL,
            commission_bps=commission_bps,
            slippage_bps=slippage_bps,
            stop_loss_pct=_cfg(exit_cfg, "stop_loss_pct", 0.03),
            take_profit_pct=_cfg(exit_cfg, "take_profit_pct", 0.06),
            max_hold_bars=int(_cfg(exit_cfg, "max_hold_bars", 0.0)),
            trailing_stop_pct=_cfg(exit_cfg, "trailing_stop_pct", 0.0),
        )
        # Bars outside the held-out calendar window get prob 0.5 -> always HOLD,
        # so they contribute feature history but never trade.
        probs = self.precompute_probs(model, fold)
        series = {
            symbol: self._indicator_engine.compute_series(bars, symbol, interval)
            for symbol, bars in fold.full.items()
            if bars
        }
        return runner._simulate(
            run, fold.full, initial_capital, params, model_outputs=probs, series=series
        )

    # ------------------------------------------------------------- aggregate

    @staticmethod
    def chain_curve(
        curve: list[tuple[datetime, Decimal]],
        fold_curve: list[tuple[datetime, Decimal]],
        equity: Decimal,
    ) -> tuple[list[tuple[datetime, Decimal]], Decimal]:
        """Append a fold's mark-to-market curve, compounding across folds."""
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


class _NoopPerformance(PerformanceRepository):
    async def upsert(self, summary: PerformanceSummary) -> PerformanceSummary:
        return summary

    async def latest_for_strategy(
        self, strategy: str, mode: TradingMode
    ) -> PerformanceSummary | None:
        return None


def _cfg(cfg: dict[str, float] | None, key: str, default: float) -> float:
    return cfg.get(key, default) if cfg else default
