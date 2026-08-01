"""Prediction models — the active model is loaded from the registry, else a
deterministic heuristic fallback. Both implement ``predict(features) -> ModelOutput``.

``LogisticModel`` is reconstructed from ``model_registry.hyperparams`` written by
the ``ModelTrainer``; the heuristic is pure and needs no registry entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import exp, tanh
from typing import Protocol

from qtrader.domain.entities import RegisteredModel

SIGMOID_GAIN = 2.2
SIGNAL_SCALE = 0.01


@dataclass(frozen=True, slots=True)
class ModelOutput:
    prob_up: float
    prob_down: float
    prob_trend: float
    confidence: float
    expected_return: float
    expected_volatility: float


class PredictionModel(Protocol):
    def predict(self, features: dict[str, float]) -> ModelOutput: ...


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + exp(-value))
    e = exp(value)
    return e / (1.0 + e)


class HeuristicModel:
    """Deterministic momentum/technical heuristic — the fallback when no trained
    model is registered. Never crashes, always returns bounded probabilities."""

    def predict(self, features: dict[str, float]) -> ModelOutput:
        ret_5 = features.get("ret_5", 0.0)
        ret_20 = features.get("ret_20", 0.0)
        macd_hist = features.get("macd_hist", 0.0)
        momentum = features.get("momentum_20", 0.0)
        rsi = features.get("rsi", 50.0)
        vol = features.get("vol_20", 0.02) or 0.02

        signal = (
            0.30 * tanh(ret_5 * 5.0)
            + 0.25 * tanh(ret_20 * 2.0)
            + 0.20 * tanh(macd_hist * 20.0)
            + 0.15 * tanh(momentum * 2.0)
            + 0.10 * (50.0 - rsi) / 50.0
        )
        prob_up = _sigmoid(SIGMOID_GAIN * signal)
        confidence = max(0.0, min(1.0, 0.35 + 0.65 * abs(signal)))
        return ModelOutput(
            prob_up=round(prob_up, 4),
            prob_down=round(1.0 - prob_up, 4),
            prob_trend=round(1.0 - abs(signal) * 0.5, 4),
            confidence=round(confidence, 4),
            expected_return=round(signal * SIGNAL_SCALE, 6),
            expected_volatility=round(vol, 6),
        )


class LogisticModel:
    """Trained logistic regression reconstructed from stored hyperparams."""

    def __init__(
        self,
        feature_names: list[str],
        coef: list[float],
        intercept: float,
        mean: list[float] | None = None,
        std: list[float] | None = None,
    ) -> None:
        self._names = feature_names
        self._coef = coef
        self._intercept = intercept
        self._mean = mean or []
        self._std = std or []

    @classmethod
    def from_registered(cls, model: RegisteredModel) -> LogisticModel | None:
        hp = model.hyperparams or {}
        names = hp.get("feature_names")
        coef = hp.get("coef")
        if not names or not coef:
            return None
        return cls(
            feature_names=[str(n) for n in names],
            coef=[float(c) for c in coef],
            intercept=float(hp.get("intercept", 0.0)),
            mean=[float(m) for m in (hp.get("mean") or [])],
            std=[float(s) for s in (hp.get("std") or [])],
        )

    def predict(self, features: dict[str, float]) -> ModelOutput:
        vector: list[float] = []
        for i, name in enumerate(self._names):
            value = features.get(name, 0.0)
            if self._mean and self._std and i < len(self._std):
                denom = self._std[i] if self._std[i] else 1.0
                value = (value - self._mean[i]) / denom
            vector.append(value)
        logit = self._intercept + sum(c * x for c, x in zip(self._coef, vector, strict=True))
        prob_up = _sigmoid(logit)
        vol = features.get("vol_20", 0.02) or 0.02
        expected_return = (2.0 * prob_up - 1.0) * vol * 0.5
        confidence = max(0.0, min(1.0, 0.4 + 1.2 * abs(prob_up - 0.5)))
        return ModelOutput(
            prob_up=round(prob_up, 4),
            prob_down=round(1.0 - prob_up, 4),
            prob_trend=round(1.0 - abs(prob_up - 0.5), 4),
            confidence=round(confidence, 4),
            expected_return=round(expected_return, 6),
            expected_volatility=round(vol, 6),
        )
