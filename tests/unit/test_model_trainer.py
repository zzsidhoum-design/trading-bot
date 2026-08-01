"""Unit tests for the numpy-only model trainer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from random import Random

import pytest

from qtrader.application.services.model_trainer import ModelTrainer
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
    assert result.metrics["samples"] >= 100
    assert result.metrics["accuracy"] >= 0.5
    assert result.promoted is True

    assert len(model_repo.versions) == 1
    stored = model_repo.versions[0]
    assert stored["hyperparams"]["feature_names"]
    assert len(stored["hyperparams"]["coef"]) == len(stored["hyperparams"]["feature_names"])
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
