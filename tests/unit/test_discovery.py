"""Unit tests for the Yahoo screener asset-discovery parser."""

from __future__ import annotations

from typing import Any

from qtrader.domain.entities import AssetType
from qtrader.infrastructure.data_providers.discovery import parse_screener_response


def _screener_payload(quotes: list[dict[str, Any]]) -> dict[str, Any]:
    return {"finance": {"result": [{"quotes": quotes, "total": len(quotes)}], "error": None}}


def test_parses_quotes_with_metadata() -> None:
    payload = _screener_payload(
        [
            {
                "symbol": "AAPL",
                "shortName": "Apple Inc.",
                "quoteType": "EQUITY",
                "exchange": "NMS",
                "marketCap": 3000000000000,
                "currency": "USD",
            },
            {
                "symbol": "SPY",
                "longName": "SPDR S&P 500 ETF",
                "quoteType": "ETF",
                "exchange": "PCX",
                "marketCap": 500000000000,
                "currency": "USD",
            },
        ]
    )
    assets = parse_screener_response(payload)
    assert [a.symbol for a in assets] == ["AAPL", "SPY"]
    assert assets[0].name == "Apple Inc."
    assert assets[0].asset_type is AssetType.COMMON_STOCK
    assert assets[0].exchange == "NMS"
    assert assets[0].market_cap == 3000000000000.0
    assert assets[0].currency == "USD"
    assert assets[1].asset_type is AssetType.ETF


def test_skips_entries_without_symbol_and_deduplicates() -> None:
    payload = _screener_payload(
        [
            {"symbol": "", "quoteType": "EQUITY"},
            {"quoteType": "EQUITY"},
            {"symbol": "MSFT", "quoteType": "EQUITY"},
            {"symbol": "msft", "quoteType": "EQUITY"},
        ]
    )
    assets = parse_screener_response(payload)
    assert [a.symbol for a in assets] == ["MSFT"]


def test_classifies_unknown_quote_type_as_other() -> None:
    payload = _screener_payload(
        [{"symbol": "BTC-USD", "quoteType": "CRYPTOCURRENCY"}]
    )
    assets = parse_screener_response(payload)
    assert assets[0].asset_type is AssetType.OTHER


def test_empty_for_error_or_no_result() -> None:
    assert parse_screener_response({"finance": {"error": {"code": "Unauthorized"}}}) == []
    assert parse_screener_response({"finance": {"result": []}}) == []
    assert parse_screener_response({}) == []
    assert parse_screener_response({"finance": {"result": [{"quotes": []}]}}) == []


def test_tolerates_missing_optional_fields() -> None:
    payload = _screener_payload([{"symbol": "X"}])
    assets = parse_screener_response(payload)
    assert assets[0].name == "X"
    assert assets[0].market_cap is None
    assert assets[0].currency == "USD"
