"""Feature store — deterministic feature vectors for the Prediction Agent.

Features are computed from price bars (pure function), then enriched with the
latest indicator snapshot and per-agent signal scores. Every vector carries a
``feature_hash`` (SHA-256 over the sorted feature values) for provenance.

Training features (``FEATURE_NAMES``) are price-only so the ``ModelTrainer``
needs nothing but the ``PriceRepository``.
"""

from __future__ import annotations

import hashlib
import statistics
from dataclasses import dataclass
from datetime import datetime

from qtrader.domain.ports import IndicatorRepository, PriceRepository, SignalRepository
from qtrader.domain.value_objects import Interval, PriceBar

# Canonical, dimensionless price features used by the ML trainer.
FEATURE_NAMES: tuple[str, ...] = (
    "ret_1",
    "ret_5",
    "ret_20",
    "momentum_20",
    "vol_20",
    "atr_pct",
    "volume_ratio",
    "range_ratio",
)


def _pct(a: float, b: float) -> float:
    return (a - b) / b if b else 0.0


def price_features_from_bars(bars: list[PriceBar]) -> dict[str, float]:
    """Pure function: OHLCV bars -> dimensionless feature dict (deterministic)."""
    if len(bars) < 2:
        return {name: 0.0 for name in FEATURE_NAMES}
    closes = [float(b.close) for b in bars]
    last = closes[-1]

    ret_1 = _pct(closes[-1], closes[-2])
    ret_5 = _pct(closes[-1], closes[-6]) if len(closes) >= 6 else _pct(closes[-1], closes[0])
    ret_20 = _pct(closes[-1], closes[-21]) if len(closes) >= 21 else _pct(closes[-1], closes[0])

    returns = [_pct(closes[i], closes[i - 1]) for i in range(1, len(closes))]
    vol_20 = statistics.pstdev(returns) if len(returns) > 1 else 0.0

    trs: list[float] = []
    for i in range(1, len(bars)):
        high, low, prev_close = float(bars[i].high), float(bars[i].low), float(bars[i - 1].close)
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    recent_trs = trs[-20:] if trs else []
    atr = sum(recent_trs) / len(recent_trs) if recent_trs else 0.0
    atr_pct = atr / last if last else 0.0

    volumes = [float(b.volume) for b in bars]
    recent_vol = sum(volumes[-5:]) / len(volumes[-5:]) if volumes else 0.0
    base_vol = sum(volumes[-20:]) / len(volumes[-20:]) if len(volumes) >= 20 else recent_vol
    volume_ratio = recent_vol / base_vol if base_vol else 0.0

    ranges = [
        (float(b.high) - float(b.low)) / float(b.close) if float(b.close) else 0.0 for b in bars
    ]
    range_ratio = statistics.fmean(ranges[-20:]) if ranges else 0.0

    return {
        "ret_1": round(ret_1, 6),
        "ret_5": round(ret_5, 6),
        "ret_20": round(ret_20, 6),
        "momentum_20": round(ret_20, 6),
        "vol_20": round(vol_20, 6),
        "atr_pct": round(atr_pct, 6),
        "volume_ratio": round(volume_ratio, 6),
        "range_ratio": round(range_ratio, 6),
    }


def feature_hash(features: dict[str, float]) -> str:
    """Deterministic SHA-256 over the sorted (name, rounded value) pairs."""
    items = "|".join(f"{k}:{v:.6f}" for k, v in sorted(features.items()))
    return hashlib.sha256(items.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class FeatureVector:
    symbol: str
    interval: Interval
    ts: datetime
    features: dict[str, float]
    feature_hash: str


SIGNAL_AGENTS: tuple[str, ...] = ("technical", "news", "fundamental")


class FeatureStore:
    def __init__(
        self,
        prices: PriceRepository,
        indicators: IndicatorRepository,
        signals: SignalRepository,
    ) -> None:
        self._prices = prices
        self._indicators = indicators
        self._signals = signals

    async def build_features(
        self,
        symbol: str,
        interval: Interval,
        lookback_bars: int = 120,
        min_bars: int = 30,
    ) -> FeatureVector | None:
        bars = await self._prices.history(symbol, interval, limit=lookback_bars)
        if len(bars) < min_bars:
            return None
        features = price_features_from_bars(bars)

        snapshot = await self._indicators.latest(symbol, interval)
        if snapshot is not None:
            if snapshot.rsi is not None:
                features["rsi"] = float(snapshot.rsi)
            if snapshot.ema_9 is not None and snapshot.ema_21:
                features["ema_ratio"] = float(snapshot.ema_9 / snapshot.ema_21)
            if snapshot.macd_hist is not None:
                features["macd_hist"] = float(snapshot.macd_hist)
            if snapshot.adx is not None:
                features["adx"] = float(snapshot.adx)
            if snapshot.stoch_k is not None:
                features["stoch_k"] = float(snapshot.stoch_k)
            if (
                snapshot.boll_upper is not None
                and snapshot.boll_lower is not None
                and snapshot.boll_middle is not None
            ):
                span = float(snapshot.boll_upper - snapshot.boll_lower)
                if span > 0:
                    features["boll_pos"] = float(
                        (float(snapshot.boll_middle) - float(snapshot.boll_lower)) / span
                    )

        for agent in SIGNAL_AGENTS:
            latest = await self._signals.latest_for_symbol(symbol, agent)
            if latest:
                features[f"signal_{agent}"] = float(latest[0].score)

        return FeatureVector(
            symbol=symbol,
            interval=interval,
            ts=bars[-1].ts,
            features=features,
            feature_hash=feature_hash(features),
        )
