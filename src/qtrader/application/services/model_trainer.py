"""Model trainer — fits a small logistic regression over labeled windows.

Labels are the sign of the forward ``horizon_bars`` return. The trained
coefficients + standardization statistics are persisted as hyperparams in the
model registry. A version is promoted to active only when its offline accuracy
meets the threshold (never auto-promoted without passing).

Pure numpy — no external ML dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from qtrader.application.services.feature_store import FEATURE_NAMES, price_features_from_bars
from qtrader.domain.ports import ModelRepository, PriceRepository
from qtrader.domain.value_objects import Interval

_LEARNING_RATE = 0.5
_EPOCHS = 200
_L2 = 1e-3


@dataclass(frozen=True, slots=True)
class TrainResult:
    name: str
    version: int
    metrics: dict[str, float]
    promoted: bool


def _clip_logit(value: np.ndarray) -> np.ndarray:
    return np.asarray(np.clip(value, -50.0, 50.0))


def fit_platt_calibration(
    logits: list[float] | np.ndarray,
    labels: list[int] | np.ndarray,
    *,
    learning_rate: float = 0.5,
    epochs: int = 500,
) -> tuple[float, float] | None:
    """Fit Platt scaling ``(a, b)`` mapping a raw logit ``z`` -> ``a*z + b``.

    Minimizes the logistic NLL of the calibration labels on a held-out set so
    ``sigmoid(a*z + b)`` becomes a calibrated probability. Returns ``None`` when
    there are no samples. Shared by the trainer and the walk-forward validator.
    """
    z = np.asarray(logits, dtype=float)
    y = np.asarray(labels, dtype=float)
    if z.size == 0:
        return None
    a, b = 1.0, 0.0
    for _ in range(epochs):
        p = 1.0 / (1.0 + np.exp(-_clip_logit(a * z + b)))
        a -= learning_rate * (z * (p - y)).mean()
        b -= learning_rate * (p - y).mean()
    return float(np.clip(a, -5.0, 5.0)), float(np.clip(b, -5.0, 5.0))


def split_calibration_samples(
    per_symbol: list[list[tuple[int, list[float], int]]], calib_frac: float = 0.2
) -> tuple[list[list[float]], list[int], list[list[float]], list[int]]:
    """Split time-ordered ``(bar_index, row, label)`` samples into fit/calibration.

    The last ``calib_frac`` of each symbol's decision-bar range is held out for
    calibration so the base model is never calibrated on its own fit labels.
    Returns ``(fit_x, fit_y, cal_x, cal_y)``.
    """
    fit_x: list[list[float]] = []
    fit_y: list[int] = []
    cal_x: list[list[float]] = []
    cal_y: list[int] = []
    for sym_samples in per_symbol:
        if not sym_samples:
            continue
        lo = min(i for i, _, _ in sym_samples)
        hi = max(i for i, _, _ in sym_samples)
        cutoff = lo + (1 - calib_frac) * (hi - lo)
        for i, row, label in sym_samples:
            if i >= cutoff:
                cal_x.append(row)
                cal_y.append(label)
            else:
                fit_x.append(row)
                fit_y.append(label)
    return fit_x, fit_y, cal_x, cal_y


def logits_for_fit(x_rows: list[list[float]], fit: dict[str, Any]) -> list[float]:
    """Standardized raw logits (pre-sigmoid) for rows under a fitted model dict."""
    x = np.asarray(x_rows, dtype=float)
    mean = np.asarray(fit["mean"], dtype=float)
    std = np.asarray(fit["std"], dtype=float)
    denom = std.copy()
    denom[denom < 1e-9] = 1.0
    x_std = (x - mean) / denom
    z = x_std @ np.asarray(fit["coef"], dtype=float) + float(fit["intercept"])
    return [float(v) for v in _clip_logit(z)]


def fit_logistic(
    x_rows: list[list[float]],
    labels: list[int],
    *,
    learning_rate: float = _LEARNING_RATE,
    epochs: int = _EPOCHS,
    l2: float = _L2,
) -> dict[str, Any] | None:
    """Fit a standardized logistic regression over labeled windows.

    Pure numpy, no I/O. Returns hyperparams (coef, intercept, mean, std,
    feature_names + offline accuracy) or ``None`` when there are no rows.
    Shared by ``ModelTrainer`` and the walk-forward validator so both train
    through the exact same fitting procedure.
    """
    if not x_rows:
        return None
    x = np.asarray(x_rows, dtype=float)
    y = np.asarray(labels, dtype=float)
    n, dim = x.shape
    if n == 0 or dim == 0:
        return None

    mean = x.mean(axis=0)
    std = x.std(axis=0)
    denom = std.copy()
    denom[denom < 1e-9] = 1.0
    x_std = (x - mean) / denom

    weights: np.ndarray = np.zeros(dim)
    bias = 0.0
    for _ in range(epochs):
        z = _clip_logit(x_std @ weights + bias)
        probs = 1.0 / (1.0 + np.exp(-z))
        grad = x_std.T @ (probs - y) / n + l2 * weights
        grad_bias = float((probs - y).mean())
        weights -= learning_rate * grad
        bias -= learning_rate * grad_bias

    predictions = (1.0 / (1.0 + np.exp(-_clip_logit(x_std @ weights + bias))) > 0.5).astype(int)
    accuracy = float((predictions == y).mean())
    return {
        "feature_names": list(FEATURE_NAMES),
        "coef": [float(c) for c in weights],
        "intercept": float(bias),
        "mean": [float(m) for m in mean],
        "std": [float(s) for s in denom],
        "accuracy": round(accuracy, 4),
        "samples": n,
        "positive_rate": round(float(y.mean()), 4),
    }


class ModelTrainer:
    def __init__(
        self,
        prices: PriceRepository,
        model_repo: ModelRepository,
        model_name: str = "momentum",
    ) -> None:
        self._prices = prices
        self._model_repo = model_repo
        self._model_name = model_name

    async def train(
        self,
        symbols: list[str],
        interval: Interval,
        *,
        horizon_bars: int = 12,
        lookback_bars: int = 120,
        min_samples: int = 100,
        promote_threshold: float = 0.52,
    ) -> TrainResult | None:
        feature_names = list(FEATURE_NAMES)
        per_symbol: list[list[tuple[int, list[float], int]]] = []
        # history() returns the first N ascending bars, so fetch enough to build
        # up to `min_samples` labeled windows.
        fetch_limit = lookback_bars + horizon_bars + min_samples
        for symbol in symbols:
            bars = await self._prices.history(symbol, interval, limit=fetch_limit)
            if len(bars) < lookback_bars + horizon_bars:
                continue
            sym_samples: list[tuple[int, list[float], int]] = []
            for i in range(lookback_bars, len(bars) - horizon_bars):
                window = bars[i - lookback_bars : i]
                feats = price_features_from_bars(window)
                entry = float(bars[i].close)
                exit_ = float(bars[i + horizon_bars].close)
                forward = (exit_ - entry) / entry if entry else 0.0
                sym_samples.append(
                    (i, [feats.get(name, 0.0) for name in feature_names], 1 if forward > 0 else 0)
                )
            per_symbol.append(sym_samples)

        total_samples = sum(len(sym) for sym in per_symbol)
        if total_samples < min_samples:
            return None

        fit_x, fit_y, cal_x, cal_y = split_calibration_samples(per_symbol)
        fit = fit_logistic(
            fit_x,
            fit_y,
            learning_rate=_LEARNING_RATE,
            epochs=_EPOCHS,
            l2=_L2,
        )
        if fit is None:
            return None
        calib_a, calib_b = 1.0, 0.0
        if cal_x:
            cal = fit_platt_calibration(logits_for_fit(cal_x, fit), cal_y)
            if cal is not None:
                calib_a, calib_b = cal
        accuracy = fit["accuracy"]
        metrics = {
            "accuracy": accuracy,
            "samples": fit["samples"],
            "positive_rate": fit["positive_rate"],
            "features": len(feature_names),
            "calibration_samples": len(cal_x),
        }
        hyperparams: dict[str, Any] = {
            "feature_names": fit["feature_names"],
            "coef": fit["coef"],
            "intercept": fit["intercept"],
            "mean": fit["mean"],
            "std": fit["std"],
            "calib_a": calib_a,
            "calib_b": calib_b,
        }
        version = await self._model_repo.create_version(
            name=self._model_name,
            hyperparams=hyperparams,
            training_window=f"{lookback_bars}x{horizon_bars}",
            offline_metrics=metrics,
        )
        promoted = accuracy >= promote_threshold
        if promoted:
            await self._model_repo.promote(self._model_name, version)
        return TrainResult(
            name=self._model_name,
            version=version,
            metrics=metrics,
            promoted=promoted,
        )
