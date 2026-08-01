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
        x_rows: list[list[float]] = []
        labels: list[int] = []
        # history() returns the first N ascending bars, so fetch enough to build
        # up to `min_samples` labeled windows.
        fetch_limit = lookback_bars + horizon_bars + min_samples
        for symbol in symbols:
            bars = await self._prices.history(symbol, interval, limit=fetch_limit)
            if len(bars) < lookback_bars + horizon_bars:
                continue
            for i in range(lookback_bars, len(bars) - horizon_bars):
                window = bars[i - lookback_bars : i]
                feats = price_features_from_bars(window)
                entry = float(bars[i].close)
                exit_ = float(bars[i + horizon_bars].close)
                forward = (exit_ - entry) / entry if entry else 0.0
                x_rows.append([feats.get(name, 0.0) for name in feature_names])
                labels.append(1 if forward > 0 else 0)

        if len(x_rows) < min_samples:
            return None

        x = np.asarray(x_rows, dtype=float)
        y = np.asarray(labels, dtype=float)
        n, dim = x.shape
        if dim == 0:
            return None

        mean = x.mean(axis=0)
        std = x.std(axis=0)
        denom = std.copy()
        denom[denom < 1e-9] = 1.0
        x_std = (x - mean) / denom

        weights: np.ndarray = np.zeros(dim)
        bias = 0.0
        for _ in range(_EPOCHS):
            z = _clip_logit(x_std @ weights + bias)
            probs = 1.0 / (1.0 + np.exp(-z))
            grad = x_std.T @ (probs - y) / n + _L2 * weights
            grad_bias = float((probs - y).mean())
            weights -= _LEARNING_RATE * grad
            bias -= _LEARNING_RATE * grad_bias

        predictions = (1.0 / (1.0 + np.exp(-_clip_logit(x_std @ weights + bias))) > 0.5).astype(
            int
        )
        accuracy = float((predictions == y).mean())

        metrics = {
            "accuracy": round(accuracy, 4),
            "samples": n,
            "positive_rate": round(float(y.mean()), 4),
            "features": dim,
        }
        hyperparams: dict[str, Any] = {
            "feature_names": feature_names,
            "coef": [float(c) for c in weights],
            "intercept": float(bias),
            "mean": [float(m) for m in mean],
            "std": [float(s) for s in denom],
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
