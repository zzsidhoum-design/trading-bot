"""RSS/Atom news provider — stdlib-only parser over httpx.

Fetches the Yahoo Finance headline RSS feed for a symbol (or the generic market
feed) and returns un-analyzed :class:`NewsItem` rows. Publishing/analysis is the
News Agent's job.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from xml.etree import ElementTree as ET

import httpx

from qtrader.domain.entities import NewsItem
from qtrader.domain.ports import NewsProvider

RSS_NS = "{http://www.w3.org/2005/Atom}"
FALLBACK_FEED = "https://feeds.finance.yahoo.com/rss/2.0/headline?region=US&lang=en-US"
GOOGLE_NEWS_URL = "https://news.google.com/rss/search"


def _text(node: Any, tag: str) -> str | None:
    found = node.find(f"{RSS_NS}{tag}")
    return (found.text or "").strip() if found is not None and found.text else None


def _rss_text(node: Any, tag: str) -> str | None:
    found = node.find(tag)
    return (found.text or "").strip() if found is not None and found.text else None


def _src_text(node: Any) -> str | None:
    found = node.find("source")
    raw: str | None = found.text if found is not None else None
    text = raw.strip() if raw else None
    if text:
        return text
    back = node.find("source[@url]")
    url: str | None = back.get("url") if back is not None else None
    if url:
        from urllib.parse import urlparse

        netloc = urlparse(url).netloc
        if netloc:
            return netloc.replace("www.", "")
    return "rss"


def _parse_datetime(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime.now(UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_feed(payload: str, symbol: str | None = None) -> list[NewsItem]:
    """Parse RSS 2.0 or Atom XML into NewsItem rows (pure, unit-testable)."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return []
    entries = root.findall(f".//{RSS_NS}entry")
    if entries:
        return _parse_atom(root, symbol)
    return _parse_rss(root, symbol)


def _parse_atom(root: ET.Element, symbol: str | None) -> list[NewsItem]:
    items: list[NewsItem] = []
    for entry in root.findall(f".//{RSS_NS}entry"):
        link_node = entry.find(f"{RSS_NS}link")
        href = link_node.get("href") if link_node is not None else None
        title = _text(entry, "title")
        if not title or not href:
            continue
        items.append(
            NewsItem(
                symbol=symbol,
                source=_text(entry, "source") or "rss",
                title=title,
                url=href,
                published_at=_parse_datetime(_text(entry, "updated")),
                content=_text(entry, "summary"),
            )
        )
    return items


def _parse_rss(root: ET.Element, symbol: str | None) -> list[NewsItem]:
    items: list[NewsItem] = []
    for item in root.findall("./channel/item"):
        title = _rss_text(item, "title")
        link = _rss_text(item, "link")
        if not title or not link:
            continue
        items.append(
            NewsItem(
                symbol=symbol,
                source=_src_text(item) or "rss",
                title=title,
                url=link,
                published_at=_parse_datetime(_rss_text(item, "pubDate")),
                content=_rss_text(item, "description"),
            )
        )
    return items


class RSSNewsProvider(NewsProvider):
    """NewsProvider backed by RSS/Atom feeds over httpx (async).

    Primary source is the Google News RSS search feed (free, no API key); the
    legacy Yahoo Finance RSS endpoint is kept as a fallback.
    """

    def __init__(self, client: httpx.AsyncClient | None = None, per_symbol: bool = True) -> None:
        self._client = client or httpx.AsyncClient(timeout=10.0, follow_redirects=True)
        self._owns_client = client is None
        self._per_symbol = per_symbol

    @staticmethod
    def _feed_url(symbol: str | None) -> str:
        if symbol:
            from urllib.parse import quote

            query = quote(f'"{symbol}" stock news')
            return f"{GOOGLE_NEWS_URL}?q={query}&hl=en-US&gl=US&ceid=US:en"
        return (
            "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"
        )

    @staticmethod
    def _fallback_url(symbol: str | None) -> str:
        if symbol:
            return (
                "https://feeds.finance.yahoo.com/rss/2.0/headline"
                f"?s={symbol}&region=US&lang=en-US"
            )
        return FALLBACK_FEED

    async def _fetch(self, url: str) -> str:
        response = await self._client.get(url)
        response.raise_for_status()
        return response.text

    async def fetch_news(self, symbol: str | None, since: datetime, limit: int) -> list[NewsItem]:
        urls = [self._feed_url(symbol), self._fallback_url(symbol)]
        payload = ""
        last_error: Exception | None = None
        for url in urls:
            try:
                payload = await self._fetch(url)
                if payload:
                    break
            except httpx.HTTPError as exc:
                last_error = exc
        if not payload or not payload.strip():
            raise RuntimeError(
                f"news fetch failed for {symbol or 'market'}: "
                f"{last_error or 'empty response'}"
            )
        items = parse_feed(payload, symbol=symbol if self._per_symbol else None)
        recent = [i for i in items if i.published_at >= since]
        return recent[:limit]

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
