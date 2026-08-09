from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from qtrader.application.services.pattern_events import (
    PatternEvent,
    breakout_events,
    collect_events,
    momentum_cross_events,
    rsi_events,
)
from qtrader.application.services.pattern_metrics import analyze_forward, edge_signals
from qtrader.domain.entities import IndicatorSnapshot
from qtrader.domain.value_objects import Interval, PriceBar

UTC = ZoneInfo("UTC")
DAY = timedelta(days=1)


def _bar(symbol: str, day: int, open_, high, low, close) -> PriceBar:
    return PriceBar(
        symbol=symbol,
        interval=Interval.D1,
        ts=datetime(2026, 1, 1, tzinfo=UTC) + day * DAY,
        open=Decimal(str(open_)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=Decimal("1000000"),
    )


def _snap(
    symbol: str, day: int, rsi: float | None, ema9: float | None, ema21: float | None
) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        symbol=symbol,
        interval=Interval.D1,
        ts=datetime(2026, 1, 1, tzinfo=UTC) + day * DAY,
        rsi=Decimal(str(rsi)) if rsi is not None else None,
        ema_9=Decimal(str(ema9)) if ema9 is not None else None,
        ema_21=Decimal(str(ema21)) if ema21 is not None else None,
    )


def _days(series) -> set[int]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return {(s.ts - start) // DAY for s in series}


class TestMomentumCross:
    def test_up_cross_detected_only_in_oos(self) -> None:
        snaps = [
            _snap("A", 0, None, 9.0, 10.0),
            _snap("A", 1, None, 9.5, 10.0),
            _snap("A", 2, None, 10.5, 10.0),
            _snap("A", 3, None, 11.0, 10.0),
        ]
        oos = [_bar("A", 2, 10.5, 11.2, 10.4, 11.0), _bar("A", 3, 11, 11.2, 10.8, 11.0)]
        events = momentum_cross_events(snaps, oos)
        assert [e.pattern for e in events] == ["momentum_up_cross"]
        assert events[0].ts == snaps[2].ts

    def test_down_cross_detected(self) -> None:
        snaps = [
            _snap("A", 0, None, 11.0, 10.0),
            _snap("A", 1, None, 10.5, 10.0),
            _snap("A", 2, None, 9.5, 10.0),
        ]
        oos = [_bar("A", 2, 9.5, 9.6, 9.4, 9.5)]
        events = momentum_cross_events(snaps, oos)
        assert [e.pattern for e in events] == ["momentum_down_cross"]

    def test_no_event_outside_oos_window(self) -> None:
        snaps = [
            _snap("A", 0, None, 9.0, 10.0),
            _snap("A", 1, None, 10.5, 10.0),
        ]
        oos = [_bar("A", 5, 10, 10, 10, 10)]
        assert momentum_cross_events(snaps, oos) == []


class TestRsiEvents:
    def test_oversold_onset(self) -> None:
        snaps = [
            _snap("A", 0, 45.0, None, None),
            _snap("A", 1, 28.0, None, None),
            _snap("A", 2, 25.0, None, None),
            _snap("A", 3, 55.0, None, None),
        ]
        oos = [_bar("A", d, 10, 10, 10, 10) for d in (1, 2, 3)]
        events = rsi_events(snaps, oos)
        assert [e.pattern for e in events] == ["rsi_oversold"]
        assert _days(events) == {1}

    def test_overbought_onset(self) -> None:
        snaps = [
            _snap("A", 0, 65.0, None, None),
            _snap("A", 1, 72.0, None, None),
            _snap("A", 2, 80.0, None, None),
        ]
        oos = [_bar("A", d, 10, 10, 10, 10) for d in (1, 2)]
        events = rsi_events(snaps, oos)
        assert [e.pattern for e in events] == ["rsi_overbought"]
        assert _days(events) == {1}


class TestBreakout:
    def test_up_breakout(self) -> None:
        bars = [
            _bar("A", i, 10, 10, 10, 10) for i in range(22)
        ]
        bars.append(_bar("A", 22, 11, 11.2, 10.9, 11.1))
        oos = [bars[-1]]
        events = breakout_events(bars, oos, window=20)
        assert [e.pattern for e in events] == ["breakout_up"]

    def test_down_breakout(self) -> None:
        bars = [
            _bar("A", i, 10, 10, 10, 10) for i in range(22)
        ]
        bars.append(_bar("A", 22, 9, 9.1, 8.8, 8.9))
        oos = [bars[-1]]
        events = breakout_events(bars, oos, window=20)
        assert [e.pattern for e in events] == ["breakout_down"]

    def test_needs_full_lookback(self) -> None:
        bars = [_bar("A", i, 10, 10, 10, 10) for i in range(5)]
        bars.append(_bar("A", 5, 15, 15, 15, 15))
        oos = [bars[-1]]
        assert breakout_events(bars, oos, window=20) == []


class TestCollectEvents:
    def test_aggregates_all_patterns(self) -> None:
        bars_by_symbol = {
            "A": [_bar("A", i, 10, 10, 10, 10) for i in range(22)]
        }
        series_by_symbol = {
            "A": [
                _snap("A", 0, None, 9.0, 10.0),
                _snap("A", 1, None, 10.5, 10.0),
                _snap("A", 2, 28.0, 10.5, 10.0),
            ]
        }
        oos = {"A": [bars_by_symbol["A"][1], bars_by_symbol["A"][2]]}
        events = collect_events(bars_by_symbol, series_by_symbol, oos=oos)
        patterns = {e.pattern for e in events}
        assert "momentum_up_cross" in patterns
        assert "rsi_oversold" in patterns


class TestAnalyzeForward:
    def _trend_bars(self) -> list[PriceBar]:
        # closes rise 1% per bar; day 0 is the signal bar at 100.
        bars = [_bar("A", 0, 100, 100, 100, 100)]
        for d in range(1, 6):
            c = round(100 * (1.01**d), 2)
            bars.append(_bar("A", d, c * 0.99, c * 1.01, c * 0.99, c))
        return bars

    def test_success_rate_and_avg_return(self) -> None:
        bars = self._trend_bars()
        events = [PatternEvent("up", "A", bars[0].ts)]
        stats = analyze_forward({"A": bars}, events, horizon_bars=5, round_trip_bps=Decimal("0"))
        assert len(stats) == 1
        assert stats[0].occurrences == 1
        assert stats[0].success_rate == Decimal(1)
        assert stats[0].avg_return > 0
        assert stats[0].avg_mfe > stats[0].avg_return

    def test_net_of_costs_drag(self) -> None:
        bars = self._trend_bars()
        events = [PatternEvent("up", "A", bars[0].ts)]
        gross = analyze_forward({"A": bars}, events, horizon_bars=5, round_trip_bps=Decimal("0"))[0]
        net = analyze_forward({"A": bars}, events, horizon_bars=5, round_trip_bps=Decimal("100"))[0]
        assert net.avg_return_net < gross.avg_return_net
        assert net.avg_return_net < net.avg_return

    def test_event_without_future_bar_skipped(self) -> None:
        bars = self._trend_bars()
        events = [PatternEvent("up", "A", bars[-1].ts)]
        assert analyze_forward({"A": bars}, events, horizon_bars=5) == []

    def test_entry_at_next_open_not_signal_close(self) -> None:
        # signal at day 0 close 100; day 1 opens at 99 -> forward uses 99.
        bars = self._trend_bars()
        events = [PatternEvent("up", "A", bars[0].ts)]
        stats = analyze_forward({"A": bars}, events, horizon_bars=1, round_trip_bps=Decimal("0"))
        entry = float(bars[1].open)
        last = float(bars[1].close)
        assert float(stats[0].avg_return) == pytest.approx(last / entry - 1)


class TestEdgeSignals:
    def test_filters_by_occurrences_and_net_edge(self) -> None:
        bars = {"A": [_bar("A", d, 100, 100 * (1.01**d), 100, 100 * (1.01**d)) for d in range(13)]}
        events = [PatternEvent("good", "A", bars["A"][0].ts) for _ in range(5)]
        stats = analyze_forward(bars, events, horizon_bars=12, round_trip_bps=Decimal("0"))
        assert edge_signals(stats, min_occurrences=20) == []
        assert edge_signals(stats, min_occurrences=1) == [s for s in stats if s.pattern == "good"]
