"""Unit tests for the Yahoo chart response parser (no network)."""

from __future__ import annotations

from decimal import Decimal

from qtrader.domain.value_objects import Interval
from qtrader.infrastructure.data_providers.yahoo import parse_chart_response


def _payload(
    timestamps: list[int] | None = None, adjclose: list[float | None] | None = None
) -> dict:
    ts = timestamps or [1780000000, 1780000600]
    indicators: dict = {
        "quote": [
            {
                "open": [100.0, 102.0],
                "high": [105.0, 106.0],
                "low": [99.0, 101.0],
                "close": [103.0, 104.0],
                "volume": [1000, 2000],
            }
        ]
    }
    if adjclose is not None:
        indicators["adjclose"] = [{"adjclose": adjclose}]
    return {
        "chart": {
            "result": [
                {
                    "meta": {"symbol": "AAPL", "regularMarketPrice": 103.0},
                    "timestamp": ts,
                    "indicators": indicators,
                }
            ],
            "error": None,
        }
    }


def test_parse_basic() -> None:
    bars = parse_chart_response(_payload(), "AAPL", Interval.M5)
    assert len(bars) == 2
    assert bars[0].symbol == "AAPL"
    assert bars[0].interval is Interval.M5
    assert bars[0].close == Decimal("103")
    assert bars[0].ts.tzinfo is not None
    assert bars[0].ts < bars[1].ts


def test_parse_skips_null_fields() -> None:
    payload = _payload()
    payload["chart"]["result"][0]["indicators"]["quote"][0]["open"][1] = None
    bars = parse_chart_response(payload, "AAPL", Interval.M5)
    assert len(bars) == 1


def test_parse_invalid_bar_skipped() -> None:
    payload = _payload()
    # high < low → invalid OHLC, dropped by PriceBar validation.
    payload["chart"]["result"][0]["indicators"]["quote"][0]["high"][1] = 90.0
    bars = parse_chart_response(payload, "AAPL", Interval.M5)
    assert len(bars) == 1


def test_parse_empty_and_error() -> None:
    assert parse_chart_response({}, "AAPL", Interval.M5) == []
    assert parse_chart_response({"chart": {"error": {"code": 404}}}, "AAPL", Interval.M5) == []
    assert (
        parse_chart_response({"chart": {"result": []}}, "AAPL", Interval.M5) == []
    )


def test_parse_adjusts_ohlc_with_adjclose() -> None:
    # close [103.0, 104.0]; adjclose factors: bar0 0.98, bar1 (latest) 1.0.
    bars = parse_chart_response(
        _payload(adjclose=[100.94, 104.0]), "AAPL", Interval.M5
    )
    assert len(bars) == 2
    assert bars[0].close == Decimal("100.94")
    assert bars[0].open == Decimal("98.00")
    assert bars[0].high == Decimal("102.90")
    assert bars[0].low == Decimal("97.02")
    assert bars[1].close == Decimal("104.0")
    assert bars[1].open == Decimal("102.0")


def test_parse_adjclose_ignored_when_auto_adjust_off() -> None:
    bars = parse_chart_response(
        _payload(adjclose=[100.94, 104.0]), "AAPL", Interval.M5, auto_adjust=False
    )
    assert bars[0].close == Decimal("103")
    assert bars[0].open == Decimal("100.0")


def test_parse_adjclose_null_falls_back_to_raw() -> None:
    bars = parse_chart_response(
        _payload(adjclose=[None, 104.0]), "AAPL", Interval.M5
    )
    assert len(bars) == 2
    assert bars[0].close == Decimal("103")
    assert bars[0].open == Decimal("100.0")
