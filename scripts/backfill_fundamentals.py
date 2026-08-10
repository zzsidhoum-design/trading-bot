"""Backfill point-in-time fundamentals from SEC EDGAR companyfacts.

For each universe symbol: fetch the full companyfacts JSON once, extract:
  - annual (FY) income items   : revenue, gross profit, operating income,
    net income, diluted EPS, operating cash flow  -> known as of the 10-K `filed` date
  - point-in-time balance items: equity, assets, long/short debt, shares
  - derived ratios             : margins, ROE, ROA, growth (YoY, FY), debt ratios

Each snapshot carries `asof` (the filing date = when the market knew it) and
`period_end`. Output is a pickle cache consumed by the diagnostic sweep.

Fetch is cached under cache_dir per symbol so reruns are incremental.
"""

from __future__ import annotations

import asyncio
import os
import pickle
import sys
from datetime import date
from decimal import Decimal
from typing import Any

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

EDGAR_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
EDGAR_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
USER_AGENT = "qtrader/0.1 (research@example.com)"

INCOME_CONCEPTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "revenue": ("Revenues", ("RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet")),
    "gross_profit": ("GrossProfit", ("SalesRevenueNet",)),
    "operating_income": ("OperatingIncomeLoss", ("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",)),
    "net_income": ("NetIncomeLoss", ("ProfitLoss",)),
    "eps": ("EarningsPerShareDiluted", ("EarningsPerShareBasic",)),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities", ("NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",)),
}

BALANCE_CONCEPTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "equity": ("StockholdersEquity", ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",)),
    "assets": ("Assets", ()),
    "debt_lt": ("LongTermDebt", ("LongTermDebtNoncurrent",)),
    "debt_st": ("ShortTermBorrowings", ("DebtCurrent",)),
    "shares": ("CommonStockSharesOutstanding", ("CommonStockSharesOutstandingIssued",)),
}


