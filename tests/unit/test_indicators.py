"""Unit tests for the indicator engine and technical scoring."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from qtrader.application.services.indicators import (
    RSI,
    VWAP,
    IndicatorEngine,
    frame_from_bars,
    score_technical,
)
from qtrader.domain.value_objects import Interval, PriceBar, SignalType

BASE = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _bars(count: int = 260, base: float = 50.0, drift: float = 0.3) -> list[PriceBar]:
    bars = []
    for i in range(count):
        close = base + drift * i
        open_ = close - drift
        high = close + 2.0
        low = close - 2.0
        bars.append(
            PriceBar(
                symbol="TEST",
                interval=Interval.M5,
                ts=BASE - timedelta(minutes=5 * (count - 1 - i)),
                open=Decimal(str(round(open_, 4))),
                high=Decimal(str(round(high, 4))),
                low=Decimal(str(round(low, 4))),
                close=Decimal(str(round(close, 4))),
                volume=Decimal("1000000"),
            )
        )
    bars.sort(key=lambda b: b.ts)
    return bars


def test_frame_from_bars_ohlc() -> None:
    bars = _bars(count=5)
    df = frame_from_bars(bars)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 5
    assert df.index.name == "ts"
    assert df["close"].iloc[-1] > df["close"].iloc[0]


def test_rsi_bounds() -> None:
    bars = _bars(count=100, drift=0.5)
    out = RSI(14).compute(frame_from_bars(bars))
    rsi = out["rsi"].dropna()
    assert ((rsi >= 0) & (rsi <= 100)).all()


def test_vwap_within_range() -> None:
    bars = _bars(count=50)
    out = VWAP().compute(frame_from_bars(bars))
    vwap = out["vwap"].dropna().iloc[-1]
    frame = frame_from_bars(bars)
    assert frame["low"].iloc[-1] <= vwap <= frame["high"].iloc[-1] or abs(
        vwap - frame["close"].iloc[-1]
    ) < frame["high"].iloc[-1]


def test_engine_snapshot_fields() -> None:
    bars = _bars(count=260, drift=0.3)
    engine = IndicatorEngine()
    snap = engine.compute(bars, "TEST", Interval.M5)
    assert snap.symbol == "TEST"
    assert snap.interval is Interval.M5
    assert snap.ts == bars[-1].ts
    assert snap.rsi is not None
    assert snap.ema_9 is not None and snap.ema_21 is not None
    assert snap.sma_50 is not None
    assert snap.macd is not None and snap.macd_hist is not None
    assert snap.atr is not None and snap.vwap is not None
    assert snap.boll_upper is not None and snap.boll_lower is not None
    assert snap.adx is not None
    assert snap.stoch_k is not None and snap.stoch_d is not None
    assert snap.ichimoku_tenkan is not None
    assert snap.volume_profile is not None


def test_engine_requires_bars() -> None:
    with pytest.raises(ValueError):
        IndicatorEngine().compute([], "TEST", Interval.M5)


def test_score_technical_uptrend() -> None:
    bars = _bars(count=260, drift=0.4)
    snap = IndicatorEngine().compute(bars, "TEST", Interval.M5)
    score, signal_type, sub = score_technical(snap)
    assert -1.0 <= score <= 1.0
    assert signal_type in {SignalType.BUY, SignalType.STRONG_BUY, SignalType.NEUTRAL}
    assert "trend" in sub and "momentum" in sub and "score" in sub


def test_score_technical_downtrend() -> None:
    bars = _bars(count=260, base=200.0, drift=-0.4)
    snap = IndicatorEngine().compute(bars, "TEST", Interval.M5)
    score, signal_type, _ = score_technical(snap)
    assert -1.0 <= score <= 1.0
    assert signal_type in {SignalType.SELL, SignalType.STRONG_SELL, SignalType.NEUTRAL}
