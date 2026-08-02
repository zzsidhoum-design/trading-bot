"""Unit tests for the WebSocket topic filter (Phase 7)."""

from __future__ import annotations

from qtrader.interfaces.api.ws import _Client, _parse_topics


def test_parse_topics_none_when_empty() -> None:
    assert _parse_topics(None) is None
    assert _parse_topics("") is None
    assert _parse_topics(", ,") is None


def test_parse_topics_normalizes() -> None:
    topics = _parse_topics("Order, trade, price")
    assert topics == {"order", "trade", "price"}


def test_client_accepts_all_without_filter() -> None:
    client = _Client(object(), None)
    assert client.accepts("OrderFilled")
    assert client.accepts("Anything")


def test_client_filters_by_topic_suffix() -> None:
    client = _Client(object(), _parse_topics("order"))
    assert client.accepts("OrderFilled")
    assert client.accepts("OrderSubmitted")
    assert not client.accepts("PriceUpdated")


def test_client_filter_matches_case_insensitive() -> None:
    client = _Client(object(), _parse_topics("ORDER"))
    assert client.accepts("OrderStatusChanged")
    assert not client.accepts("ScanCompleted")


def test_client_filter_blocks_non_matching() -> None:
    client = _Client(object(), _parse_topics("position"))
    assert client.accepts("PositionClosed")
    assert not client.accepts("OrderFilled")
