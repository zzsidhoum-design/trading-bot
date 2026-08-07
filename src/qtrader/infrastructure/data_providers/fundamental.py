"""Fundamental data providers (docs/02-agents.md §5).

``EdgarFundamentalProvider`` pulls REAL, authoritative fundamentals from the
SEC's EDGAR XBRL API (public, keyless, rate-limited to ~10 req/s) and computes
valuation multiples with a live price from the Yahoo provider.

``StubFundamentalProvider`` remains as an offline fallback used only when the
EDGAR fetch fails — it is deterministic but its values are synthetic, so the
agent logs a warning whenever it is exercised.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import httpx

from qtrader.config.logging import get_logger
from qtrader.domain.entities import FundamentalData
from qtrader.domain.ports import FundamentalProvider, MarketDataProvider
from qtrader.infrastructure.resilience import TokenBucket, retry_async

_logger = get_logger("qtrader.edgar")

EDGAR_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
EDGAR_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# XBRL concepts needed to build the FundamentalData record. `base` is the
# primary tag; `fallbacks` are alternates seen in older filings.
CONCEPTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "revenue": (
        "Revenues",
        ("RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"),
    ),
    "gross_profit": ("GrossProfit", ("SalesRevenueNet",)),
    "operating_income": (
        "OperatingIncomeLoss",
        (
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxes"
            "ExtraordinaryItemsNoncontrollingInterest",
        ),
    ),
    "net_income": ("NetIncomeLoss", ("ProfitLoss",)),
    "stockholders_equity": (
        "StockholdersEquity",
        (
            "StockholdersEquityIncludingPortionAttributableTo"
            "NoncontrollingInterest",
        ),
    ),
    "assets": ("Assets", ()),
    "long_term_debt": ("LongTermDebt", ("LongTermDebtNoncurrent",)),
    "short_term_debt": ("ShortTermBorrowings", ("DebtCurrent",)),
    "operating_cash_flow": (
        "NetCashProvidedByUsedInOperatingActivities",
        ("NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",),
    ),
    "eps": ("EarningsPerShareDiluted", ("EarningsPerShareBasic",)),
    "shares_outstanding": (
        "CommonStockSharesOutstanding",
        ("CommonStockSharesOutstandingIssued",),
    ),
}

_DECIMAL_0 = Decimal("0")


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ValueError, TypeError):
        return None


def _ratio(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    if a is None or b is None or b == _DECIMAL_0:
        return None
    return (a / b).quantize(Decimal("0.0001"))


def _all_entries(
    facts: dict[str, Any], primary: str, fallbacks: tuple[str, ...]
) -> list[dict[str, Any]]:
    """Every fact record for a concept, merged across all units."""
    merged: list[dict[str, Any]] = []
    for tag in (primary, *fallbacks):
        node = facts.get(tag)
        if not node:
            continue
        for _unit, entries in (node.get("units") or {}).items():
            merged.extend(entries)
    return merged


def _latest_annual(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Latest fiscal-year entry; falls back to the most recent period."""
    annual = [e for e in entries if e.get("fp") == "FY"]
    pool = annual or entries
    return max(pool, key=lambda e: e.get("end") or "", default=None)


def _extract_concept(
    facts: dict[str, Any], primary: str, fallbacks: tuple[str, ...]
) -> dict[str, Any] | None:
    entries = _all_entries(facts, primary, fallbacks)
    best = _latest_annual(entries)
    if best is not None and best.get("val") is not None:
        return {
            "val": best["val"],
            "end": best.get("end"),
            "start": best.get("start"),
        }
    return None


