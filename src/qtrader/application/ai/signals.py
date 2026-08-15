"""Agent signal collection, weighting and ensemble aggregation.

Every decision input flows through :class:`AgentSignalProvider`, which pulls the
latest persisted signal from each enabled agent (technical/news/fundamental/
pattern/prediction) plus the live regime and news assessments, and applies the
**versioned** :class:`AgentWeightsConfig`. Weights are auditable configuration,
never hard-coded here. Missing signals are simply absent — the ensemble never
fabricates one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from qtrader.application.ai.models import (
    AgentSignal,
    AgentSignalSet,
    AgentWeightsConfig,
    NewsAssessment,
)
from qtrader.application.ai.regime import MarketRegimeAgent
from qtrader.application.ai.sentiment import NewsSentimentPipeline
from qtrader.domain.entities import Prediction, Signal
from qtrader.domain.ports import PredictionRepository, SignalRepository

# Agent -> class used to look up the persisted signal for that agent.
AGENT_SIGNAL_SOURCES: tuple[str, ...] = (
    "technical",
    "news",
    "fundamental",
    "pattern",
)

SIGNAL_CONFIDENCE_DEFAULT = 0.5
PREDICTION_AGENT = "prediction"


@runtime_checkable
class AgentSignalSource(Protocol):
    """Anything that can yield the latest signal for one agent/symbol."""

    async def latest(self, symbol: str) -> AgentSignal | None: ...


def _signal_score(signal: Signal) -> float:
    return float(signal.score)


def _signal_reason(signal: Signal) -> str:
    bits = [signal.signal_type.value, f"score={float(signal.score):+.3f}"]
    if signal.horizon:
        bits.append(f"horizon={signal.horizon}")
    return " ".join(bits)


def _signal_features(signal: Signal) -> dict[str, float]:
    features: dict[str, float] = {}
    sub = signal.metadata.get("sub_scores") if isinstance(signal.metadata, dict) else None
    if isinstance(sub, dict):
        for key, value in sub.items():
            if isinstance(value, (int, float)):
                features[f"sub_scores.{key}"] = float(value)
    return features


def _prediction_signal(prediction: Prediction) -> AgentSignal:
    prob_up = float(prediction.prob_up or 0.0)
    prob_down = float(prediction.prob_down or 0.0)
    score = prob_up - prob_down
    return AgentSignal(
        agent=PREDICTION_AGENT,
        version=f"model-{prediction.model_name}-v{prediction.model_version}",
        score=score,
        confidence=float(prediction.confidence or 0.0),
        reason=(
            f"model={prediction.model_name} v{prediction.model_version} "
            f"exp_ret={float(prediction.expected_return or 0.0):+.4f} "
            f"exp_vol={float(prediction.expected_volatility or 0.0):.4f}"
        ),
        timestamp=prediction.created_at,
        features={
            "prob_up": prob_up,
            "prob_down": prob_down,
            "expected_return": float(prediction.expected_return or 0.0),
            "expected_volatility": float(prediction.expected_volatility or 0.0),
        },
    )


class AgentSignalProvider:
    """Collects the latest signals and regime/news context for one symbol."""

    def __init__(
        self,
        signals: SignalRepository,
        predictions: PredictionRepository,
        regime_agent: MarketRegimeAgent,
        news_pipeline: NewsSentimentPipeline | None = None,
    ) -> None:
        self._signals = signals
        self._predictions = predictions
        self._regime = regime_agent
        self._news = news_pipeline

    async def collect(
        self,
        symbol: str,
        *,
        as_of: datetime | None = None,
        closes: list[tuple[datetime, float]] | None = None,
    ) -> AgentSignalSet:
        """Latest signals for ``symbol`` as of ``as_of`` (defaults to now)."""
        as_of = as_of or datetime.now(UTC)
        collected: list[AgentSignal] = []

        for agent in AGENT_SIGNAL_SOURCES:
            latest = await self._signals.latest_for_symbol(symbol, agent)
            if not latest:
                continue
            signal = latest[0]
            collected.append(
                AgentSignal(
                    agent=agent,
                    version="latest",
                    score=_signal_score(signal),
                    confidence=SIGNAL_CONFIDENCE_DEFAULT,
                    reason=_signal_reason(signal),
                    timestamp=signal.created_at,
                    features=_signal_features(signal),
                )
            )

        predictions = await self._predictions.latest_for_symbol(symbol, limit=1)
        if predictions:
            collected.append(_prediction_signal(predictions[0]))

        regime = None
        if closes:
            regime = self._regime.assess(closes, as_of=as_of)

        news: NewsAssessment | None = None
        if self._news is not None:
            news = await self._news.assess(symbol, as_of=as_of)

        return AgentSignalSet(
            asset=symbol,
            as_of=as_of,
            signals=tuple(collected),
            regime=regime,
            news=news,
        )


class WeightedEnsemble:
    """Applies a versioned weight config to a signal set -> one ensemble score."""

    def __init__(self, config: AgentWeightsConfig) -> None:
        self._config = config

    @property
    def config(self) -> AgentWeightsConfig:
        return self._config

    def aggregate(
        self,
        signal_set: AgentSignalSet,
    ) -> tuple[float, dict[str, float], dict[str, float]]:
        """Return (ensemble score, weighted agent scores, raw agent scores).

        The ensemble score is a confidence-weighted blend of the per-agent
        scores. When the total weight is zero (no enabled agent produced a
        signal) the ensemble score is ``0.0`` — a neutral, do-nothing stance.
        """
        signals = signal_set.by_agent()
        weighted: dict[str, float] = {}
        raw: dict[str, float] = {}
        for agent in self._config.effective_agents():
            signal = signals.get(agent)
            if signal is None:
                continue
            w = self._config.weight(agent)
            c = max(0.0, min(1.0, signal.confidence))
            weighted[agent] = w * c * signal.score
            raw[agent] = signal.score
        if not weighted:
            return 0.0, {}, {}
        total = sum(
            self._config.weight(a) * max(0.0, min(1.0, signals[a].confidence))
            for a in weighted
        )
        if total == 0.0:
            return 0.0, {}, {}
        ensemble = sum(weighted.values()) / total
        return ensemble, weighted, raw


def parse_agent_weights(
    weights: dict[str, float],
    *,
    version: str,
    enabled: tuple[str, ...] | None = None,
) -> AgentWeightsConfig:
    """Validate and freeze a versioned weight config from raw settings."""
    return AgentWeightsConfig(version=version, weights=dict(weights), enabled=enabled or ())


__all__ = [
    "AGENT_SIGNAL_SOURCES",
    "AgentSignalProvider",
    "AgentSignalSource",
    "PREDICTION_AGENT",
    "WeightedEnsemble",
    "parse_agent_weights",
]
