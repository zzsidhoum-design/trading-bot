# Baseline Freeze Record v2 — Fixed Engine (Phase 1)

Date: 2026-08-08
Git commit: `fcb51dd` (main) — restored from the dirty worktree that had reverted it.
Suite: **340 passed** (315 unit + 25 integration, `QTRADER_RUN_INTEGRATION=1`), ruff clean, mypy clean (108 files).
Mode: `backtest` (`QTRADER_MODE=backtest`, `ENABLE_LIVE_TRADING=false`) — live trading disabled, paper broker.

This is the immutable baseline for the Phase 1–20 audit. It differs from the
first freeze (`f7cff7b`, `docs/audit/01-baseline-freeze.md`) in that the engine
now carries the `fcb51dd` remediation (real 1% risk sizing aligned to the 3%
bracket, re-enabled exposure/sector/daily-loss/cooldown/trade-count limits,
dollar-weighted profit factor). All subsequent repairs must be measured against
**these** numbers.

## 1. Persisted performance (strategy_performance)

| id | strategy | period | trades | win rate | PF | Sharpe | total return | max DD |
|---|---|---|---|---|---|---|---|---|
| 143 | walk-forward | 2025-07-01..2026-07-31 | 636 | 35.85% | 1.0027 | 0.412 | +3.34% | −7.29% |
| 144 | technical | 2025-07-01..2026-07-31 | 442 | 31.00% | 0.8665 | −0.819 | −12.65% | −21.04% |
| 145 | walk-forward | 2021-07-01..2026-07-31 | 477 | 38.78% | 1.1949 | 0.327 | +9.23% | −6.24% |

Note: rows 143–145 were persisted by the **pre-fix** engine (the only rows in
the table; no fixed-engine run has been persisted because every candidate was
gate-DENIED and DENIED runs write nothing).

Gate status (system_logs, `system_gate`, last id 2671): **DENIED for ensemble/paper** —
`win rate 38.78% < min 39% (derived)`, `profit factor 1.19 < min 1.20`, `sharpe 0.33 < min 1.00`.

The 40.1% win rate quoted in the audit brief is **not persisted**; it matches the
last OOS experiment aggregate (reversal-canonicalized features, uncalibrated,
threshold 0.55/0.45): 494 trades, win 40.08%, PF 1.223, Sharpe −0.26, return −5.9%.
The `fcb51dd` fixed-engine re-runs gave: calibrated 67 trades / win 40.30% /
PF 1.210 / Sharpe 0.30 / +8.61% (DENIED, Sharpe); uncalibrated 123 trades /
win 40.65% / PF 0.995 / Sharpe 0.011 / −0.94% (DENIED, PF/Sharpe/ret).

## 2. Configuration (`.env` + settings defaults)

- Watchlist / backtest universe: `AAPL,MSFT,TSLA,NVDA,AMZN,GOOGL` (6 mega-caps); live backtest run used the 502-name S&P 500 pull.
- `SCAN_MIN_ATR_PCT=0.1`, `SCAN_TOP_K=10`; `BACKTEST_LOOKBACK_DAYS=500`; backtest interval `1d`.
- Broker `paper`; data provider Yahoo (`YAHOO_ENABLED` commented → default on); fundamentals EDGAR (keyless); news RSS (Google/Yahoo); **no LLM key → news items carry no scored sentiment**.
- Decision weights: `technical 0.30, news 0.25, fundamental 0.20, prediction 0.25`; buy 0.15 / sell −0.15 / conflict 0.50 / min coverage 0.50.
- Gate (`.env`): `GATE_MIN_TRADES=30`, `GATE_MIN_PROFIT_FACTOR=1.20`, `GATE_MIN_SHARPE=1.00`, `GATE_MAX_DRAWDOWN=0.25`, `GATE_MIN_TOTAL_RETURN=0.0`; no `GATE_MIN_WIN_RATE` → derived R:R-aware floor `stop/(stop+target)+margin` = 33.33% + 6pp = **39.33%**.

## 3. Universe & dataset

