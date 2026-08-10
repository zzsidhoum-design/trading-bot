"""Diagnose the ML prediction signal: horizon x feature-set sweep.

Expanding walk-forward (12-bar embargo + purge), strictly out-of-sample.
For each (horizon, feature set) we report:
  AUC              : rank AUC of model prob vs true label (excess or raw)
  IC               : mean per-date Spearman rank correlation prob vs fwd ret
  buy_hit / base   : hit rate when prob_up >= 0.52 vs the block base rate

Feature sets:
  raw      : the production 11 features
  rank     : raw + cross-sectional percentile ranks (within-date)
  rev      : raw + short-term reversal features (ret_2, ret_3, 5d max-min)
  rank+rev : both

Labels: excess return vs equal-weight universe (market-relative).
"""

from __future__ import annotations

import asyncio

import numpy as np
import pandas as pd

from qtrader.application.services.model_trainer import fit_logistic

LOOKBACK = 120
PROB_BUY = 0.52
FOLDS = 5
EMBARGO = 12
MIN_HISTORY_BARS = 900

RAW_COLS = [
    "ret_1", "ret_5", "ret_10", "ret_20", "ret_60", "vol_20",
    "atr_pct", "volume_ratio", "range_ratio", "pos_in_range_20", "up_ratio_20",
]
REV_COLS = ["ret_2", "ret_3", "range_5"]
FUND_COLS = [
    "pe", "pb", "log_mc", "roe", "roa", "net_margin", "gross_margin",
    "operating_margin", "debt_to_equity", "revenue_growth", "earnings_growth",
]
FUND_PATH = r"C:\Users\User\AppData\Local\Temp\opencode\fundamentals.pkl"


