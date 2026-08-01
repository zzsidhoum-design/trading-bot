"""Fundamental scoring — pure function over FundamentalData (docs/02-agents.md §5).

Normalizes each metric into [-1, 1] contributions and combines them into a
composite score with a human-readable rating. No I/O, fully unit-testable.
"""

from __future__ import annotations

from decimal import Decimal

from qtrader.domain.entities import FundamentalData
from qtrader.domain.value_objects import SignalType


def _f(value: Decimal | None) -> float:
    return float(value) if value is not None else 0.0


def _clip(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def score_fundamentals(data: FundamentalData) -> tuple[float, SignalType, dict[str, float]]:
    sub: dict[str, float] = {}

    growth = _clip((_f(data.revenue_growth) + _f(data.earnings_growth)) / 2)
    sub["growth"] = round(growth, 4)

    margins = _clip(
        (_f(data.gross_margin) + _f(data.operating_margin) + _f(data.net_margin)) / 3 * 4
    )
    sub["margins"] = round(margins, 4)

    profitability = _clip((_f(data.roe) + _f(data.roa)) / 2 * 5)
    sub["profitability"] = round(profitability, 4)

    cash_flow = _f(data.cash_flow)
    debt = _f(data.debt_total)
    revenue = _f(data.revenue)
    leverage = 0.0
    if debt > 0:
        ratio = debt / revenue if revenue > 0 else 0.0
        leverage = _clip(1.0 - ratio)
    if cash_flow < 0:
        leverage -= 0.5
    sub["leverage"] = round(_clip(leverage), 4)

    pe = _f(data.pe_ratio)
    pb = _f(data.price_to_book)
    valuation = 0.0
    if pe > 0:
        valuation += _clip((1.0 - pe / 30.0) * 0.5)
    if pb > 0:
        valuation += _clip((1.0 - pb / 10.0) * 0.5)
    sub["valuation"] = round(_clip(valuation), 4)

    score = _clip(
        0.25 * growth + 0.2 * margins + 0.2 * profitability + 0.15 * leverage + 0.2 * valuation
    )
    sub["score"] = round(score, 4)

    if score >= 0.6:
        signal_type = SignalType.STRONG_BUY
    elif score >= 0.2:
        signal_type = SignalType.BUY
    elif score <= -0.6:
        signal_type = SignalType.STRONG_SELL
    elif score <= -0.2:
        signal_type = SignalType.SELL
    else:
        signal_type = SignalType.NEUTRAL
    return round(score, 4), signal_type, sub
