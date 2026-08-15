"""Market Regime Agent — regime, confidence, volatility and trend assessment.

Wraps the existing causal :class:`MarketRegimeEngine` and adds what the Phase 6
request requires and the engine does not provide: a **confidence** score, the
**trend condition**, the **volatility condition** and the **timeframe**. The
agent is a pure classifier — it never places, modifies or cancels any order.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime
from typing import ClassVar

import pandas as pd

from qtrader.application.ai.models import RegimeAssessment
from qtrader.application.services.market_regime import (
    MarketRegime,
    MarketRegimeEngine,
)
from qtrader.domain.value_objects import Interval, PriceBar


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _trend_confidence(market: MarketRegime, close: float, fast: float, slow: float) -> float:
    """Confidence from how decisively the trend is set (0.30..0.95)."""
    if market is MarketRegime.SIDEWAYS:
        spread = abs(fast - slow) / slow if slow else 0.0
        return _clip(0.95 - spread * 40.0, 0.30, 0.80)
    distance = abs(close / slow - 1.0) if slow else 0.0
    return _clip(0.45 + distance * 25.0, 0.45, 0.95)


def _volatility_confidence(percentile: float | None) -> float:
    """Confidence from how far the vol percentile sits from 50% (0.30..0.90)."""
    if percentile is None:
        return 0.30
    return _clip(abs(percentile - 50.0) / 50.0 + 0.35, 0.35, 0.90)


class MarketRegimeAgent:
    """Deterministic regime classifier with confidence/timeframe metadata."""

    name: ClassVar[str] = "regime"

    def __init__(
        self,
        engine: MarketRegimeEngine | None = None,
        *,
        timeframe: Interval = Interval.D1,
    ) -> None:
        self._engine = engine or MarketRegimeEngine()
        self._timeframe = timeframe

    def assess(
        self,
        closes: Sequence[tuple[datetime, float]],
        *,
        as_of: datetime | None = None,
        timeframe: Interval | None = None,
    ) -> RegimeAssessment | None:
        """Classify the reference close series; returns None when history is
        too short to form a regime label (fail-safe — no fabricated regime)."""
        if not closes:
            return None
        labels = self._engine.classify(closes)
        if not labels:
            return None

        last = labels[-1]
        if last.market is None:
            return None

        df = pd.DataFrame(
            {"close": [c for _, c in closes]},
            index=pd.DatetimeIndex([ts for ts, _ in closes]),
        )
        slow = self._engine.trend_period
        fast = self._engine.fast_trend_period
        sma_slow = float(df["close"].rolling(slow).mean().iloc[-1])
        sma_fast = float(df["close"].rolling(fast).mean().iloc[-1])
        close = float(df["close"].iloc[-1])

        trend_conf = _trend_confidence(last.market, close, sma_fast, sma_slow)
        if last.volatility is not None:
            vol_conf = _volatility_confidence(self._percentile(closes))
        else:
            vol_conf = 0.30  # volatility condition unknown — do not fabricate

        return RegimeAssessment(
            ts=as_of or last.ts,
            regime=last.market,
            confidence=round(_clip(0.6 * trend_conf + 0.4 * vol_conf, 0.0, 1.0), 3),
            volatility=last.volatility,
            trend=last.market.value,
            timeframe=timeframe or self._timeframe,
        )

    def from_bars(
        self,
        bars: Sequence[PriceBar],
        *,
        as_of: datetime | None = None,
        timeframe: Interval | None = None,
    ) -> RegimeAssessment | None:
        """Convenience: assess from an OHLCV bar sequence (uses closes)."""
        return self.assess(
            [(b.ts, float(b.close)) for b in bars],
            as_of=as_of,
            timeframe=timeframe,
        )

    def _percentile(self, closes: Sequence[tuple[datetime, float]]) -> float | None:
        """Trailing percentile rank of the latest realized-vol window."""
        window = self._engine.vol_history
        if len(closes) < window + self._engine.vol_period:
            return None
        log_ret = pd.Series(
            [math.log(c) for _, c in closes], index=pd.RangeIndex(len(closes))
        ).diff()
        realized = log_ret.rolling(self._engine.vol_period).std() * math.sqrt(252.0)
        trailing = realized.iloc[-(window + 1) :]
        if trailing.isna().all():
            return None
        series = trailing.dropna()
        if len(series) < 2:
            return None
        latest = series.iloc[-1]
        pct = float((series < latest).mean() * 100.0)
        return _clip(pct, 0.0, 100.0)


__all__ = ["MarketRegimeAgent"]
