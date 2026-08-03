"""Prediction Agent — probability-of-movement (docs/02-agents.md §6).

Builds a deterministic feature vector via the FeatureStore, runs the active
model from the registry (falling back to a heuristic when absent), persists a
``Prediction`` row and publishes ``PredictionGenerated``.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import ClassVar

from qtrader.application.agents.base import AgentBase, AgentContext
from qtrader.application.services.feature_store import FeatureStore
from qtrader.application.services.prediction_model import (
    HeuristicModel,
    LogisticModel,
    PredictionModel,
)
from qtrader.domain.entities import Prediction, RegisteredModel
from qtrader.domain.events import DomainEvent, PredictionGenerated, ScanCompleted
from qtrader.domain.ports import EventBus, ModelRepository, PredictionRepository
from qtrader.domain.value_objects import Interval

SCORE_QUANT = Decimal("0.0001")
RETURN_QUANT = Decimal("0.000001")


def _dec(value: float, quant: Decimal) -> Decimal:
    return Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP)


class PredictionAgent(AgentBase):
    name: ClassVar[str] = "prediction"
    consumes: ClassVar[tuple[type[DomainEvent], ...]] = (ScanCompleted,)
    produces: ClassVar[tuple[type[DomainEvent], ...]] = (PredictionGenerated,)

    def __init__(
        self,
        features: FeatureStore,
        models: ModelRepository,
        predictions: PredictionRepository,
        bus: EventBus,
        *,
        model_name: str = "momentum",
        horizon: str = "intraday",
        interval: Interval = Interval.M5,
        lookback_bars: int = 120,
        min_bars: int = 30,
    ) -> None:
        self._features = features
        self._models = models
        self._predictions = predictions
        self._bus = bus
        self._model_name = model_name
        self._horizon = horizon
        self._interval = interval
        self._lookback_bars = lookback_bars
        self._min_bars = min_bars

    async def predict_symbol(
        self, symbol: str, interval: Interval | None = None
    ) -> Prediction | None:
        interval = interval or self._interval
        vector = await self._features.build_features(
            symbol, interval, lookback_bars=self._lookback_bars, min_bars=self._min_bars
        )
        if vector is None:
            self._logger.warning("prediction.no_features", symbol=symbol, interval=interval)
            return None

        registered: RegisteredModel | None = await self._models.load_active(self._model_name)
        trained = LogisticModel.from_registered(registered) if registered else None
        model: PredictionModel
        model_name: str
        model_version: int
        if trained is not None and registered is not None:
            model = trained
            model_name = registered.name
            model_version = registered.version
        else:
            model = HeuristicModel()
            model_name = self._model_name
            model_version = 0

        output = model.predict(vector.features)
        prediction = Prediction(
            symbol=symbol,
            model_name=model_name,
            model_version=model_version,
            horizon=self._horizon,
            prob_up=_dec(output.prob_up, SCORE_QUANT),
            prob_down=_dec(output.prob_down, SCORE_QUANT),
            prob_trend=_dec(output.prob_trend, SCORE_QUANT),
            confidence=_dec(output.confidence, SCORE_QUANT),
            expected_return=_dec(output.expected_return, RETURN_QUANT),
            expected_volatility=_dec(output.expected_volatility, RETURN_QUANT),
            features_hash=vector.feature_hash,
        )
        await self._predictions.save(prediction)
        self._logger.info(
            "prediction.generated",
            symbol=symbol,
            model=model_name,
            version=model_version,
            prob_up=output.prob_up,
        )
        await self._bus.publish(
            PredictionGenerated(
                symbol=symbol,
                model_name=model_name,
                prob_up=output.prob_up,
                prob_down=output.prob_down,
                prob_trend=output.prob_trend,
                confidence=output.confidence,
                expected_return=output.expected_return,
            )
        )
        return prediction

    async def predict_candidates(self, symbols: list[str]) -> int:
        return await self.run_batch(
            symbols, self.predict_symbol, action="prediction.analyze_failed"
        )

    async def on_event(self, event: DomainEvent) -> None:
        if isinstance(event, ScanCompleted):
            await self.predict_candidates([c["symbol"] for c in event.candidates])

    async def run(self, ctx: AgentContext) -> None:
        await self.predict_symbol(ctx.symbol, ctx.interval)
