"""Phase 6 — AI failure monitor (fail-safe, never self-authorizes a trade)."""

from __future__ import annotations

from datetime import UTC, datetime

from qtrader.application.ai.failure import AiFailureMonitor, FailureConfig
from qtrader.application.ai.models import (
    AgentSignal,
    AgentSignalSet,
    NewsAssessment,
    RegimeAssessment,
)
from qtrader.application.services.market_regime import MarketRegime
from qtrader.domain.value_objects import Interval

_TS = datetime(2025, 1, 1, tzinfo=UTC)


def _signal_set(*signals: AgentSignal) -> AgentSignalSet:
    return AgentSignalSet(asset="AAPL", as_of=_TS, signals=signals)


def _signal(score: float, confidence: float = 0.5) -> AgentSignal:
    return AgentSignal("technical", "1", score, confidence, "r", _TS)


def test_healthy_report_is_ok() -> None:
    monitor = AiFailureMonitor()
    report = monitor.check(_signal_set(_signal(0.5, 0.5), _signal(0.4, 0.5)))
    assert report.ok is True
    assert report.codes() == ()


def test_data_quality_warning_when_nothing_available() -> None:
    monitor = AiFailureMonitor()
    report = monitor.check()
    assert report.codes() == ("data_quality",)
    assert report.ok is True  # single warning is not degraded


def test_agent_disagreement_warning() -> None:
    monitor = AiFailureMonitor(FailureConfig(max_agent_dispersion=0.5))
    report = monitor.check(
        _signal_set(_signal(0.9, 0.5), _signal(-0.9, 0.5))
    )
    assert "agent_disagreement" in report.codes()
    assert report.ok is True


def test_overconfidence_warning() -> None:
    config = FailureConfig(max_mean_confidence=0.5)
    monitor = AiFailureMonitor(config)
    report = monitor.check(_signal_set(_signal(0.5, 0.9)))
    assert "overconfidence" in report.codes()


def test_instability_warning() -> None:
    config = FailureConfig(max_confidence_std=0.1)
    monitor = AiFailureMonitor(config)
    report = monitor.check(
        _signal_set(_signal(0.5, 0.1), _signal(0.5, 0.9))
    )
    assert "instability" in report.codes()


def test_news_staleness_warning() -> None:
    monitor = AiFailureMonitor()
    news = NewsAssessment(
        asset="AAPL",
        timestamp=_TS,
        sentiment=0.0,
        confidence=0.0,
        sources=(),
        relevance=0.0,
        aggregated_sentiment=0.0,
        items_used=0,
        model="lexicon",
    )
    report = monitor.check(news=news)
    assert "news_staleness" in report.codes()


def test_drift_warning() -> None:
    monitor = AiFailureMonitor()
    for value in (0.1, 0.1, 0.1):
        monitor.observe(ensemble_score=value, ts=_TS)
    monitor.observe(ensemble_score=0.9, ts=_TS)
    report = monitor.check()
    assert "drift" in report.codes()


def test_three_warnings_degrades() -> None:
    monitor = AiFailureMonitor(
        FailureConfig(
            max_mean_confidence=0.4,
            max_agent_dispersion=0.5,
            max_confidence_std=0.1,
        )
    )
    report = monitor.check(
        _signal_set(_signal(0.9, 0.9), _signal(-0.9, 0.1))
    )
    assert report.degraded is True
    assert report.reason


def test_regime_present_avoids_data_quality_warning() -> None:
    monitor = AiFailureMonitor()
    regime = RegimeAssessment(
        ts=_TS,
        regime=MarketRegime.BULL,
        confidence=0.8,
        volatility=None,
        trend="bull",
        timeframe=Interval.D1,
    )
    report = monitor.check(regime=regime)
    assert "data_quality" not in report.codes()
