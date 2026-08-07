# Scientific Audit — Final Report

Date: 2026-08-07. Baseline: `f7cff7b`. Evidence in `docs/audit/01..03`.
Method: all numbers recomputed against the persisted DB, the walk-forward
experiments, and the live pipeline state. No parameter changes were made to the
frozen baseline; nothing new was persisted (the gate denied every candidate).

## Executive verdict

**The system has never produced an out-of-sample tradable edge.** Every
configuration of the model family fails the graduation gate; the headline
"40% win rate" is a metric-level artifact of one experiment (win 40.08%,
PF 1.223, **Sharpe −0.26, return −5.9%**) and is not even present in the
database. Against a zero-skill equal-weight buy-and-hold over the identical
OOS window the strategy underperforms by ~19 percentage points. Multiple
integrity problems (unadjusted prices, bar-index fold misalignment,
backtest≠live signal path, trained model never used) mean the persisted
numbers cannot be treated as evidence of predictive skill.

## Answers to the audit's core questions

1. **Is the 40% win rate real / where did it come from?**
   It is not in `strategy_performance`. It matches the last OOS reversal
   experiment aggregate (494 trades, win 40.08%, PF 1.223, Sharpe −0.26,
   return −5.9%). The DB's best walk-forward row is 477 trades, win 38.78%,
   PF 1.195, Sharpe 0.327, return +9.2%. Both variants were DENIED by the gate.

2. **Is a 39% win rate profitable?**
   For the 3%/6% bracket, breakeven is 33.3% (no cost) / 34.7% (1bp+5bp per
   side). The gate's 39.33% floor = breakeven + 6pp margin — defensible in
   design. But the realized payoff ratio is 1.83:1 (not 2:1), raising the true
   breakeven to ~36%+. At PF 1.008 (calibrated) the trade-level edge is ~zero.

3. **Did the walk-forward OOS really span 5 years?**
   No. Folds are bar-index aligned; short-listed 2024 names (GEV/SOLV) compress
   the schedule. For 492/502 symbols the true OOS test window is
   **2022-09-07..2023-11-29 (~14 months)**; a few names are tested on a
   different period entirely.

4. **Does the backtest test the same decisions as live?**
   No. Backtest `_SignalEngine` uses ML prob thresholds (0.52/0.48) or EMA(9/21)
   crossover + RSI>70. The live Chief uses a weighted ensemble
   (technical 0.30 / news 0.25 / fundamental 0.20 / prediction 0.25). The
   backtest never uses news or fundamentals. **Backtest ≠ live.**

5. **Is the trained model actually used?**
   No. Predictions agent requests `momentum`; registry holds `dash-momentum`
   only. All 2569 predictions are `HeuristicModel` fallback (momentum v0). The
   registry hyperparams (`{coef:[0.1]}`, no `feature_names`) would fail to
   reconstruct a `LogisticModel` anyway.

6. **Is the data reliable?**
   Mostly structurally clean (0 dup, 0 high<low, 0 weekends, 0 >30-day gaps) but
   **OHLC is unadjusted**: 16 single-day moves >50% are split artifacts +
   genuine crashes; 2 OHLC violations on the final bar; 352 zero-volume bars
   (all SW); 7 symbols have <800 bars (new listings). Features/labels computed
   on raw closes are corrupted at split dates. Universe is survivorship-biased
   (current S&P 500, no delisted names).

7. **What does the live pipeline actually produce?**
   On 2026-08-06: 2569 signals/decisions/predictions, but 0 trades persisted,
   `risk_history` empty, fundamental data for 7/502 names, news sentiment
   default (no LLM key), all 502 S&P names flagged `is_active=False`. Live
   trading disabled (paper broker, `ENABLE_LIVE_TRADING=false`).

## Findings by phase

| phase | finding |
|---|---|
| 1 baseline freeze | 335 tests pass; commit `f7cff7b`; headline numbers frozen (see 01) |
| 2 data integrity | clean structure; **unadjusted OHLC**; split artifacts; 2 OHLC errors on last bar; 352 zero-vol bars |
| 3 universe | **survivorship bias**; bar-index fold misalignment ⇒ OOS ≈ 14 months not 5y |
| 4 agent/DB audit | trained model unused; news/fundamental legs weak; 0 trades persisted; no STRONG_SELL |
| 5 benchmarks | B&H +12.8% / SMA200 +3.0% vs strategy −5.9% over same window (see 03) |
| 6 breakeven/gate | 39.33% floor = breakeven+6pp; realized payoff 1.83:1 makes margin thinner |
| 7 regimes | market +0.65 Sharpe over OOS; strategy lost in a rising market |
| 8 metric integrity | win_rate/PF/return use different bases; PF 1.008 vs return +25% divergence |
| 9 backtest vs live | different decision paths; historical results don't test the live ensemble |
| 10 model review | registry placeholder; trainer path unreachable by prediction agent |
| 12-13 experiments | calibrated & uncalibrated variants both DENIED (see 03) |
| 14-15 execution | fills next-open, 2:1 bracket, 1% risk; commission 1bp/slip 5bp; no trade audit trail |
| 16-17 win-rate/process | 40% win ⇒ negative return shown; experiments stopped, nothing persisted |

## Recommended repairs (for a future remediation phase, not applied here)

1. Apply split/dividend adjustment (use Yahoo `adjclose` or an adjustment
   source) and re-derive features/labels; or drop split bars.
2. Rebuild the universe point-in-time (incl. delisted names) and make folds
   **calendar-date aligned** per symbol.
3. Make the backtest replay the actual Chief ensemble path so backtest ≡ live.
4. Fix model selection: request the registered model name; make
   `LogisticModel.from_registered` validate hyperparams; log which model served
   each prediction.
5. Persist trade-level rows from backtests (currently aggregates only).
6. Reconcile `win_rate` / `profit_factor` / `total_return` on a single P/L
   basis (e.g., mark-to-market equity) and add a cost-aware breakeven to the
   gate (payoff-ratio aware, not fixed 2:1).
7. Enable `is_active` consistency and real fundamental/news feeds, or zero out
   the corresponding ensemble weights.

## Open items (Phase 20 backlog)

- Ablation A/B on adjusted prices (requires new data import, out of scope for
  this read-only audit session).
- Walk-forward with calendar-aligned folds on a survivorship-free universe.
- Stress/latency/replay tests of the live pipeline (currently paper-only).
