"""Phase 6 — Market Regime Agent (regime + confidence + volatility + timeframe)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from qtrader.application.ai.regime import MarketRegimeAgent
from qtrader.application.services.market_regime import MarketRegime, VolatilityRegime
from qtrader.domain.value_objects import Interval
from tests.unit.fakes_ai import (
    falling_closes,
    make_price_bars,
    rising_closes,
    sideways_closes,
)


def _closes(series: list[float]) -> list[tuple[datetime, float]]:
    return [
        (datetime(2023, 1, 1, tzinfo=UTC) + timedelta(days=i), c)
        for i, c in enumerate(series)
    ]


def test_assess_returns_bull_for_rising_series() -> None:
    agent = MarketRegimeAgent()
    result = agent.assess(_closes(rising_closes(300)))
    assert result is not None
    assert result.regime is MarketRegime.BULL
    assert result.timeframe is Interval.D1


def test_assess_returns_bear_for_falling_series() -> None:
    agent = MarketRegimeAgent()
    result = agent.assess(_closes(falling_closes(300)))
    assert result is not None
    assert result.regime is MarketRegime.BEAR


def test_assess_returns_sideways_for_flat_series() -> None:
    agent = MarketRegimeAgent()
    result = agent.assess(_closes(sideways_closes(300)))
    assert result is not None
    assert result.regime is MarketRegime.SIDEWAYS


def test_confidence_is_bounded_and_volatility_reported() -> None:
    agent = MarketRegimeAgent()
    result = agent.assess(_closes(rising_closes(300)))
    assert result is not None
    assert 0.0 <= result.confidence <= 1.0
    assert result.volatility in (
        VolatilityRegime.LOW,
        VolatilityRegime.HIGH,
        VolatilityRegime.EXTREME,
        None,
    )
    assert result.trend == "bull"


def test_assess_returns_none_for_empty_or_too_short_history() -> None:
    agent = MarketRegimeAgent()
    assert agent.assess([]) is None
    short = _closes(rising_closes(10))
    assert agent.assess(short) is None


def test_assess_honours_custom_timeframe() -> None:
    agent = MarketRegimeAgent(timeframe=Interval.M5)
    result = agent.assess(_closes(rising_closes(300)), timeframe=Interval.H1)
    assert result is not None
    assert result.timeframe is Interval.H1


def test_from_bars_uses_closes() -> None:
    agent = MarketRegimeAgent()
    bars = make_price_bars("SPY", rising_closes(300))
    result = agent.from_bars(bars)
    assert result is not None
    assert result.regime is MarketRegime.BULL


def test_as_of_is_respected() -> None:
    agent = MarketRegimeAgent()
    now = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    result = agent.assess(_closes(rising_closes(300)), as_of=now)
    assert result is not None
    assert result.ts == now
