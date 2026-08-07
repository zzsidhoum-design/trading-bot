"""TechnicalIndicators — pure, vectorized indicator engine (pandas, no I/O).

One class per indicator (OCP). Each computes one or more named series over a
price frame indexed by timestamp; the engine concatenates the latest row into
an :class:`IndicatorSnapshot` that matches the ``indicators`` table.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any

import pandas as pd

from qtrader.domain.entities import IndicatorSnapshot
from qtrader.domain.value_objects import Interval, PriceBar, SignalType

SIX = Decimal("0.000001")


def frame_from_bars(bars: list[PriceBar]) -> pd.DataFrame:
    df = pd.DataFrame(
        {
            "open": [float(b.open) for b in bars],
            "high": [float(b.high) for b in bars],
            "low": [float(b.low) for b in bars],
            "close": [float(b.close) for b in bars],
            "volume": [float(b.volume) for b in bars],
        },
        index=pd.DatetimeIndex([b.ts for b in bars]),
    )
    df.index.name = "ts"
    return df


def _dec(value: Any) -> Decimal | None:
    try:
        if value is None or pd.isna(value):
            return None
        return Decimal(str(round(float(value), 6))).quantize(SIX)
    except (ValueError, TypeError, OverflowError):
        return None


class Indicator(ABC):
    name: str

    @abstractmethod
    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        """Return a DataFrame (index = df.index) with one or more columns."""


class RSI(Indicator):
    name = "rsi"

    def __init__(self, period: int = 14) -> None:
        self.period = period

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        delta = df["close"].diff()
        gain = delta.clip(lower=0.0)
        loss = -delta.clip(upper=0.0)
        avg_gain = gain.ewm(alpha=1 / self.period, adjust=False, min_periods=self.period).mean()
        avg_loss = loss.ewm(alpha=1 / self.period, adjust=False, min_periods=self.period).mean()
        rs = avg_gain / avg_loss
        rsi = 100 - 100 / (1 + rs)
        return pd.DataFrame({"rsi": rsi})


class ExponentialMovingAverage(Indicator):
    def __init__(self, period: int) -> None:
        self.period = period
        self.name = f"ema_{period}"

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame({self.name: df["close"].ewm(span=self.period, adjust=False).mean()})


class SimpleMovingAverage(Indicator):
    def __init__(self, period: int) -> None:
        self.period = period
        self.name = f"sma_{period}"

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame({self.name: df["close"].rolling(self.period).mean()})


class MACD(Indicator):
    name = "macd"

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9) -> None:
        self.fast, self.slow, self.signal = fast, slow, signal

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        fast = df["close"].ewm(span=self.fast, adjust=False).mean()
        slow = df["close"].ewm(span=self.slow, adjust=False).mean()
        macd = fast - slow
        sig = macd.ewm(span=self.signal, adjust=False).mean()
        return pd.DataFrame({"macd": macd, "macd_signal": sig, "macd_hist": macd - sig})


class ATR(Indicator):
    name = "atr"

    def __init__(self, period: int = 14) -> None:
        self.period = period

    @staticmethod
    def true_range(df: pd.DataFrame) -> pd.Series:
        prev_close = df["close"].shift(1)
        return pd.concat(
            [
                df["high"] - df["low"],
                (df["high"] - prev_close).abs(),
                (df["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame({"atr": self.true_range(df).rolling(self.period).mean()})


class VWAP(Indicator):
    name = "vwap"

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        typical = (df["high"] + df["low"] + df["close"]) / 3
        vol = df["volume"]
        cum_vol = vol.cumsum()
        vwap = (typical * vol).cumsum() / cum_vol.where(cum_vol != 0)
        return pd.DataFrame({"vwap": vwap})


class BollingerBands(Indicator):
    name = "bollinger"

    def __init__(self, period: int = 20, std_dev: float = 2.0) -> None:
        self.period, self.std_dev = period, std_dev

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        middle = df["close"].rolling(self.period).mean()
        std = df["close"].rolling(self.period).std()
        return pd.DataFrame(
            {
                "boll_upper": middle + self.std_dev * std,
                "boll_middle": middle,
                "boll_lower": middle - self.std_dev * std,
            }
        )


class ADX(Indicator):
    name = "adx"

    def __init__(self, period: int = 14) -> None:
        self.period = period

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        up = df["high"].diff()
        down = -df["low"].diff()
        plus_dm = pd.Series(0.0, index=df.index)
        minus_dm = pd.Series(0.0, index=df.index)
        plus_dm[(up > down) & (up > 0)] = up[(up > down) & (up > 0)]
        minus_dm[(down > up) & (down > 0)] = down[(down > up) & (down > 0)]
        tr = ATR.true_range(df)
        alpha = 1 / self.period
        atr_s = tr.ewm(alpha=alpha, adjust=False, min_periods=self.period).mean()
        plus_di = 100 * self._smooth(plus_dm) / atr_s.where(atr_s != 0)
        minus_di = 100 * self._smooth(minus_dm) / atr_s.where(atr_s != 0)
        di_sum = plus_di + minus_di
        dx = 100 * (plus_di - minus_di).abs() / di_sum.where(di_sum != 0)
        adx = dx.ewm(alpha=alpha, adjust=False, min_periods=self.period).mean()
        return pd.DataFrame({"adx": adx})

    def _smooth(self, series: pd.Series) -> pd.Series:
        return series.ewm(
            alpha=1 / self.period, adjust=False, min_periods=self.period
        ).mean()


class Stochastic(Indicator):
    name = "stochastic"

    def __init__(self, k_period: int = 14, d_period: int = 3) -> None:
        self.k_period, self.d_period = k_period, d_period

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        low_min = df["low"].rolling(self.k_period).min()
        high_max = df["high"].rolling(self.k_period).max()
        rng = (high_max - low_min).where(high_max != low_min)
        k = 100 * (df["close"] - low_min) / rng
        d = k.rolling(self.d_period).mean()
        return pd.DataFrame({"stoch_k": k, "stoch_d": d})


class Ichimoku(Indicator):
    name = "ichimoku"

    def __init__(self, tenkan: int = 9, kijun: int = 26, senkou_b: int = 52) -> None:
        self.tenkan, self.kijun, self.senkou_b = tenkan, kijun, senkou_b

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        tenkan = (df["high"].rolling(self.tenkan).max() + df["low"].rolling(self.tenkan).min()) / 2
        kijun = (df["high"].rolling(self.kijun).max() + df["low"].rolling(self.kijun).min()) / 2
        senkou_a = ((tenkan + kijun) / 2).shift(self.kijun)
        senkou_b = (
            (df["high"].rolling(self.senkou_b).max() + df["low"].rolling(self.senkou_b).min()) / 2
        ).shift(self.kijun)
        chikou = df["close"].shift(-self.kijun)
        return pd.DataFrame(
            {
                "ichimoku_tenkan": tenkan,
                "ichimoku_kijun": kijun,
                "ichimoku_senkou_a": senkou_a,
                "ichimoku_senkou_b": senkou_b,
                "ichimoku_chikou": chikou,
            }
        )


class VolumeProfile(Indicator):
    name = "volume_profile"

    def __init__(self, bins: int = 10) -> None:
        self.bins = bins

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        profile: dict[str, float] = {}
        if not df.empty and df["close"].max() != df["close"].min():
            edges = pd.cut(df["close"], bins=self.bins, include_lowest=True)
            grouped = df.groupby(edges, observed=False)["volume"].sum()
            profile = {str(interval): float(v) for interval, v in grouped.items()}
        row = pd.DataFrame({"volume_profile": [profile]}, index=df.index[-1:])
        return row


class IndicatorEngine:
    """Composes indicators and emits the latest IndicatorSnapshot."""

    def __init__(self, indicators: list[Indicator] | None = None) -> None:
        self.indicators = indicators or [
            RSI(),
            ExponentialMovingAverage(9),
            ExponentialMovingAverage(21),
            SimpleMovingAverage(50),
            SimpleMovingAverage(200),
            MACD(),
            ATR(),
            VWAP(),
            BollingerBands(),
            ADX(),
            Stochastic(),
            Ichimoku(),
            VolumeProfile(),
        ]

    def compute(self, bars: list[PriceBar], symbol: str, interval: Interval) -> IndicatorSnapshot:
        df = frame_from_bars(bars)
        if df.empty:
            raise ValueError(f"no bars to compute indicators for {symbol}")
        combined = pd.concat([ind.compute(df) for ind in self.indicators], axis=1)
        return self._snapshot(combined.iloc[-1], bars[-1], symbol, interval)

    def compute_series(
        self, bars: list[PriceBar], symbol: str, interval: Interval
    ) -> list[IndicatorSnapshot]:
        """Vectorized snapshot per bar, identical to ``compute`` at each index.

        Every indicator is an online (cumulative) function, so the value at row
        ``i`` over the full series equals the value ``compute`` would return for
        the prefix ``bars[:i+1]``. Callers can therefore precompute once per
        symbol instead of re-running the whole frame for every bar. The two
        exceptions are ``volume_profile`` (only the final row is populated) and
        ``ichimoku_chikou`` (forward-looking ``shift(-26)``); neither is read by
        the backtest signal path.
        """
        df = frame_from_bars(bars)
        if df.empty:
            return []
        combined = pd.concat([ind.compute(df) for ind in self.indicators], axis=1)
        return [
            self._snapshot(combined.iloc[idx], bars[idx], symbol, interval)
            for idx in range(len(bars))
        ]

    @staticmethod
    def _snapshot(
        row: Any, latest: PriceBar, symbol: str, interval: Interval
    ) -> IndicatorSnapshot:
        profile = row.get("volume_profile")
        return IndicatorSnapshot(
            symbol=symbol,
            interval=interval,
            ts=latest.ts,
            rsi=_dec(row.get("rsi")),
            ema_9=_dec(row.get("ema_9")),
            ema_21=_dec(row.get("ema_21")),
            sma_50=_dec(row.get("sma_50")),
            sma_200=_dec(row.get("sma_200")),
            macd=_dec(row.get("macd")),
            macd_signal=_dec(row.get("macd_signal")),
            macd_hist=_dec(row.get("macd_hist")),
            atr=_dec(row.get("atr")),
            vwap=_dec(row.get("vwap")),
            boll_upper=_dec(row.get("boll_upper")),
            boll_middle=_dec(row.get("boll_middle")),
            boll_lower=_dec(row.get("boll_lower")),
            adx=_dec(row.get("adx")),
            stoch_k=_dec(row.get("stoch_k")),
            stoch_d=_dec(row.get("stoch_d")),
            ichimoku_tenkan=_dec(row.get("ichimoku_tenkan")),
            ichimoku_kijun=_dec(row.get("ichimoku_kijun")),
            ichimoku_senkou_a=_dec(row.get("ichimoku_senkou_a")),
            ichimoku_senkou_b=_dec(row.get("ichimoku_senkou_b")),
            ichimoku_chikou=_dec(row.get("ichimoku_chikou")),
            volume_profile=profile if isinstance(profile, dict) else None,
        )


def _signal_type(score: float) -> SignalType:
    if score >= 0.7:
        return SignalType.STRONG_BUY
    if score >= 0.3:
        return SignalType.BUY
    if score <= -0.7:
        return SignalType.STRONG_SELL
    if score <= -0.3:
        return SignalType.SELL
    return SignalType.NEUTRAL


def score_technical(snapshot: IndicatorSnapshot) -> tuple[float, SignalType, dict[str, float]]:
    """Composite technical score in [-1, 1] from the latest indicator row."""
    sub: dict[str, float] = {}

    ema9 = float(snapshot.ema_9) if snapshot.ema_9 else 0.0
    ema21 = float(snapshot.ema_21) if snapshot.ema_21 else 0.0
    sma50 = float(snapshot.sma_50) if snapshot.sma_50 else 0.0
    if ema9 and ema21 and sma50:
        trend = 1.0 if (ema9 > ema21 > sma50) else (-1.0 if (ema9 < ema21 < sma50) else 0.0)
    else:
        trend = 0.0
    sub["trend"] = trend

    rsi = float(snapshot.rsi) if snapshot.rsi else 50.0
    if rsi >= 70:
        momentum = -0.5
    elif rsi >= 60:
        momentum = 1.0
    elif rsi <= 30:
        momentum = 0.5
    elif rsi <= 40:
        momentum = -1.0
    else:
        momentum = 0.0
    hist = float(snapshot.macd_hist) if snapshot.macd_hist else 0.0
    momentum += 0.5 if hist > 0 else (-0.5 if hist < 0 else 0.0)
    momentum = max(-1.0, min(1.0, momentum / 1.5))
    sub["momentum"] = momentum

    adx = float(snapshot.adx) if snapshot.adx else 0.0
    trend_weight = min(1.0, max(0.4, adx / 50.0)) if adx > 0 else 0.4

    score = trend * trend_weight + momentum * (1.0 - trend_weight)
    score = max(-1.0, min(1.0, score))
    sub["rsi"] = (rsi - 50.0) / 50.0
    sub["score"] = score
    return score, _signal_type(score), sub
