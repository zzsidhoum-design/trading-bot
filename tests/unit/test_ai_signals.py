"""Phase 6 — Agent signal collection, weighted ensemble, weight parsing."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from qtrader.application.ai.models import (
    AgentSignal,
    AgentSignalSet,
    AgentWeightsConfig,
)
from qtrader.application.ai.regime import MarketRegimeAgent
from qtrader.application.ai.sentiment import NewsSentimentPipeline
from qtrader.application.ai.signals import (
    AgentSignalProvider,
    WeightedEnsemble,
    parse_agent_weights,
)
from qtrader.domain.entities import NewsItem, Prediction
from tests.unit.fakes_ai import (
    FakeNewsProvider,
    FakeNewsRepository,
    FakePredictionRepository,
    FakeSignalRepository,
    StubSentimentModel,
    make_signal,
    rising_closes,
)


def _ts(hours_ago: int = 1) -> datetime:
    from datetime import timedelta

    return datetime.now(UTC) - timedelta(hours=hours_ago)


class TestWeightedEnsemble:
    def _config(self) -> AgentWeightsConfig:
        return AgentWeightsConfig(
            version="1.0",
            weights={"technical": 1.0, "news": 1.0},
        )

    def test_aggregate_blends_confidence_weighted_scores(self) -> None:
        signals = AgentSignalSet(
            asset="AAPL",
            as_of=_ts(),
            signals=(
                AgentSignal("technical", "1", 0.8, 0.5, "up", _ts()),
                AgentSignal("news", "1", 0.4, 0.5, "ok", _ts()),
            ),
        )
        ensemble, weighted, raw = WeightedEnsemble(self._config()).aggregate(signals)
        assert raw == {"technical": 0.8, "news": 0.4}
        assert weighted["technical"] == pytest.approx(0.4)
        assert weighted["news"] == pytest.approx(0.2)
        assert ensemble == pytest.approx(0.6)

    def test_aggregate_returns_zero_when_nothing_present(self) -> None:
        signals = AgentSignalSet(asset="AAPL", as_of=_ts())
        ensemble, weighted, raw = WeightedEnsemble(self._config()).aggregate(signals)
        assert ensemble == 0.0
        assert weighted == {}
        assert raw == {}

    def test_aggregate_ignores_disabled_or_weightless_agents(self) -> None:
        config = AgentWeightsConfig(
            version="1.0",
            weights={"technical": 1.0, "news": 0.0},
            enabled=("technical", "news"),
        )
        signals = AgentSignalSet(
            asset="AAPL",
            as_of=_ts(),
            signals=(
                AgentSignal("technical", "1", 1.0, 1.0, "up", _ts()),
                AgentSignal("news", "1", -1.0, 1.0, "bad", _ts()),
            ),
        )
        ensemble, weighted, _ = WeightedEnsemble(config).aggregate(signals)
        assert weighted == {"technical": 1.0}
        assert ensemble == pytest.approx(1.0)


class TestAgentWeightsConfig:
    def test_rejects_unknown_agent(self) -> None:
        with pytest.raises(ValueError):
            AgentWeightsConfig(version="1", weights={"alien": 1.0})

    def test_rejects_negative_weights(self) -> None:
        with pytest.raises(ValueError):
            AgentWeightsConfig(version="1", weights={"technical": -0.5})

    def test_enabled_defaults_to_positive_weight_agents(self) -> None:
        cfg = AgentWeightsConfig(version="1", weights={"technical": 1.0, "news": 0.0})
        assert cfg.enabled == ("technical",)

    def test_effective_agents_excludes_zero_weight(self) -> None:
        cfg = AgentWeightsConfig(
            version="1",
            weights={"technical": 1.0, "news": 0.0},
            enabled=("technical", "news"),
        )
        assert cfg.effective_agents() == ("technical",)


def test_parse_agent_weights_round_trip() -> None:
    cfg = parse_agent_weights(
        {"technical": 1.0}, version="2.0", enabled=("technical",)
    )
    assert cfg.version == "2.0"
    assert cfg.weight("technical") == 1.0


class TestAgentSignalProvider:
    @pytest.mark.asyncio
    async def test_collect_gathers_signals_prediction_regime_and_news(self) -> None:
        signals = FakeSignalRepository()
        signals.rows.append(make_signal("AAPL", "technical", 0.7))
        predictions = FakePredictionRepository()
        predictions.rows.append(
            Prediction(
                symbol="AAPL",
                model_name="gbm",
                model_version=3, horizon="1d",
                prob_up=0.6,
                prob_down=0.2,
                confidence=0.8,
                expected_return=Decimal("0.01"),
                expected_volatility=Decimal("0.02"),
                created_at=_ts(),
            )
        )
        provider = AgentSignalProvider(
            signals,
            predictions,
            MarketRegimeAgent(),
            NewsSentimentPipeline(
                FakeNewsProvider(
                    [
                        NewsItem(
                            symbol="AAPL",
                            source="test",
                            title="AAPL beats expectations",
                            url="https://x.com/n1",
                            published_at=_ts(),
                        )
                    ]
                ),
                FakeNewsRepository(),
                StubSentimentModel(),
            ),
        )
        closes = [
            (datetime(2023, 1, 1, tzinfo=UTC), c)
            for c in rising_closes(300)
        ]
        result = await provider.collect("AAPL", closes=closes, as_of=_ts())
        assert result.asset == "AAPL"
        agents = {s.agent for s in result.signals}
        assert agents == {"technical", "prediction"}
        assert result.regime is not None
        assert result.news is not None

    @pytest.mark.asyncio
    async def test_collect_has_no_signals_when_repositories_empty(self) -> None:
        provider = AgentSignalProvider(
            FakeSignalRepository(),
            FakePredictionRepository(),
            MarketRegimeAgent(),
        )
        result = await provider.collect("AAPL", as_of=_ts())
        assert result.signals == ()
        assert result.regime is None
        assert result.news is None

    @pytest.mark.asyncio
    async def test_collect_uses_latest_signal_per_agent(self) -> None:
        signals = FakeSignalRepository()
        signals.rows.append(make_signal("AAPL", "technical", 0.3, created_at=_ts(4)))
        signals.rows.append(make_signal("AAPL", "technical", 0.9, created_at=_ts(1)))
        provider = AgentSignalProvider(
            signals, FakePredictionRepository(), MarketRegimeAgent()
        )
        result = await provider.collect("AAPL", as_of=_ts())
        assert result.by_agent()["technical"].score == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_prediction_signal_maps_probabilities_to_score() -> None:
    prediction = Prediction(
        symbol="AAPL",
        model_name="gbm",
        model_version=1,
        horizon="1d",
        prob_up=0.75,
        prob_down=0.1,
        confidence=0.6,
        created_at=_ts(),
    )
    signals = FakePredictionRepository()
    signals.rows.append(prediction)
    provider = AgentSignalProvider(
        FakeSignalRepository(), signals, MarketRegimeAgent()
    )
    result = await provider.collect("AAPL", as_of=_ts())
    from qtrader.application.ai.signals import PREDICTION_AGENT

    signal = result.by_agent()[PREDICTION_AGENT]
    assert signal.score == pytest.approx(0.65)
    assert signal.features["expected_return"] == 0.0