async def load_bars() -> pd.DataFrame:
    from qtrader.config.settings import Settings

    import asyncpg

    settings = Settings()
    conn = await asyncpg.connect(settings.database_url.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        rows = await conn.fetch(
            """
            SELECT s.symbol AS symbol, p.ts AS ts, p.open, p.high, p.low,
                   p.close, p.volume
            FROM prices p JOIN stocks s ON s.id = p.stock_id
            WHERE p.interval = '1d'
            ORDER BY s.symbol, p.ts
            """
        )
    finally:
        await conn.close()
    df = pd.DataFrame([dict(r) for r in rows])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df["date"] = df["ts"].dt.date
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    return df


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    out: list[pd.DataFrame] = []
    for sym, g in df.groupby("symbol", sort=True):
        g = g.sort_values("date").reset_index(drop=True)
        c = g["close"]
        r1 = c.pct_change()
        f = pd.DataFrame(index=g.index)
        f["ret_1"] = r1
        f["ret_5"] = c.pct_change(5)
        f["ret_10"] = c.pct_change(10)
        f["ret_20"] = c.pct_change(20)
        f["ret_60"] = c.pct_change(60)
        f["ret_2"] = c.pct_change(2)
        f["ret_3"] = c.pct_change(3)
        f["vol_20"] = r1.rolling(20, min_periods=2).std(ddof=0)
        tr = pd.concat(
            [(g["high"] - g["low"]), (g["high"] - c.shift(1)).abs(), (g["low"] - c.shift(1)).abs()],
            axis=1,
        ).max(axis=1)
        f["atr_pct"] = tr.rolling(20, min_periods=1).mean() / c
        v = g["volume"]
        f["volume_ratio"] = v.rolling(5, min_periods=1).mean() / v.rolling(20, min_periods=5).mean()
        rng = (g["high"] - g["low"]) / c
        f["range_ratio"] = rng.rolling(20, min_periods=1).mean()
        f["range_5"] = rng.rolling(5, min_periods=1).mean()
        lo = g["low"].rolling(20, min_periods=1).min()
        hi = g["high"].rolling(20, min_periods=1).max()
        f["pos_in_range_20"] = (c - lo) / (hi - lo).replace(0, np.nan)
        f["up_ratio_20"] = (r1 > 0).rolling(20, min_periods=2).mean()
        f["symbol"] = sym
        f["date"] = g["date"]
        f["close"] = c
        out.append(f)
    feat = pd.concat(out, ignore_index=True)
    return feat


def add_labels(feat: pd.DataFrame, horizon: int) -> pd.DataFrame:
    feat = feat.sort_values(["symbol", "date"]).reset_index(drop=True)
    fwd = feat.groupby("symbol")["close"].shift(-horizon)
    feat["fwd_ret"] = fwd / feat["close"] - 1.0
    mkt = feat.groupby("date")["fwd_ret"].mean()
    feat["fwd_mkt"] = feat["date"].map(mkt)
    return feat


def cross_sectional_ranks(feat: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    for col in cols:
        feat[f"xr_{col}"] = feat.groupby("date")[col].rank(pct=True)
    return feat


def add_fundamentals(feat: pd.DataFrame, path: str = FUND_PATH) -> pd.DataFrame:
    """Point-in-time as-of join of EDGAR snapshots onto each (symbol, date).

    Valuation features (pe, pb, log_mc) use the decision-date close against the
    latest disclosed EPS / book / share count, so nothing leaks forward.
    """
    fund = pd.read_pickle(path)
    fund = fund[["symbol", "asof", "eps", "book_per_share", "shares", "roe", "roa",
                 "net_margin", "gross_margin", "operating_margin",
                 "debt_to_equity", "revenue_growth", "earnings_growth"]]
    fund["asof"] = pd.to_datetime(fund["asof"])
    fund = fund.sort_values("asof")

    feat = feat.copy()
    feat["dts"] = pd.to_datetime(feat["date"])
    feat = feat.sort_values("dts")
    merged = pd.merge_asof(
        feat, fund, left_on="dts", right_on="asof", by="symbol", direction="backward"
    )
    merged["pe"] = merged["close"] / merged["eps"]
    merged["pb"] = merged["close"] / merged["book_per_share"]
    merged["log_mc"] = np.log(merged["shares"] * merged["close"])
    return merged.drop(columns=["dts", "asof"])


def auc(y: np.ndarray, p: np.ndarray) -> float:
    order = np.argsort(p)
    ranks = np.empty_like(order)
    ranks[order] = np.arange(1, len(p) + 1)
    n1 = int(y.sum())
    n0 = len(y) - n1
    if n0 == 0 or n1 == 0:
        return float("nan")
    return (ranks[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


def run_sweep(feat: pd.DataFrame, date_index: pd.Index, feature_cols: list[str], label: str) -> dict:
    n = len(date_index)
    block = (n - EMBARGO) // FOLDS
    all_y: list[int] = []
    all_p: list[float] = []
    folds: list[dict] = []
    for k in range(FOLDS):
        train_end = EMBARGO + k * block
        test_start = train_end + EMBARGO
        test_end = test_start + block if k < FOLDS - 1 else n
        if test_end - test_start < 60 or train_end <= LOOKBACK + 12:
            continue
        train = feat[(feat["date"] >= date_index[0]) & (feat["date"] <= date_index[train_end - 1])]
        purge_until = test_start - 12
        train = train[train["date"] <= date_index[purge_until]]
        test = feat[(feat["date"] >= date_index[test_start]) & (feat["date"] <= date_index[test_end - 1])]
        train = train[train["fwd_ret"].notna()]
        test = test[test["fwd_ret"].notna()]
        train = train[train[feature_cols].notna().all(axis=1)]
        test = test[test[feature_cols].notna().all(axis=1)]
        active = [c for c in feature_cols if train[c].std(ddof=0) > 1e-12]
        if len(active) < 2 or len(train) < 1000 or len(test) < 200:
            continue
        if label == "excess":
            yt = (train["fwd_ret"] > train["fwd_mkt"]).astype(int).tolist()
            ye = (test["fwd_ret"] > test["fwd_mkt"]).astype(int).tolist()
        else:
            yt = (train["fwd_ret"] > 0).astype(int).tolist()
            ye = (test["fwd_ret"] > 0).astype(int).tolist()
        fit = fit_logistic(train[active].values.tolist(), yt)
        if fit is None:
            continue
        x = test[active].to_numpy(dtype=float)
        mean = np.asarray(fit["mean"]); std = np.asarray(fit["std"])
        denom = std.copy(); denom[denom < 1e-9] = 1.0
        logit = (x - mean) / denom @ np.asarray(fit["coef"]) + float(fit["intercept"])
        prob = 1.0 / (1.0 + np.exp(-np.clip(logit, -50, 50)))
        yy = np.asarray(ye)
        test = test.copy()
        test["prob"] = prob

        def _spearman(a: np.ndarray, b: np.ndarray) -> float:
            a_r = a.argsort().argsort().astype(float)
            b_r = b.argsort().argsort().astype(float)
            am = a_r.mean(); bm = b_r.mean()
            num = ((a_r - am) * (b_r - bm)).sum()
            den = np.sqrt(((a_r - am) ** 2).sum() * ((b_r - bm) ** 2).sum())
            return float(num / den) if den else 0.0

        per_date_ic = np.mean(
            [
                _spearman(g["prob"].to_numpy(), g["fwd_ret"].to_numpy())
                for _, g in test.groupby("date")
                if len(g) > 2
            ]
        )
        folds.append({"fold": k, "n": int(len(yy)), "auc": round(auc(yy, prob), 4),
                      "ic": round(float(per_date_ic), 4),
                      "base": round(float(yy.mean()), 4),
                      "buy_hit": round(float(yy[prob >= PROB_BUY].mean()), 4),
                      "n_buy": int((prob >= PROB_BUY).sum())})
        all_y.extend(ye)
        all_p.extend(prob.tolist())
    ay, ap = np.asarray(all_y), np.asarray(all_p)
    return {
        "n": len(ay), "auc": round(auc(ay, ap), 4),
        "ic": round(float(np.mean([f["ic"] for f in folds])), 4),
        "base": round(float(ay.mean()), 4),
        "buy_hit": round(float(ay[ap >= PROB_BUY].mean()), 4),
        "n_buy": int((ap >= PROB_BUY).sum()),
        "folds": folds,
    }


def main() -> None:
    df = asyncio.run(load_bars())
    syms = df.groupby("symbol")["date"].nunique()
    syms = syms[syms >= MIN_HISTORY_BARS]
    print(f"universe: {len(syms)} symbols")
    df = df[df["symbol"].isin(syms.index)]
    feat = build_features(df)
    feat = add_fundamentals(feat)
    feat = cross_sectional_ranks(feat, RAW_COLS + REV_COLS + FUND_COLS)
    date_index = pd.Index(sorted(feat["date"].unique()))

    feature_sets = {
        "raw": RAW_COLS,
        "rank": RAW_COLS + [f"xr_{c}" for c in RAW_COLS],
        "rev": RAW_COLS + REV_COLS,
        "rank+rev": RAW_COLS + REV_COLS + [f"xr_{c}" for c in RAW_COLS],
        "fund": FUND_COLS,
        "raw+fund": RAW_COLS + FUND_COLS,
        "all": RAW_COLS + REV_COLS + FUND_COLS
            + [f"xr_{c}" for c in RAW_COLS + FUND_COLS],
    }

    print(f"dates: {len(date_index)} ({date_index[0]} .. {date_index[-1]})")
    print("\nAUC / IC / buy_hit (excess-return labels), all OOS")
    print(f"{'horizon':>8} {'feats':>10} {'n':>7} {'AUC':>6} {'IC':>7} {'base':>7} {'buy_hit':>8} {'n_buy':>6}")
    best: list[tuple[float, int, str]] = []
    for horizon in (1, 5, 12, 21, 63):
        h = add_labels(feat.copy(), horizon)
        for name, cols in feature_sets.items():
            res = run_sweep(h, date_index, cols, "excess")
            print(f"{horizon:>8} {name:>10} {res['n']:>7} {res['auc']:>6} {res['ic']:>7.4f} {res['base']:>7} {res['buy_hit']:>8} {res['n_buy']:>6}")
            best.append((res["auc"] - 0.5, horizon, name))
    best.sort(reverse=True)
    print("\nbest AUC-0.5:", best[:6])


if __name__ == "__main__":
    main()
