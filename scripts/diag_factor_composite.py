"""Factor-composite test: fixed long-short score, no ML fit.

Score = +xr(gross_margin) + xr(operating_margin) - xr(pb) - xr(log_mc)
        - xr(debt_to_equity) + xr(rev)   (xr = cross-sectional percentile)

A fixed rule has no fitted parameters, so evaluating on the full sample is
honest. Report per-horizon: IC, top/bottom decile fwd-return spread, and the
hit rate of the top decile vs base.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pandas as pd

from diag_prediction_signal import (
    FUND_COLS, MIN_HISTORY_BARS, REV_COLS,
    add_fundamentals, build_features, cross_sectional_ranks, load_bars,
)

SCORE_COLS = [
    "gross_margin", "operating_margin", "-pb", "-log_mc",
    "-debt_to_equity", "ret_20",
]

COMPONENTS = {
    "gross_margin": 1.0, "operating_margin": 1.0,
    "pb": -1.0, "log_mc": -1.0, "debt_to_equity": -1.0,
    "ret_20": 1.0,
}


def main() -> None:
    df = asyncio.run(load_bars())
    syms = df.groupby("symbol")["date"].nunique()
    syms = syms[syms >= MIN_HISTORY_BARS]
    df = df[df["symbol"].isin(syms.index)]
    feat = build_features(df)
    feat = add_fundamentals(feat)
    feat = cross_sectional_ranks(feat, list(COMPONENTS))
    feat = feat.dropna(subset=list(COMPONENTS))

    variants = {
        "quality": {"gross_margin": 1.0, "operating_margin": 1.0},
        "value": {"pb": -1.0, "log_mc": -1.0},
        "leverage": {"debt_to_equity": -1.0},
        "quality+value": {"gross_margin": 1.0, "operating_margin": 1.0,
                          "pb": -1.0, "log_mc": -1.0},
        "all": COMPONENTS,
    }
    print("components:", SCORE_COLS, f"rows {len(feat)}")
    for hname, comps in variants.items():
        for horizon in (63, 126):
            h = feat.copy()
            h["fwd_ret"] = h.groupby("symbol")["close"].shift(-horizon) / h["close"] - 1.0
            h = h.dropna(subset=["fwd_ret"])
            score = sum(w * h[f"xr_{c}"] for c, w in comps.items())
            h["score"] = score
            ics = []
            for _d, grp in h.groupby("date"):
                if len(grp) < 20:
                    continue
                r = grp[["score", "fwd_ret"]].corr().iloc[0, 1]
                if np.isfinite(r):
                    ics.append(r)
            ic = float(np.mean(ics))
            mkt = h.set_index("date")["fwd_ret"].groupby(level=0).mean()
            top = h[h["score"] >= h.groupby("date")["score"].transform(lambda s: s.quantile(0.9))]
            bot = h[h["score"] <= h.groupby("date")["score"].transform(lambda s: s.quantile(0.1))]
            top_m = top["fwd_ret"].mean() - top["date"].map(mkt).mean()
            bot_m = bot["fwd_ret"].mean() - bot["date"].map(mkt).mean()
            print(f"{hname:>12} h={horizon:>3}  n={len(h):>6}  IC={ic:.4f}  "
                  f"top-exc {top_m:+.4f}  bot-exc {bot_m:+.4f}  spread {top_m - bot_m:+.4f}")


if __name__ == "__main__":
    main()
