# Data, Universe & Agent Audit (Phases 2–4)

Date: 2026-08-07. Baseline commit: `f7cff7b`.
All numbers below were recomputed directly against the persisted `prices` table and
agent/decision tables in this audit session.

## 1. Price data integrity (Phase 2)

Source: `prices` (daily bars, timestamped 13:30 UTC). 502 symbols, 624,820 rows,
range 2021-08-02..2026-08-06.

| check | result |
|---|---|
| duplicate (symbol, ts) | 0 |
| high < low | 0 |
| high < open or high < close | 0 |
| low > open or low > close | **2** — GL, STE on 2026-08-06 (last bar; open above low on a live snapshot) |
| price <= 0 | 0 |
| volume <= 0 | **352** — all `SW` (Aug 2021–Aug 2022), i.e. one symbol's early bars |
| Saturday / Sunday bars | 0 |
| gap > 30 calendar days | 0 |
| \|single-day return\| > 50% | **16** |

### The 16 large single-day moves are corporate-action artifacts + genuine crashes

Stored OHLC is **unadjusted** (Yahoo `quote` fields; `adjclose` is fetched but not
applied). Split discontinuities therefore appear as ±50% "returns":

- Split/reorganization artifacts (price discontinuity): GL 2024-04-11 (104.93 → 49.17, −53%),
  ALGN 2025-07-31 (−37%), FISV 2025-10-29 (−44%), CVNA (reverse-split sequence:
  +61%, −43%, +56%), VRT 2022-02-23 (−37%), WST 2025-02-13 (−38%), HOOD 2021-08-04 (−33%).
- Genuine single-day crashes (real earnings/guidance shocks): NFLX 2022-04-20 (−35%),
  DXCM 2024-07-26 (−41%), SNPS 2025-09-10 (−36%), TTD 2025-08-08 (−39%),
  CNC 2025-07-02 (−40%), SMCI 2026-03-20 (−33%), ECHO 2025-08-26 (−41%).

Consequences:
- Any feature computed from raw closes (e.g. `ret_60`, `atr_pct`, `momentum_20`)
  is **corrupted on split dates** for the affected symbols. The split bars also
  poison the model's forward-return labels for training.
- The model had to learn around this corruption, or implicitly classify split
  discontinuities as "returns", degrading both features and labels.

## 2. Universe & survivorship (Phase 3)

- The 502 symbols are a **current S&P 500 pull**: no delisted names, no point-in-time
  membership, no corporate-event history beyond prices. Class shares (BRK-B, BF-B)
  are treated as separate names. **Survivorship bias confirmed** — backtest returns
  are measured only on today's index members, which overstates any historical signal.
- DB state inconsistency: **all 502 S&P names are `is_active=False`**; only 4 test
  fixtures (BTST, TSTB, TSTC, TSTD) are active. The scheduler re-activates the
  watchlist at startup, so the flag is not a reliable record of trading eligibility.
- Only 4 "active" symbols => the live pipeline's realistic trading universe is the
  6-symbol watchlist; the backtest used the full 502. Two different universes.

## 3. Agent & pipeline activity (Phase 4)

Evidence from persisted tables (last run 2026-08-06):

| component | persisted evidence |
|---|---|
| Signals | technical 2570, fundamental 2568, news 2567. Mix: 3084 BUY, 516 STRONG_BUY, 513 SELL, **0 STRONG_SELL**, 1522 HOLD, 2070 NEUTRAL |
| Predictions | 2569 rows, `model_name=momentum`, **version=0** — all from `HeuristicModel` |
| Decisions | 2569 `decision_log` rows with full ensemble trace (`weights` 0.30/0.25/0.20/0.25, buy 0.15/sell −0.15, conflict 0.50) |
| News | 125 items: 123 real headlines (Yahoo Finance, Stocktwits, CNBC, Seeking Alpha, …) + 2 test fixtures. Sentiment dist. −1:20, 0:75, +0.33:1, +1:29. **No LLM key → no schema-validated analysis; per-item sentiment is provider/default; test items carry +1.0** |
| Fundamentals | only **7 rows** (watchlist mega-caps). Fundamental signals for the other 495 symbols are default NEUTRAL (score 0.0) → that leg is effectively inert |
| Indicators | 32 rows / 7 symbols — technical agent computes on the fly, does not persist per-symbol history |
| Trades | **0 rows**. The backtest persists only aggregates (`strategy_performance`, `backtest_runs`) — no trade-level audit trail in DB |
| Orders / positions | 2 FILLED (backtest mode, test symbols) / 2 OPEN |
| backtest_runs | 1 row: `technical-oos-sp500`, 2025-07-01..2026-07-31, 442 trades, win 30.9955%, PF 0.8665, Sharpe −0.8195, ret −12.65%, maxDD −21.04%, commission 1bp, slippage 5bp — matches the `strategy_performance.technical` row |
| Model registry | 6 `dash-momentum` entries (v1 active, offline_metrics accuracy 0.60 for **all** versions), `hyperparams={coef:[0.1]}` — placeholder data |

### Findings that matter

1. **The trained model is never used.** The prediction agent requests model name
   `momentum`; the registry only holds `dash-momentum`. Result: `HeuristicModel`
   fallback (deterministic momentum formula) produced all 2569 predictions. Even if
   the name matched, `LogisticModel.from_registered` returns `None` because
   `hyperparams` lacks `feature_names`/`coef` lists (only `{coef:[0.1]}`).
2. **Backtest ≠ live.** The backtest `_SignalEngine` (backtest.py:212) decides on
   ML probability thresholds 0.52/0.48 — or, with no model, on **EMA(9/21)
   crossover + RSI>70** (backtest.py:248). It never runs the Chief ensemble
   (technical/news/fundamental/prediction weighted vote). So none of the persisted
   historical results (442 / 636 / 477 trades) test the decisions the live system
   would take.
3. **News leg quality is weak without an LLM.** Real headlines are persisted but
   carry default sentiment (0.0 / LOW / conf 0.5); only test fixtures show ±1.0.
   The news weight (0.25) in the live ensemble therefore mostly contributes noise.
4. **Fundamental leg is inert for the actual universe** (7/502 have data).
5. **STRONG_SELL never fires** (516 STRONG_BUY vs 0 STRONG_SELL) — asymmetric
   signal distribution across all agents.
6. **No trade-level audit records** — aggregate-only persistence makes post-hoc
   trade analysis and reconciliation impossible from the DB.

## 4. Answers updated for the audit brief

- The 40.1% headline OOS win rate is real but came from the reversal experiment
  aggregate (494 trades, win 40.08%, PF 1.223, Sharpe −0.26, return −5.9%) — it is
  **not** in the DB (see 01-baseline-freeze.md §1).
- The system has never traded live (paper broker, `ENABLE_LIVE_TRADING=false`,
  0 trades in `trades`, 2 backtest-mode fills).
