"""Unit tests for heuristic & logistic prediction models."""

from __future__ import annotations

import pytest

from qtrader.application.services.prediction_model import (
    HeuristicModel,
    LogisticModel,
)
from qtrader.domain.entities import RegisteredModel


def test_heuristic_always_bounded() -> None:
    model = HeuristicModel()
    for features in ({}, {"ret_5": 5.0}, {"ret_5": -5.0}, {"rsi": 90.0}):
        out = model.predict(features)
        assert 0.0 <= out.prob_up <= 1.0
        assert 0.0 <= out.prob_down <= 1.0
        assert 0.0 <= out.confidence <= 1.0
        assert pytest.approx(out.prob_up + out.prob_down) == 1.0


def test_heuristic_up_momentum_beats_down_momentum() -> None:
    model = HeuristicModel()
    up = model.predict({"ret_5": 2.0, "momentum_20": 2.0, "macd_hist": 0.5})
    down = model.predict({"ret_5": -2.0, "momentum_20": -2.0, "macd_hist": -0.5})
    assert up.prob_up > down.prob_up
    assert up.expected_return > down.expected_return


def test_logistic_reconstructed_from_registered_hyperparams() -> None:
    registered = RegisteredModel(
        name="momentum",
        version=3,
        hyperparams={
            "feature_names": ["ret_5", "momentum_20"],
            "coef": [1.0, 0.5],
            "intercept": -0.2,
            "mean": [0.0, 0.0],
            "std": [1.0, 1.0],
        },
        is_active=True,
    )
    model = LogisticModel.from_registered(registered)
    assert model is not None
    out = model.predict({"ret_5": 1.0, "momentum_20": 1.0})
    assert 0.0 <= out.prob_up <= 1.0
    assert pytest.approx(out.prob_up + out.prob_down) == 1.0


def test_logistic_monotone_in_positive_feature() -> None:
    registered = RegisteredModel(
        name="momentum",
        version=1,
        hyperparams={
            "feature_names": ["momentum_20"],
            "coef": [2.0],
            "intercept": 0.0,
        },
    )
    model = LogisticModel.from_registered(registered)
    assert model is not None
    low = model.predict({"momentum_20": -1.0})
    high = model.predict({"momentum_20": 1.0})
    assert high.prob_up > low.prob_up


def test_logistic_missing_hyperparams_returns_none() -> None:
    registered = RegisteredModel(name="momentum", version=1, hyperparams={})
    assert LogisticModel.from_registered(registered) is None


def test_logistic_from_registered_roundtrips_calibration() -> None:
    registered = RegisteredModel(
        name="momentum",
        version=2,
        hyperparams={
            "feature_names": ["ret_5"],
            "coef": [1.0],
            "intercept": 0.0,
            "calib_a": 0.7,
            "calib_b": -0.2,
        },
    )
    model = LogisticModel.from_registered(registered)
    assert model is not None
    assert model._calib_a == pytest.approx(0.7)
    assert model._calib_b == pytest.approx(-0.2)


def test_logistic_predict_applies_calibration() -> None:
    raw = LogisticModel(feature_names=["ret_5"], coef=[2.0], intercept=0.0)
    calibrated = LogisticModel(
        feature_names=["ret_5"], coef=[2.0], intercept=0.0, calib_a=0.5, calib_b=0.0
    )
    out_raw = raw.predict({"ret_5": 2.0})
    out_cal = calibrated.predict({"ret_5": 2.0})
    assert out_raw.prob_up > out_cal.prob_up > 0.5
    assert 0.0 <= out_cal.prob_up <= 1.0
    assert pytest.approx(out_cal.prob_up + out_cal.prob_down) == 1.0
