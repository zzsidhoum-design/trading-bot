"""Asset discovery adapters for the dynamic trading universe.

Primary source is Yahoo's predefined screener ("most active", "day gainers",
...) which returns a ranked candidate universe with metadata. The screener
requires a crumb; the provider obtains it lazily through the cookie flow and
caches it for the client lifetime. All payload parsing lives in the pure
``parse_screener_response`` function so it is unit-testable without a network.
"""

from __future__ import annotations

from typing import Any

import httpx

from qtrader.config.logging import get_logger
from qtrader.domain.entities import AssetType, DiscoveredAsset
from qtrader.domain.ports import AssetDiscoveryProvider
from qtrader.infrastructure.resilience import (
    CircuitBreaker,
    TokenBucket,
    retry_async,
)

DEFAULT_BASE_URL = "https://query1.finance.yahoo.com"
FINANCE_HOME_URL = "https://finance.yahoo.com"

_logger = get_logger("qtrader.discovery")

# Yahoo quoteType -> our broad asset classification.
_ASSET_TYPE_MAP: dict[str, AssetType] = {
    "EQUITY": AssetType.COMMON_STOCK,
    "ETF": AssetType.ETF,
    "MUTUALFUND": AssetType.CLOSED_END_FUND,
    "REIT": AssetType.REIT,
}

# Yahoo exchange codes we want to keep (equities/ETFs). Unknown codes are
# kept too — the engine's liquidity filters decide tradability either way.
_KNOWN_EXCHANGES = {"NMS", "NGM", "NCM", "NYQ", "NYS", "NYE", "PCX", "ASE", "BTS", "OQB", "OQX"}


def _asset_type(quote_type: str | None) -> AssetType:
    if not quote_type:
        return AssetType.OTHER
    return _ASSET_TYPE_MAP.get(quote_type.upper(), AssetType.OTHER)


def parse_screener_response(payload: dict[str, Any]) -> list[DiscoveredAsset]:
    """Parse a Yahoo ``/v1/finance/screener/predefined/saved`` response.

    Returns every quote with a usable symbol (deduplicated, insertion order).
    Quotes missing a symbol, or with a non-equity/ETF quote type we cannot
    classify, are skipped. Empty for errors, ``null`` results or no data.
    """
    finance = payload.get("finance") or {}
    if finance.get("error"):
        return []
    result = finance.get("result") or []
    if not result:
        return []
    quotes = result[0].get("quotes") or []
    if not quotes:
        return []

    assets: list[DiscoveredAsset] = []
    seen: set[str] = set()
    for quote in quotes:
        symbol = quote.get("symbol")
        if not symbol or not str(symbol).strip():
            continue
        symbol = str(symbol).strip().upper()
        if symbol in seen:
            continue
        name = quote.get("shortName") or quote.get("longName") or symbol
        exchange = quote.get("exchange")
        if exchange is not None and str(exchange).upper() in _KNOWN_EXCHANGES:
            exchange = str(exchange).upper()
        try:
            market_cap = float(quote["marketCap"]) if quote.get("marketCap") is not None else None
        except (TypeError, ValueError):
            market_cap = None
        assets.append(
            DiscoveredAsset(
                symbol=symbol,
                name=str(name),
                exchange=exchange,
                asset_type=_asset_type(quote.get("quoteType")),
                currency=str(quote.get("currency") or "USD").upper(),
                market_cap=market_cap,
            )
        )
        seen.add(symbol)
    return assets


class YahooAssetDiscoveryProvider(AssetDiscoveryProvider):
    """AssetDiscoveryProvider backed by Yahoo's predefined screener."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 15.0,
        client: httpx.AsyncClient | None = None,
        circuit: CircuitBreaker | None = None,
        requests_per_second: float = 1.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds)
        self._client = client
        self._circuit = circuit or CircuitBreaker(name="yahoo_screener")
        self._limiter = TokenBucket(
            capacity=requests_per_second * 2,
            refill_rate_per_second=requests_per_second,
            name="yahoo_screener_http",
        )
        self._crumb: str | None = None

    def _transport(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                headers={"User-Agent": "qtrader/0.1"},
                follow_redirects=True,
            )
        return self._client

    async def discover_candidates(self, limit: int = 500) -> list[DiscoveredAsset]:
        """Fetch the predefined "most active" screener, capped at ``limit``."""
        scid = "most_actives"
        count = max(1, min(limit, 200))
        crumb = await self._get_crumb()
        response = await self._request_screener(scid, count, crumb)
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("yahoo screener returned non-JSON") from exc
        assets = parse_screener_response(payload)
        _logger.info(
            "discovery.screener",
            scid=scid,
            requested=count,
            parsed=len(assets),
        )
        return assets[:limit]

    async def _get_crumb(self) -> str:
        if self._crumb:
            return self._crumb
        client = self._transport()
        home = await client.get(FINANCE_HOME_URL)
        home.raise_for_status()
        crumb_response = await client.get("/v1/test/getcrumb")
        crumb_response.raise_for_status()
        crumb = crumb_response.text.strip()
        if not crumb:
            raise RuntimeError("yahoo returned an empty crumb")
        self._crumb = crumb
        return crumb

    @retry_async()
    async def _request_screener(
        self, scid: str, count: int, crumb: str
    ) -> httpx.Response:
        await self._limiter.wait()
        response = await self._transport().get(
            "/v1/finance/screener/predefined/saved",
            params={
                "scrIds": scid,
                "start": 0,
                "count": count,
                "crumb": crumb,
            },
        )
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "unknown")
            raise httpx.HTTPStatusError(
                f"yahoo screener rate limited (429, retry-after={retry_after}s)",
                request=response.request,
                response=response,
            )
        response.raise_for_status()
        return response

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
