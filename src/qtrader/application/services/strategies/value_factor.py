"""Cross-sectional value-factor strategy.

Ranks the tradable universe each ``rebalance_bars`` by
``score = -rank(pb) - rank(log_market_cap)`` (cheap, small) and signals the top
``quantile`` fraction with EVENT_BUY on rebalance bars, everything else with
EVENT_SELL, and HOLD between rebalances — so the backtest engine rebalances the
portfolio to the value tilt on a fixed cadence and never churns intra-period.

Point-in-time discipline: fundamentals are joined as-of the filing's disclosure
date (``asof <= bar date``); ``pb`` and ``log_mc`` use the decision-date close,
so nothing leaks forward. Symbols with no valid (pb, log_mc) at a bar are
excluded from that bar's ranking (and, having never been buyable, are never
held), so no stale position can accumulate.

Consumes the same ``model_outputs`` contract as the ML strategy (0.5 HOLD,
>=0.52 BUY, <=0.48 SELL), so execution, costs and risk sizing are identical.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from qtrader.application.services.strategies.base import (
    EVENT_BUY,
    EVENT_SELL,
    HOLD,
    Strategy,
    StrategyInputs,
)


class ValueFactorStrategy(Strategy):
    """Long the cheapest, smallest names in the universe, rebalanced regularly."""

    name = "value_factor"
    kind = "factor"

    def __init__(
        self,
        fundamentals: pd.DataFrame,
        rebalance_bars: int = 63,
        quantile: float = 0.15,
    ) -> None:
        if not isinstance(fundamentals, pd.DataFrame):
            raise TypeError("fundamentals must be a pandas DataFrame")
        required = {"symbol", "asof", "book_per_share", "shares"}
        missing = required - set(fundamentals.columns)
        if missing:
            raise ValueError(f"fundamentals missing columns: {sorted(missing)}")
        if not 0.0 < quantile < 1.0:
            raise ValueError("quantile must be in (0, 1)")
        self._fundamentals = fundamentals[["symbol", "asof", "book_per_share", "shares"]]
        self._rebalance_bars = int(rebalance_bars)
        self._quantile = quantile

    def generate_probs(self, inputs: StrategyInputs) -> dict[str, dict[datetime, float]]:
        rows: list[pd.DataFrame] = []
        for symbol, bars in inputs.oos.items():
            if not bars:
                continue
            frame = pd.DataFrame(
                {
                    "symbol": symbol,
                    "ts": [b.ts for b in bars],
                    "close": [float(b.close) for b in bars],
                }
            )
            rows.append(frame)
        if not rows:
            return {}
        bars = pd.concat(rows, ignore_index=True)
        bars["dts"] = pd.to_datetime(bars["ts"], utc=True).astype("datetime64[ns, UTC]")
        bars = bars.sort_values("dts")

        fund = self._fundamentals.copy()
        fund["asof"] = pd.to_datetime(fund["asof"], utc=True).astype("datetime64[ns, UTC]")
        fund = fund.sort_values("asof")

        merged = pd.merge_asof(
            bars, fund, left_on="dts", right_on="asof", by="symbol", direction="backward"
        )
        merged["pb"] = merged["close"] / merged["book_per_share"]
        merged["log_mc"] = np.log(merged["shares"] * merged["close"])
        merged["date"] = merged["dts"].dt.date
        merged = merged[np.isfinite(merged["pb"]) & np.isfinite(merged["log_mc"])]

        rebalance = self._rebalance_bars
        if rebalance <= 0:
            merged["reb"] = True
        else:
            all_dates = np.sort(merged["date"].unique())
            reb_dates = set(all_dates[::rebalance].tolist())
            merged["reb"] = merged["date"].isin(reb_dates)

        sig = merged[merged["reb"]]
        sig["xr_pb"] = sig.groupby("date")["pb"].rank(pct=True)
        sig["xr_mc"] = sig.groupby("date")["log_mc"].rank(pct=True)
        sig["score"] = -(sig["xr_pb"] + sig["xr_mc"])
        threshold = sig.groupby("date")["score"].transform(lambda s: s.quantile(1 - self._quantile))
        sig["selected"] = sig["score"] >= threshold
        merged = merged.merge(
            sig[["symbol", "date", "selected"]], on=["symbol", "date"], how="left"
        )

        out: dict[str, dict[datetime, float]] = {}
        for symbol, group in merged.groupby("symbol"):
            probs: dict[datetime, float] = {}
            for row in group.itertuples():
                if not row.reb:
                    prob = HOLD
                elif row.selected:
                    prob = EVENT_BUY
                else:
                    prob = EVENT_SELL
                probs[row.ts] = prob
            out[symbol] = probs
        return out

    def probs_for_symbol(
        self, inputs: StrategyInputs, symbol: str
    ) -> dict[datetime, float]:
        raise NotImplementedError(
            "ValueFactorStrategy is cross-sectional; use generate_probs()."
        )