class EdgarFundamentalProvider(FundamentalProvider):
    """Real fundamentals straight from SEC EDGAR company facts.

    - Resolves the ticker → CIK map once (cached in-process).
    - Fetches the full company-facts document per symbol (cached 12 h).
    - Computes margins/ratios from raw XBRL values and a live close price
      (used only for P/E and P/B).

    EDGAR asks for a descriptive User-Agent and ≤10 requests/second; the token
    bucket keeps us well under that.
    """

    def __init__(
        self,
        prices: MarketDataProvider | Callable[[], MarketDataProvider] | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        user_agent: str = "qtrader/0.1 (research@example.com)",
        facts_ttl_seconds: float = 12 * 3600,
    ) -> None:
        self._prices = prices
        self._client = client or httpx.AsyncClient(
            timeout=25.0,
            follow_redirects=True,
            headers={"User-Agent": user_agent},
        )
        self._owns_client = client is None
        self._limiter = TokenBucket(capacity=10, refill_rate_per_second=2.0, name="edgar")
        self._facts_ttl = facts_ttl_seconds
        self._tickers: dict[str, str] | None = None
        self._facts_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    async def _get(self, url: str) -> httpx.Response:
        await self._limiter.wait()
        response = await self._client.get(url)
        response.raise_for_status()
        return response

    @retry_async()
    async def _get_retried(self, url: str) -> httpx.Response:
        return await self._get(url)

    async def _ticker_map(self) -> dict[str, str]:
        if self._tickers is None:
            response = await self._get_retried(EDGAR_TICKERS_URL)
            payload = response.json()
            self._tickers = {
                str(row["ticker"]).upper(): str(row["cik_str"]).zfill(10)
                for row in payload.values()
            }
        return self._tickers

    async def _company_facts(self, cik: str) -> dict[str, Any]:
        now = asyncio.get_event_loop().time()
        cached = self._facts_cache.get(cik)
        if cached and now - cached[0] < self._facts_ttl:
            return cached[1]
        response = await self._get_retried(EDGAR_FACTS_URL.format(cik=cik))
        facts: dict[str, Any] = response.json().get("facts", {}).get("us-gaap", {}) or {}
        self._facts_cache[cik] = (now, facts)
        return facts

    async def _live_price(self, symbol: str) -> Decimal | None:
        if self._prices is None:
            return None
        provider = self._prices() if callable(self._prices) else self._prices
        try:
            bar = await provider.fetch_quote(symbol)
            return _dec(bar.close) if bar is not None else None
        except Exception as exc:  # pragma: no cover - defensive
            _logger.warning("edgar.quote_failed", symbol=symbol, error=str(exc))
            return None

    def _extract_values(self, facts: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, (primary, fallbacks) in CONCEPTS.items():
            hit = _extract_concept(facts, primary, fallbacks)
            out[key] = hit
        return out

    def _build(
        self,
        symbol: str,
        facts: dict[str, Any],
        price: Decimal | None,
    ) -> FundamentalData | None:
        values = self._extract_values(facts)
        revenue = _dec(values["revenue"] and values["revenue"]["val"])
        gross = _dec(values["gross_profit"] and values["gross_profit"]["val"])
        operating = _dec(values["operating_income"] and values["operating_income"]["val"])
        net = _dec(values["net_income"] and values["net_income"]["val"])
        equity = _dec(values["stockholders_equity"] and values["stockholders_equity"]["val"])
        assets = _dec(values["assets"] and values["assets"]["val"])
        debt_lt = _dec(values["long_term_debt"] and values["long_term_debt"]["val"])
        debt_st = _dec(values["short_term_debt"] and values["short_term_debt"]["val"])
        cash_flow = _dec(values["operating_cash_flow"] and values["operating_cash_flow"]["val"])
        eps = _dec(values["eps"] and values["eps"]["val"])
        shares = _dec(values["shares_outstanding"] and values["shares_outstanding"]["val"])

        if revenue is None or net is None:
            _logger.warning("edgar.incomplete", symbol=symbol)
            return None

        debt_total = None
        if debt_lt is not None and debt_st is not None:
            debt_total = debt_lt + debt_st
        elif debt_lt is not None:
            debt_total = debt_lt

        book_per_share = _ratio(equity, shares)

        report_end = values["net_income"] and values["net_income"].get("end")
        report_date = None
        try:
            if report_end:
                report_date = date.fromisoformat(report_end[:10])
        except ValueError:
            report_date = None

        revenue_growth: Decimal | None = None
        earnings_growth: Decimal | None = None
        revenue_entries = self._all_annual(facts, "Revenues", CONCEPTS["revenue"][1])
        net_entries = self._all_annual(facts, "NetIncomeLoss", CONCEPTS["net_income"][1])
        prev_rev = _dec(revenue_entries[1].get("val")) if len(revenue_entries) >= 2 else None
        prev_net = _dec(net_entries[1].get("val")) if len(net_entries) >= 2 else None
        if prev_rev is not None and revenue and prev_rev != _DECIMAL_0:
            revenue_growth = ((revenue - prev_rev) / abs(prev_rev)).quantize(Decimal("0.0001"))
        if prev_net is not None and net and prev_net != _DECIMAL_0:
            earnings_growth = ((net - prev_net) / abs(prev_net)).quantize(Decimal("0.0001"))

        return FundamentalData(
            symbol=symbol,
            period="annual",
            report_date=report_date,
            revenue=revenue,
            eps=eps,
            pe_ratio=_ratio(price, eps),
            debt_total=debt_total,
            cash_flow=cash_flow,
            roe=_ratio(net, equity),
            roa=_ratio(net, assets),
            gross_margin=_ratio(gross, revenue),
            operating_margin=_ratio(operating, revenue),
            net_margin=_ratio(net, revenue),
            revenue_growth=revenue_growth,
            earnings_growth=earnings_growth,
            price_to_book=_ratio(price, book_per_share),
        )

    def _all_annual(
        self, facts: dict[str, Any], primary: str, fallbacks: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        annual = sorted(
            (e for e in _all_entries(facts, primary, fallbacks) if e.get("fp") == "FY"),
            key=lambda e: e.get("end") or "",
            reverse=True,
        )
        return annual

    async def fetch_fundamentals(self, symbol: str) -> FundamentalData | None:
        symbol = symbol.upper()
        try:
            tickers = await self._ticker_map()
            cik = tickers.get(symbol)
            if not cik:
                _logger.warning("edgar.unknown_symbol", symbol=symbol)
                return None
            facts = await self._company_facts(cik)
            price = await self._live_price(symbol)
        except (httpx.HTTPError, ValueError) as exc:
            _logger.warning("edgar.fetch_failed", symbol=symbol, error=str(exc))
            return None
        return self._build(symbol, facts, price)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _seed(symbol: str) -> int:
    return int(hashlib.sha256(symbol.encode()).hexdigest(), 16)


class StubFundamentalProvider(FundamentalProvider):
    """Deterministic pseudo-fundamentals derived from a symbol hash.

    Synthetic only — kept as an offline fallback for tests and for when the
    EDGAR API is unreachable. Values are stable per symbol.
    """

    async def fetch_fundamentals(self, symbol: str) -> FundamentalData | None:
        _logger.warning("fundamental.stub_used", symbol=symbol)
        n = _seed(symbol)
        growth = (n % 200 - 80) / 100.0
        gross = 0.20 + (n % 30) / 100.0
        operating = gross - 0.06 - (n % 15) / 100.0
        net = operating - 0.02 - (n % 10) / 100.0
        revenue = Decimal((1_000_000_000 + n % 40_000_000_000) // 100) * 100
        eps = Decimal(n % 2000 - 300) / 100
        pe = Decimal(4 + (n // 7) % 60)
        pb = Decimal(0.5 + (n // 11) % 40) / 10
        return FundamentalData(
            symbol=symbol,
            period="annual",
            report_date=date.today() - timedelta(days=30),
            revenue=revenue,
            eps=eps,
            pe_ratio=pe,
            debt_total=revenue * Decimal("0.8") if (n % 3) else None,
            cash_flow=revenue * Decimal("0.06"),
            roe=Decimal("0.05") + Decimal(n % 3000) / 10000,
            roa=Decimal("0.02") + Decimal(n % 1500) / 10000,
            gross_margin=Decimal(str(round(gross, 4))),
            operating_margin=Decimal(str(round(operating, 4))),
            net_margin=Decimal(str(round(net, 4))),
            revenue_growth=Decimal(str(round(growth, 4))),
            earnings_growth=Decimal(str(round(growth + 0.05, 4))),
            price_to_book=pb,
        )


async def _demo() -> None:
    """Sanity check against live EDGAR (dev only)."""
    provider = EdgarFundamentalProvider()
    try:
        for symbol in ("AAPL", "MSFT", "TSLA"):
            data = await provider.fetch_fundamentals(symbol)
            _logger.info(
                "edgar.demo",
                symbol=symbol,
                revenue=data.revenue if data else None,
                net_margin=data.net_margin if data else None,
                pe=data.pe_ratio if data else None,
                report_date=data.report_date if data else None,
            )
    finally:
        await provider.close()


if __name__ == "__main__":
    asyncio.run(_demo())
