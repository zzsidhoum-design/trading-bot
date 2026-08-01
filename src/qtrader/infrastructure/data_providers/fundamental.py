"""Fundamental data providers (docs/02-agents.md §5).

``StubFundamentalProvider`` is a deterministic offline source so the pipeline and
tests work without paid data APIs. A real provider (e.g. Polygon/Yahoo) can
implement the same port later.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from decimal import Decimal

from qtrader.domain.entities import FundamentalData
from qtrader.domain.ports import FundamentalProvider


def _seed(symbol: str) -> int:
    return int(hashlib.sha256(symbol.encode()).hexdigest(), 16)


class StubFundamentalProvider(FundamentalProvider):
    """Deterministic pseudo-fundamentals derived from a symbol hash.

    Values are stable per symbol so scores and tests are reproducible.
    """

    async def fetch_fundamentals(self, symbol: str) -> FundamentalData | None:
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
