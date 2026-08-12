"""Strategy evaluator — rule spec -> per-(symbol, bar) ``prob_up`` series.

The evaluator builds a per-symbol feature frame (production ``IndicatorSnapshot``
columns plus price-derived features) and evaluates a :class:`StrategySpec`'s
entry/exit/regime rules vectorized per bar. Output follows the backtest engine's
``model_outputs`` contract: ``EVENT_BUY`` (0.9) when entry fires, ``EVENT_SELL``
(0.1) when exit fires, ``HOLD`` (0.5) otherwise. Bars before the warm-up window
always HOLD so indicators have converged.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pandas as pd

from qtrader.application.research.strategy.feature_library import (
    SNAPSHOT_FEATURES,
)
from qtrader.application.research.strategy.specs import (
    Condition,
    Operator,
    StrategySpec,
)
from qtrader.application.services.indicators import frame_from_bars
from qtrader.application.services.strategies.base import EVENT_BUY, EVENT_SELL, HOLD
from qtrader.domain.entities import IndicatorSnapshot
from qtrader.domain.value_objects import PriceBar

_BAR_EPSILON = 1e-12


def _float_or_nan(value: object) -> float:
    return float(value) if isinstance(value, (int, float, Decimal)) else float("nan")


class StrategyEvaluator:
    """Pure rule engine: strategy spec + bars + snapshots -> prob series."""

    def __init__(self, warmup_bars: int = 30) -> None:
        if warmup_bars < 0:
            raise ValueError("warmup_bars must be >= 0")
        self._warmup = warmup_bars

    def probs(
        self,
        spec: StrategySpec,
        bars_by_symbol: dict[str, list[PriceBar]],
        series_by_symbol: dict[str, list[IndicatorSnapshot]],
    ) -> dict[str, dict[datetime, float]]:
        """prob_up per OOS bar for every symbol (missing bars default HOLD)."""
        out: dict[str, dict[datetime, float]] = {}
        for symbol, bars in bars_by_symbol.items():
            if not bars:
                out[symbol] = {}
                continue
            series = series_by_symbol.get(symbol)
            frame = self._feature_frame(bars, series)
            probabilities = self._evaluate(spec, frame)
            out[symbol] = {
                bar.ts: probabilities[i] for i, bar in enumerate(bars)
            }
        return out

    def _evaluate(self, spec: StrategySpec, frame: pd.DataFrame) -> list[float]:
        entry_masks = [self._condition_mask(frame, c) for c in spec.entry.conditions]
        exit_masks = [self._condition_mask(frame, c) for c in spec.exit.conditions]
        regime_masks = [
            self._condition_mask(frame, c)
            for c in (spec.regime.conditions if spec.regime is not None else ())
        ]

        entry = self._combine(frame, entry_masks, spec.entry.logic, default=True)
        exit_ = self._combine(frame, exit_masks, spec.exit.logic, default=False)
        regime = self._combine(frame, regime_masks, "all", default=True)

        buy = (regime & entry).fillna(False)
        sell = exit_.fillna(False)
        probs = pd.Series(
            pd.array([HOLD] * len(frame), dtype="float64"),
            index=frame.index,
        )
        probs = probs.mask(buy, EVENT_BUY).mask(sell, EVENT_SELL)
        values = probs.to_numpy(dtype=float, copy=True)
        values[: min(self._warmup, len(values))] = HOLD
        return [float(v) for v in values]

    def _combine(
        self,
        frame: pd.DataFrame,
        masks: list[pd.Series],
        logic: str,
        *,
        default: bool,
    ) -> pd.Series:
        if not masks:
            return pd.Series([default] * len(frame), index=frame.index)
        combined = masks[0].fillna(False)
        for mask in masks[1:]:
            if logic == "all":
                combined = combined & mask.fillna(False)
            else:
                combined = combined | mask.fillna(False)
        return combined

    def _condition_mask(self, frame: pd.DataFrame, cond: Condition) -> pd.Series:
        left = frame[cond.feature]
        if cond.ref_feature is not None:
            right = frame[cond.ref_feature]
            valid = left.notna() & right.notna()
            if cond.op is Operator.CROSS_ABOVE:
                return valid & (left.shift(1) <= right.shift(1)) & (left > right)
            if cond.op is Operator.CROSS_BELOW:
                return valid & (left.shift(1) >= right.shift(1)) & (left < right)
            if cond.op is Operator.GT:
                return valid & (left > right)
            if cond.op is Operator.LT:
                return valid & (left < right)
            if cond.op is Operator.GE:
                return valid & (left >= right)
            if cond.op is Operator.LE:
                return valid & (left <= right)
            raise ValueError(f"unsupported operator {cond.op.value}")
        value = cond.value
        if cond.op is Operator.CROSS_ABOVE:
            return (left.shift(1) <= value + _BAR_EPSILON) & (left > value)
        if cond.op is Operator.CROSS_BELOW:
            return (left.shift(1) >= value - _BAR_EPSILON) & (left < value)
        if cond.op is Operator.GT:
            return left > value
        if cond.op is Operator.LT:
            return left < value
        if cond.op is Operator.GE:
            return left >= value
        if cond.op is Operator.LE:
            return left <= value
        raise ValueError(f"unsupported operator {cond.op.value}")

    def _feature_frame(
        self,
        bars: list[PriceBar],
        series: list[IndicatorSnapshot] | None,
    ) -> pd.DataFrame:
        df = frame_from_bars(bars)
        for name in SNAPSHOT_FEATURES:
            if series:
                df[name] = [_float_or_nan(getattr(snapshot, name)) for snapshot in series]
            else:
                df[name] = float("nan")

        closes = df["close"]
        df["ret_1"] = closes.pct_change()
        df["ret_5"] = closes.pct_change(5)
        df["ret_10"] = closes.pct_change(10)
        df["ret_20"] = closes.pct_change(20)
        df["ret_60"] = closes.pct_change(60)
        df["vol_20"] = df["ret_1"].rolling(20).std()
        df["atr_pct"] = df["atr"] / closes.replace(0.0, float("nan"))
        df["volume_ratio"] = df["volume"].rolling(5).mean() / df["volume"].rolling(20).mean()
        df["range_ratio"] = ((df["high"] - df["low"]) / closes).rolling(20).mean()
        low20 = df["low"].rolling(20).min()
        high20 = df["high"].rolling(20).max()
        span = (high20 - low20).replace(0.0, float("nan"))
        df["pos_in_range_20"] = (closes - low20) / span
        df["up_ratio_20"] = (closes > closes.shift(1)).rolling(20).mean()
        return df


__all__ = ["StrategyEvaluator", "_BAR_EPSILON"]
