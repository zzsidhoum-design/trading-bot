"""Unit tests for the numpy-only model trainer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import exp, isfinite
from random import Random

import pytest

from qtrader.application.services.model_trainer import (
    ModelTrainer,
    fit_platt_calibration,
    split_calibration_samples,
)
from qtrader.domain.ports import ModelRepository, PriceRepository
from qtrader.domain.value_objects import Interval, PriceBar

BASE = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _bars(count: int = 300, step: float = 0.2, noisy: bool = False) -> list[PriceBar]:
    rng = Random(42)
    bars = []
    for i in range(count):
        close = 100.0 + rng.gauss(0.0, 0.8) if noisy else 100.0 + step * i
        bars.append(
            PriceBar(
                symbol="AAPL",
                interval=Interval.M5,
                ts=BASE - timedelta(minutes=5 * (count - 1 - i)),
                open=Decimal(str(round(close - 0.1, 4))),
                high=Decimal(str(round(close + 0.5, 4))),
                low=Decimal(str(round(close - 0.5, 4))),
                close=Decimal(str(round(close, 4))),
                volume=Decimal("1000000"),
            )
        )
    bars.sort(key=lambda b: b.ts)
    return bars


class FakePriceRepository(PriceRepository):
    def __init__(self, data: dict[str, list[PriceBar]]) -> None:
        self._data = data

    async def upsert_bars(self, bars) -> int:
        return len(bars)

    async def latest(self, symbol, interval) -> PriceBar | None:
        series = self._data.get(symbol, [])
        return series[-1] if series else None

    async def history(self, symbol, interval, start=None, end=None, limit=500) -> list[PriceBar]:
        return self._data.get(symbol, [])[:limit]


class FakeModelRepository(ModelRepository):
    def __init__(self) -> None:
        self.versions: list[dict] = []
        self.promotions: list[tuple[str, int]] = []

    async def load_active(self, name: str):
        return None

    async def create_version(self, name, hyperparams, training_window, offline_metrics) -> int:
        self.versions.append(
            {
                "name": name,
                "hyperparams": hyperparams,
                "training_window": training_window,
                "offline_metrics": offline_metrics,
            }
        )
        return len(self.versions)

    async def promote(self, name: str, version: int) -> None:
        self.promotions.append((name, version))


@pytest.mark.asyncio
async def test_train_fits_registers_and_promotes_on_trending_data() -> None:
    model_repo = FakeModelRepository()
    trainer = ModelTrainer(
        prices=FakePriceRepository({"AAPL": _bars()}),
        model_repo=model_repo,
        model_name="momentum",
    )
    result = await trainer.train(
        ["AAPL"],
        Interval.M5,
        horizon_bars=12,
        lookback_bars=120,
        min_samples=100,
        promote_threshold=0.52,
    )
    assert result is not None
    assert result.name == "momentum"
    assert result.version == 1
    assert result.metrics["samples"] >= 80
    assert result.metrics["accuracy"] >= 0.5
    assert result.promoted is True

    assert len(model_repo.versions) == 1
    stored = model_repo.versions[0]
    assert stored["hyperparams"]["feature_names"]
    assert len(stored["hyperparams"]["coef"]) == len(stored["hyperparams"]["feature_names"])
    assert isfinite(stored["hyperparams"]["calib_a"])
    assert isfinite(stored["hyperparams"]["calib_b"])
    assert result.metrics["calibration_samples"] > 0
    assert model_repo.promotions == [("momentum", 1)]


@pytest.mark.asyncio
async def test_train_insufficient_samples_returns_none() -> None:
    model_repo = FakeModelRepository()
    trainer = ModelTrainer(
        prices=FakePriceRepository({"AAPL": _bars(count=140)}),
        model_repo=model_repo,
    )
    result = await trainer.train(
        ["AAPL"],
        Interval.M5,
        horizon_bars=12,
        lookback_bars=120,
        min_samples=100,
    )
    assert result is None
    assert model_repo.versions == []


@pytest.mark.asyncio
async def test_train_below_threshold_does_not_promote() -> None:
    model_repo = FakeModelRepository()
    trainer = ModelTrainer(
        prices=FakePriceRepository({"AAPL": _bars(noisy=True)}),
        model_repo=model_repo,
    )
    result = await trainer.train(
        ["AAPL"],
        Interval.M5,
        horizon_bars=12,
        lookback_bars=120,
        min_samples=100,
        promote_threshold=0.95,
    )
    assert result is not None
    assert result.metrics["accuracy"] < 0.95
    assert result.promoted is False
    assert model_repo.promotions == []


def test_fit_platt_calibration_corrects_overconfidence() -> None:
    rng = Random(7)
    z = [rng.gauss(0.0, 1.0) for _ in range(4000)]
    p = [1.0 / (1.0 + exp(-zi)) for zi in z]
    y = [1 if rng.random() < pi else 0 for pi in p]
    # Model is overconfident: raw logits are 2*z while truth is sigmoid(z).
    a, b = fit_platt_calibration([2.0 * zi for zi in z], y)
    assert a == pytest.approx(0.5, abs=0.08)
    assert b == pytest.approx(0.0, abs=0.08)


def test_fit_platt_calibration_empty_returns_none() -> None:
    assert fit_platt_calibration([], []) is None


def test_split_calibration_samples_is_time_ordered() -> None:
    per_symbol = [
        [(10, [1.0], 0), (11, [1.0], 1), (12, [1.0], 0), (13, [1.0], 1), (14, [1.0], 1)],
        [],
    ]
    fit_x, fit_y, cal_x, cal_y = split_calibration_samples(per_symbol, calib_frac=0.2)
    # lo=10, hi=14, cutoff = 10 + 0.8*4 = 13.2 -> fit {10..13}, cal {14}
    assert fit_y == [0, 1, 0, 1]
    assert cal_y == [1]
    assert len(fit_x) == 4
    assert len(cal_x) == 1