def _all_entries(facts: dict[str, Any], primary: str, fallbacks: tuple[str, ...]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for tag in (primary, *fallbacks):
        node = facts.get(tag)
        if not node:
            continue
        for _unit, entries in (node.get("units") or {}).items():
            merged.extend(entries)
    return merged


def _first_by_end(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """One entry per period-end; keep the EARLIEST filing (first disclosure)."""
    best: dict[str, dict[str, Any]] = {}
    for e in entries:
        end = e.get("end")
        if not end:
            continue
        filed = e.get("filed") or ""
        if end not in best or filed < best[end].get("filed", ""):
            best[end] = e
    return best


def _full_year_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Income entries covering a full fiscal year (window >= 350 days)."""
    out = []
    for e in entries:
        start, end = e.get("start"), e.get("end")
        if not start or not end:
            continue
        try:
            days = (date.fromisoformat(end[:10]) - date.fromisoformat(start[:10])).days
        except ValueError:
            continue
        if days >= 350:
            out.append(e)
    return out


def _as_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def build_snapshots(symbol: str, facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Point-in-time snapshots keyed by disclosure (asof, period_end).

    Balance items are point-in-time; income items use full-year (FY) values so
    YTD-cumulative 10-Q income never pollutes the feature. Restatements are
    collapsed to the first filing date that disclosed each period.
    """
    balance_by_end: dict[str, dict[str, dict[str, Any]]] = {}
    us_gaap = facts.get("us-gaap", {}) or {}
    dei = facts.get("dei", {}) or {}
    for key, (primary, fallbacks) in BALANCE_CONCEPTS.items():
        if key == "shares":
            continue
        balance_by_end[key] = _first_by_end(_all_entries(us_gaap, primary, fallbacks))

    # Shares: us-gaap outstanding -> issued -> dei cover-page share count.
    share_entries = _all_entries(us_gaap, "CommonStockSharesOutstanding", ())
    share_entries += _all_entries(us_gaap, "CommonStockSharesIssued", ())
    share_entries += _all_entries(dei, "EntityCommonStockSharesOutstanding", ())
    balance_by_end["shares"] = _first_by_end(share_entries)

    annual_income_by_end: dict[str, dict[str, Any]] = {}
    for key, (primary, fallbacks) in INCOME_CONCEPTS.items():
        annual_income_by_end[key] = _first_by_end(
            _full_year_entries(_all_entries(us_gaap, primary, fallbacks))
        )

    # Filing events: any (end, filed) disclosed by a balance item or FY income.
    filing_keys: set[tuple[str, str]] = set()
    for by_end in balance_by_end.values():
        for end, e in by_end.items():
            if e.get("filed"):
                filing_keys.add((end, e["filed"]))
    for end, e in annual_income_by_end.items():
        if e.get("filed"):
            filing_keys.add((end, e["filed"]))

    snapshots: list[dict[str, Any]] = []
    for end, filed in sorted(filing_keys, key=lambda k: k[1]):
        end_date = _as_date(end)
        asof = _as_date(filed)
        if end_date is None or asof is None or asof < date(2019, 1, 1):
            continue
        snap: dict[str, Any] = {"symbol": symbol, "asof": asof, "period_end": end_date}

        for concept, by_end in balance_by_end.items():
            hit = by_end.get(end)
            if hit is not None:
                snap[concept] = hit.get("val")

        for concept, by_end in annual_income_by_end.items():
            hit = by_end.get(end)
            if hit is not None:
                snap[concept] = hit.get("val")
                snap[f"{concept}_fy"] = hit.get("fy")
        snapshots.append(snap)

    # Carry annual income forward across balance-only filings until the next 10-K.
    snapshots.sort(key=lambda s: s["asof"])
    last_income: dict[str, Any] = {}
    for snap in snapshots:
        changed = False
        for concept in INCOME_CONCEPTS:
            if concept in snap:
                last_income[concept] = snap[concept]
                last_income[f"{concept}_fy"] = snap[f"{concept}_fy"]
                changed = True
        if not changed:
            for concept in INCOME_CONCEPTS:
                if concept in last_income:
                    snap[concept] = last_income[concept]
                    snap[f"{concept}_fy"] = last_income[f"{concept}_fy"]

    # Carry latest-known balance items forward too, so a shares-only filing (dei
    # cover-page count) still carries the most recent equity / debt / assets.
    last_bal: dict[str, Any] = {}
    for snap in snapshots:
        for concept in BALANCE_CONCEPTS:
            if concept in snap:
                last_bal[concept] = snap[concept]
            elif concept in last_bal:
                snap[concept] = last_bal[concept]
    return snapshots


def derive(snap: dict[str, Any]) -> dict[str, Any]:
    d = Decimal
    val = lambda k: snap.get(k)
    num = lambda v: d(str(v)) if v is not None else None

    revenue = num(val("revenue"))
    gross = num(val("gross_profit"))
    operating = num(val("operating_income"))
    net = num(val("net_income"))
    equity = num(val("equity"))
    assets = num(val("assets"))
    debt_lt = num(val("debt_lt"))
    debt_st = num(val("debt_st"))
    shares = num(val("shares"))
    eps = num(val("eps"))

    debt_total = None
    if debt_lt is not None and debt_st is not None:
        debt_total = debt_lt + debt_st
    elif debt_lt is not None:
        debt_total = debt_lt

    def ratio(a, b):
        if a is None or b is None or b == 0:
            return None
        return a / b

    out: dict[str, Any] = {
        "symbol": snap["symbol"],
        "asof": snap["asof"],
        "period_end": snap["period_end"],
        "revenue": revenue,
        "net_income": net,
        "eps": eps,
        "equity": equity,
        "assets": assets,
        "debt_total": debt_total,
        "cash_flow": num(val("operating_cash_flow")),
        "shares": shares,
        "gross_margin": ratio(gross, revenue),
        "operating_margin": ratio(operating, revenue),
        "net_margin": ratio(net, revenue),
        "roe": ratio(net, equity),
        "roa": ratio(net, assets),
        "debt_to_equity": ratio(debt_total, equity),
        "debt_to_assets": ratio(debt_total, assets),
        "book_per_share": ratio(equity, shares),
        "fy": snap.get("revenue_fy"),
    }
    # YoY growth vs the prior fiscal-year snapshot (computed later over sorted FYs).
    return out


async def fetch_companyfacts(client: httpx.AsyncClient, cik: str) -> dict[str, Any]:
    r = await client.get(EDGAR_FACTS_URL.format(cik=cik))
    r.raise_for_status()
    return r.json().get("facts", {}) or {}


async def main(universe_csv: str, out_pkl: str) -> None:
    import pandas as pd

    uni = pd.read_csv(universe_csv)
    symbols = sorted(uni["symbol"].tolist())
    print(f"universe: {len(symbols)} symbols", flush=True)

    cache_dir = os.path.join(os.path.dirname(out_pkl), "edgar_cache")
    os.makedirs(cache_dir, exist_ok=True)

    async with httpx.AsyncClient(
        timeout=40.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ) as client:
        tickers = (await client.get(EDGAR_TICKERS_URL)).json()
        cik_map = {str(r["ticker"]).upper(): str(r["cik_str"]).zfill(10) for r in tickers.values()}
        print(f"ticker map: {len(cik_map)}", flush=True)

        snapshots: list[dict[str, Any]] = []
        missed: list[str] = []
        empty: list[str] = []
        for i, symbol in enumerate(symbols):
            cache_path = os.path.join(cache_dir, f"{symbol}.json")
            facts: dict[str, Any] | None = None
            if os.path.exists(cache_path):
                with open(cache_path, "r", encoding="utf-8") as f:
                    cached = __import__("json").load(f)
                if "us-gaap" in cached:
                    facts = cached
                else:
                    os.remove(cache_path)  # stale us-gaap-only cache
            if facts is None:
                cik = cik_map.get(symbol)
                if cik is None:
                    missed.append(symbol)
                    continue
                try:
                    facts = await fetch_companyfacts(client, cik)
                except httpx.HTTPError as exc:
                    print(f"  {symbol}: fetch failed: {exc}", flush=True)
                    missed.append(symbol)
                    continue
                with open(cache_path, "w", encoding="utf-8") as f:
                    __import__("json").dump(facts, f)
            snaps = build_snapshots(symbol, facts)
            if not snaps:
                empty.append(symbol)
                continue
            snapshots.extend(derive(s) for s in snaps)
            if (i + 1) % 25 == 0:
                print(f"  {i + 1}/{len(symbols)} symbols, {len(snapshots)} snapshots", flush=True)

    df = pd.DataFrame(snapshots)
    for col in df.columns:
        if col in {"symbol", "asof", "period_end", "fy"}:
            continue
        df[col] = pd.to_numeric(df[col], errors="coerce")
    # YoY growth over the fiscal-year series per symbol.
    fy = df.dropna(subset=["fy"]).copy()
    fy = fy.sort_values(["symbol", "fy"])
    for col in ("revenue", "net_income"):
        gname = "revenue_growth" if col == "revenue" else "earnings_growth"
        fy[gname] = fy.groupby("symbol")[col].pct_change()
    fy = fy.drop_duplicates(subset=["symbol", "asof", "fy"])
    df = df.merge(fy[["symbol", "asof", "fy", "revenue_growth", "earnings_growth"]], on=["symbol", "asof", "fy"], how="left")
    df = df.drop_duplicates(subset=["symbol", "asof", "period_end"])

    df.to_pickle(out_pkl)
    print(f"\nsaved {len(df)} snapshots -> {out_pkl}")
    print(f"symbols covered: {df['symbol'].nunique()} / {len(symbols)}")
    print(f"missed (no CIK): {len(missed)} {missed[:10]}")
    print(f"empty (no facts): {len(empty)} {empty[:10]}")
    if not df.empty:
        print("asof range:", df["asof"].min(), "..", df["asof"].max())
        print(df[["symbol", "asof", "period_end", "fy", "revenue", "roe", "net_margin", "revenue_growth"]].tail(12).to_string(index=False))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
