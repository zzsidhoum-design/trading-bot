"""Unit tests for the RSS/Atom news provider parser."""

from __future__ import annotations

from qtrader.infrastructure.news.feed import parse_feed

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Test</title>
  <item>
    <title>AAPL beats earnings</title>
    <link>https://example.com/a</link>
    <pubDate>Mon, 20 Jul 2026 12:00:00 GMT</pubDate>
    <description>Strong quarter results.</description>
  </item>
  <item>
    <title>No link</title>
  </item>
</channel></rss>
"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Test</title>
  <entry>
    <title>MSFT surges on cloud growth</title>
    <link href="https://example.com/b"/>
    <updated>2026-07-21T09:30:00Z</updated>
    <summary>Cloud revenue up.</summary>
  </entry>
</feed>
"""

INVALID = "not xml at all"


def test_parse_rss() -> None:
    items = parse_feed(RSS, symbol="AAPL")
    assert len(items) == 1
    assert items[0].symbol == "AAPL"
    assert items[0].title == "AAPL beats earnings"
    assert items[0].url == "https://example.com/a"
    assert items[0].published_at.year == 2026


def test_parse_atom() -> None:
    items = parse_feed(ATOM, symbol="MSFT")
    assert len(items) == 1
    assert items[0].title == "MSFT surges on cloud growth"
    assert items[0].url == "https://example.com/b"
    assert items[0].published_at.hour == 9


def test_parse_invalid_returns_empty() -> None:
    assert parse_feed(INVALID) == []