- DB prices: **502 symbols, 624,820 daily bars**, interval `1d`, 2021-08-02..2026-08-06, `source=NULL`.
- Bar stamps: 09:30 ET open, stored in UTC → 13:30 UTC (winter, 414,001 bars) and 14:30 UTC (EDT, 210,819 bars) — DST offset, benign.
- Symbols with <800 bars (short listings, compress walk-forward folds): HONA 37, FDXF 50, Q 195, SNDK 371, GEV 592, SOLV 593, VLTO 712.
- **All 502 S&P names are `is_active=false`**; only 4 test fixtures are active (BTST/TSTB/TSTC/TSTD). Scheduler re-activates the watchlist at startup.
- No adjustment columns persisted; Yahoo `adjclose` fetched but **ignored** by the parser (splits/dividends not applied to stored OHLC).
- Current membership pull → **survivorship-biased** (no delisted names, no point-in-time membership).

## 4. Models (model_registry)

- Only `dash-momentum` v1..v6; v1 `is_active=true`; all `offline_metrics={accuracy:0.60}`, `hyperparams={coef:[0.1]}`, `training_window="200x5"` — **placeholder data**.
- Prediction agent requests `momentum` → no match → `HeuristicModel` fallback served all 2,569 persisted predictions (`version=0`).
- `LogisticModel.from_registered` would return `None` for registry rows (hyperparams lack `feature_names`/`coef` lists).
- Walk-forward gate trains its own per-fold logistic (lookback 60, horizon 12, 11 price features) + Platt calibration (20% temporal held-out slice).

## 5. Strategy / execution assumptions (fixed engine)

- `BacktestParams` defaults: stop 3% / target 6% (2:1 bracket), max_hold 0, trailing 0, warmup 30; backtest default commission 1bp / slippage 0bp; walk-forward default commission 1bp / slippage **5bp per side**.
- Fills: next bar's open; intrabar stop/target on bar range (stop-before-target resolved conservatively); forced-flat at test end (`outcome="end_of_test"`).
- Risk (`RiskPolicy`): risk 1% / trade, ATR 1.5× stop, 2R target, max 10 positions, 80% exposure cap, 40% per-sector cap, 5-min cooldown, 10 trades/day, 3% daily-loss cap. Now actually enforced in the backtest via `_SimContext` (exposure/sector/daily-PnL/cooldown/trade-count threaded live) and sizing off the real bracket stop (`entry × stop_loss_pct`).
- Profit factor: **dollar-weighted** (`gross_profit_$ / gross_loss_$` via `trade_pnl_amounts`); win rate still `(n − losses)/n` with **pnl==0 counted as a win**.
- ML trainer: horizon 12, lookback 120, min samples 100, promote ≥0.52, LR 0.5 / 200 epochs / L2 1e-3, Platt 500 epochs.
- Walk-forward: 5 folds, **bar-index aligned** (not calendar) — see `walk_forward.py:_make_folds`; `walk-forward` strategy label feeds the gate.
- Signal engine: `_SignalEngine` (backtest.py) decides on ML prob thresholds 0.52/0.48, or EMA(9/21) cross + RSI>70 with no model — it does **not** replay the live Chief ensemble.

## 6. Persisted activity snapshot (2026-08-06 run)

| table | rows | note |
|---|---|---|
| prices | 624,820 | 502 symbols, 1d |
| signals | 7,705 | technical 2570 / fundamental 2568 / news 2567; 0 STRONG_SELL |
| predictions | 2,569 | all `HeuristicModel` (momentum v0) |
| decision_log | 2,569 | full ensemble trace persisted |
| trades | **0** | no trade-level audit trail |
| orders | 4 | 2 FILLED (backtest-mode test symbols) |
| news | 125 | no LLM analysis (default sentiment) |
| fundamentals | **7** | watchlist mega-caps only |
| risk_history | **0** | no rejection history |
| indicators | 32 | 7 symbols |

## 7. Baseline audit hypotheses (starting point for this audit)

1. Backtest signal path ≠ live Chief ensemble → persisted backtest results do not test the decisions live would take.
2. Win-rate metric counts pnl==0 as a win; PF is now dollar-weighted but win rate is not.
3. News agent is inert without an LLM key; fundamental agent covers 7/502; neither is exercised by the backtest engine.
4. Universe is survivorship-biased (current S&P 500, no delisted names, no point-in-time membership).
5. Trained model is never used (name mismatch + non-reconstructable hyperparams); all predictions are heuristic.
6. Bar-index (not calendar) fold alignment compresses the true OOS window and misaligns periods across symbols.
7. Unadjusted OHLC corrupts split-date features and labels.

This document is immutable. All later phases reference these numbers.
