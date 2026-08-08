"""Market regime classification -- pure, causal trend + volatility regimes.

Regimes are computed from a single reference series (the equal-weight S&P 500
index or a benchmark like SPY). Every value at row ``i`` uses only data up to
row ``i`` (no look-ahead), so the classifier is safe to label OOS bars.

- Market trend axis: BULL / BEAR / SIDEWAYS from price vs 200-day SMA plus
  50/200-day SMA alignment.
- Volatility axis: LOW / HIGH / EXTREME from the trailing percentile rank of
  20-day realized (annualized) volatility within its own 250-day history.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import pandas as pd


class MarketRegime(StrEnum):
    BULL = "bull"
    BEAR = "bear"
    SIDEWAYS = "sideways"


class VolatilityRegime(StrEnum):
    LOW = "low"
    HIGH = "high"
    EXTREME = "extreme"


@dataclass(frozen=True, slots=True)
class RegimeLabel:
    """One day's regime classification (``None`` when history is too short)."""

    ts: datetime
    market: MarketRegime | None
    volatility: VolatilityRegime | None

    @property
    def label(self) -> str:
        if self.market is None or self.volatility is None:
            return "n/a"
        return f"{self.market.value}-{self.volatility.value}"


class MarketRegimeEngine:
    """Classifies a reference close series into trend x volatility regimes."""

    def __init__(
        self,
        *,
        trend_period: int = 200,
        fast_trend_period: int = 50,
        vol_period: int = 20,
        vol_history: int = 250,
        high_vol_percentile: float = 70.0,
        extreme_vol_percentile: float = 95.0,
    ) -> None:
        self.trend_period = trend_period
        self.fast_trend_period = fast_trend_period
        self.vol_period = vol_period
        self.vol_history = vol_history
        self.high_vol_percentile = high_vol_percentile
        self.extreme_vol_percentile = extreme_vol_percentile

    def classify(
        self, closes: Sequence[tuple[datetime, float]]
    ) -> list[RegimeLabel]:
        """Return one label per input day (None fields for cold-start rows)."""
        if not closes:
            return []
        df = pd.DataFrame(
            {"close": [c for _, c in closes]},
            index=pd.DatetimeIndex([ts for ts, _ in closes]),
        )

        market = self._trend_axis(df)
        volatility = self._volatility_axis(df)

        return [
            RegimeLabel(ts=ts, market=m, volatility=v)
            for ts, m, v in zip(
                df.index.to_pydatetime(),
                market,
                volatility,
                strict=True,
            )
        ]

    def _trend_axis(self, df: pd.DataFrame) -> list[MarketRegime | None]:
        sma_fast = df["close"].rolling(self.fast_trend_period).mean()
        sma_slow = df["close"].rolling(self.trend_period).mean()
        out: list[MarketRegime | None] = []
        for close, fast, slow in zip(
            df["close"], sma_fast, sma_slow, strict=True
        ):
            if close != close or fast != fast or slow != slow:
                out.append(None)
            elif close > slow and fast > slow:
                out.append(MarketRegime.BULL)
            elif close < slow and fast < slow:
                out.append(MarketRegime.BEAR)
            else:
                out.append(MarketRegime.SIDEWAYS)
        return out

    def _volatility_axis(self, df: pd.DataFrame) -> list[VolatilityRegime | None]:
        log_ret = df["close"].apply(math.log).diff()
        realized = log_ret.rolling(self.vol_period).std() * math.sqrt(252.0)

        def rank_pct(window: pd.Series) -> float:
            if window.isna().any() or len(window) == 0:
                return float("nan")
            return float((window < window.iloc[-1]).mean() * 100.0)

        percentile = realized.rolling(
            self.vol_history, min_periods=self.vol_history
        ).apply(rank_pct, raw=False)

        out: list[VolatilityRegime | None] = []
        for pct, vol in zip(percentile, realized, strict=True):
            if pct != pct or vol != vol:
                out.append(None)
            elif pct >= self.extreme_vol_percentile:
                out.append(VolatilityRegime.EXTREME)
            elif pct >= self.high_vol_percentile:
                out.append(VolatilityRegime.HIGH)
            else:
                out.append(VolatilityRegime.LOW)
        return out
