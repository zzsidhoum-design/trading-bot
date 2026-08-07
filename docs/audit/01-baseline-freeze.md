# Baseline Freeze Record — Pre-Audit Snapshot

Date: 2026-08-07
Git commit: `f7cff7b` (main)
Suite: 335 passed (309 unit + 26 integration), ruff clean, mypy clean (108 files).
Mode: `backtest` (`QTRADER_MODE=backtest`, `ENABLE_LIVE_TRADING=false`) — live trading disabled.

This document freezes the system state that produced the current performance
before any audit-driven change. It is immutable: subsequent repairs must be
measured against these numbers.

## 1. Persisted performance (strategy_performance)

| strategy | mode | period | trades | win rate | PF | Sharpe | total return | max DD |
|---|---|---|---|---|---|---|---|---|
| technical | backtest | 2025-07-01..2026-07-31 | 442 | 31.0% | 0.867 | -0.819 | -12.7% | -21.0% |
| walk-forward | backtest | 2025-07-01..2026-07-31 | 636 | 35.9% | 1.003 | 0.412 | +3.3% | -7.3% |
| walk-forward | backtest | 2021-07-01..2026-07-31 | 477 | 38.8% | 1.195 | 0.327 | +9.2% | -6.2% |

Gate status (persisted system_logs, `system_gate`): **DENIED for ensemble/paper** —
`win rate 38.78% < min 39%`, `profit factor 1.19 < min 1.20`, `sharpe 0.33 < min 1.00`.

The 40.1% win rate quoted in the audit brief is **not persisted** in the DB; it
matches the last out-of-sample experiment aggregate (reversal-canonicalized
features, uncalibrated, threshold 0.55/0.45): 494 trades, win 40.08%, PF 1.223,
Sharpe -0.26, return -5.9%. It is treated here as the system's headline OOS number.

## 2. Configuration (`.env` + settings defaults)

- Watchlist / backtest universe: `AAPL,MSFT,TSLA,NVDA,AMZN,GOOGL` (6 mega-caps)
- SCAN_MIN_ATR_PCT=0.1, SCAN_TOP_K=10, scan interval M5
- BACKTEST_LOOKBACK_DAYS=500, backtest interval `1d`
- Broker: `paper`. Data provider: `yahoo`. Fundamentals: EDGAR (keyless). News: RSS (Google/Yahoo); **no LLM key configured → news items carry no sentiment → News Agent emits no signal**.
- Decision weights: technical 0.30, news 0.25, fundamental 0.20, prediction 0.25; buy 0.15 / sell -0.15; conflict 0.5; min coverage 0.5.

## 3. Universe & dataset

- DB prices: **502 symbols, 624,820 daily bars**, 2021-08-02..2026-08-06, stamped 13:30 UTC (09:30 ET open).
- Only 4 stocks marked active (TSTB/TSTC/TSTD test names); watchlist symbols are inactive in DB (worker `_ensure_watchlist_active` re-activates at startup).
- The 502 names are a **current** S&P 500 membership pull → survivorship-biased: no delisted names, no point-in-time membership.
- No adjustment columns persisted; Yahoo `adjclose` is fetched but **ignored** by the parser (split/dividend adjustments not applied to stored OHLC).

## 4. Models

- Registry: only `dash-momentum` (v1 active, offline accuracy 0.60, window 200x5). Not what the prediction agent loads.
- Prediction agent requests `momentum` → no registered model → **falls back to `HeuristicModel`** in the live path.
- Walk-forward gate trains its own per-fold logistic (60-bar lookback, 12-bar horizon, 11 price features) + Platt calibration (20% held-out slice).

## 5. Strategy / execution assumptions

- Backtest engine (BacktestParams defaults): stop 3%, target 6% (2:1 bracket), max_hold 0, trailing 0, warmup 30.
- Costs: backtest default commission 1bp/slippage 0bp; walk-forward default commission 1bp / slippage 5bp per side.
- Fills: next bar's open; intrabar stop/target on bar range; flat forced at test end.
- Risk: 1% risk per trade, ATR 1.5x stop, 2R target, max 10 positions, 80% exposure cap, 40% sector cap, 5-min cooldown, 10 trades/day.
- ML trainer: horizon 12, lookback 120, min samples 100, promote threshold 0.52, LR 0.5 / 200 epochs / L2 1e-3, Platt 500 epochs.
- Walk-forward: 5 folds, expanding train / forward test, `walk-forward` strategy label feeds the gate.

## 6. Headline audit hypotheses recorded before changes

1. Backtest signal path (`_SignalEngine`) uses only ML probability (or EMA-cross fallback); it does **not** replay the live Chief ensemble (technical/news/fundamental/prediction). Backtest ≠ live decisions.
2. Win-rate metric counts pnl==0 trades as wins (`wins = pnl>0`, `losses = pnl<0`, `win_rate = (n - losses)/n`).
3. News agent is inert without an LLM key; fundamental agent's signal quality depends on EDGAR staleness handling; neither is exercised by the backtest engine.
4. Universe is survivorship-biased; no delisted names; no point-in-time membership.
5. Only Yahoo for real-time quotes (delayed), single source of truth for historical data.
