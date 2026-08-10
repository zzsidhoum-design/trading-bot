"""Decompose the fundamental edge: per-fold stability + per-feature AUC@63d."""

from __future__ import annotations

import asyncio

import numpy as np
import pandas as pd

from diag_prediction_signal import (
    FUND_COLS, MIN_HISTORY_BARS, RAW_COLS, REV_COLS,
    add_fundamentals, add_labels, auc, build_features, cross_sectional_ranks,
    load_bars, run_sweep,
)

HORIZON = 63


def main() -> None:
    df = asyncio.run(load_bars())
    syms = df.groupby("symbol")["date"].nunique()
    syms = syms[syms >= MIN_HISTORY_BARS]
    df = df[df["symbol"].isin(syms.index)]
    feat = build_features(df)
    feat = add_fundamentals(feat)
    feat = cross_sectional_ranks(feat, FUND_COLS)
    date_index = pd.Index(sorted(feat["date"].unique()))
    h = add_labels(feat.copy(), HORIZON)

    sets = {
        "fund": FUND_COLS,
        "xr_fund": [f"xr_{c}" for c in FUND_COLS],
        "raw+fund": RAW_COLS + FUND_COLS,
        "all+fund": RAW_COLS + REV_COLS + FUND_COLS + [f"xr_{c}" for c in FUND_COLS],
    }
    print(f"horizon {HORIZON}d, excess labels, {len(date_index)} dates")
    for name, cols in sets.items():
        res = run_sweep(h.copy(), date_index, cols, "excess")
        print(f"\n{name}: AUC {res['auc']} IC {res['ic']} base {res['base']} "
              f"buy_hit {res['buy_hit']} n {res['n']}")
        for i, f in enumerate(res["folds"]):
            print(f"  fold {i}->{i + 1}:", f)

    print("\nper-feature univariate AUC@63 (mean over folds)")
    for col in FUND_COLS:
        res = run_uni(h.copy(), date_index, col)
        print(f"  {col:>18} AUC {res['auc']} IC {res['ic']} n {res['n']}")


def run_uni(feat: pd.DataFrame, date_index: pd.Index, col: str) -> dict:
    """Single-feature purged walk-forward, mirrors run_sweep's fold split."""
    from diag_prediction_signal import EMBARGO, FOLDS
    from qtrader.application.services.model_trainer import fit_logistic

    n = len(date_index)
    block = (n - EMBARGO) // FOLDS
    all_y: list[int] = []
    all_p: list[float] = []
    for k in range(FOLDS):
        train_end = EMBARGO + k * block
        test_start = train_end + EMBARGO
        test_end = test_start + block if k < FOLDS - 1 else n
        if test_end - test_start < 60:
            continue
        train = feat[feat["date"] <= date_index[train_end - 1]]
        train = train[train["date"] <= date_index[test_start - 12]]
        test = feat[(feat["date"] >= date_index[test_start]) & (feat["date"] <= date_index[test_end - 1])]
        train = train[train["fwd_ret"].notna() & train[col].notna()]
        test = test[test["fwd_ret"].notna() & test[col].notna()]
        if len(train) < 1000 or len(test) < 200 or train[col].std(ddof=0) < 1e-12:
            continue
        yt = (train["fwd_ret"] > train["fwd_mkt"]).astype(int).tolist()
        ye = (test["fwd_ret"] > test["fwd_mkt"]).astype(int).tolist()
        fit = fit_logistic(train[[col]].values.tolist(), yt)
        if fit is None:
            continue
        x = test[[col]].to_numpy(dtype=float)
        mean = float(fit["mean"][0]); std = max(float(fit["std"][0]), 1e-9)
        logit = (x[:, 0] - mean) / std * float(fit["coef"][0]) + float(fit["intercept"])
        prob = 1.0 / (1.0 + np.exp(-np.clip(logit, -50, 50)))
        yy = np.asarray(ye)
        all_y.extend(yy.tolist())
        all_p.extend(prob.tolist())
    ay, ap = np.asarray(all_y), np.asarray(all_p)
    if len(ay) == 0 or ay.sum() == 0 or (ay == 0).sum() == 0:
        return {"auc": float("nan"), "ic": float("nan"), "n": 0}
    r = pd.Series(ap).rank().to_numpy()
    ic = float(np.corrcoef(r, ay)[0, 1])
    return {"auc": round(auc(ay, ap), 4), "ic": round(ic, 4), "n": int(len(ay))}


if __name__ == "__main__":
    main()
