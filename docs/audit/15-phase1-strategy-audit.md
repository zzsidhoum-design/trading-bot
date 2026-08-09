# Phase 1 — Strategy & Backtesting Audit

Date: 2026-08-09. Baseline: `a459406` (P19), engine now at HEAD `b27e29d`.
Work request: *"determine whether the trading system has a real edge without
artificially inflating win rate; audit strategies, pattern testing, market
regimes, backtest correctness, walk-forward, full metrics, and baselines."*

This is the audit itself. Nothing was tuned to raise win rate; every fix made
here is a correctness fix proven by a test. Live trading remains forbidden.

## Executive verdict

**No active strategy in the system produces a positive-alpha, cost-adjusted
edge over the four-year OOS window.** At the audit's gate costs (10bp
commission + 50bp slippage, now including exit commissions):

- **every** strategy loses money or barely breaks even under the production
  3%/6% bracket (best: ML −3.9%);
- under the no-target/time-12 exit the ML entry (+25.4%) and Donchian breakout
  (+15.4%) beat the always-long (−6.1%) and random (−26.9%) controls — but all
  are **massively below the passive benchmarks** (equal-weight Buy&Hold
  +109.8%, equal-weight index +93.6%, SMA200 filter +95.1% over the same
  window);
- the ~32–35% win rates measured here are **not** an artifact of tuning — they
  are the honest, cost-corrected outcome of trading bracket exits on adjusted
  data. The prior "40% win rate" figure was a single-experiment artifact
  already debunked in `04-final-report.md`;
- prior P19 magnitudes (including the headline V3 "+58.37% passes") were
  computed on an engine that charged **zero exit commission**. They are not
  reproducible on the fixed engine; the qualitative conclusion that survives
  is *ML entry > always-long at realistic costs with time exits*, not a
  positive-alpha claim;
- pattern-level forward drift exists (rsi_oversold net +0.80%/event over 12
  bars, win 59%) but none of it survives as a standalone strategy after
  costs/filtering — consistent with the phase-5/7 finding that these buckets
  do not persist into OOS.

Bottom line: the only "edge" in the measured window is the bull market. The
acceptance bar for graduating anything to live trading remains **not met**.

## 1. Protocol

- Data: SQLite-derived Postgres bar DB, split/dividend-adjusted (fix #1),
  2021-08-01..2026-07-31, 498 S&P 500 symbols with ≥372 D1 bars.
- Folds: calendar-aligned, point-in-time (P19 fix #2),
  `CalendarWalkForwardValidator.make_folds`, 4 contiguous OOS blocks
  2022-08-01..2026-07-31. PIT-eligible symbols per fold (listed strictly
  before block start).
- Engine: production `BacktestRunner._simulate` (next-open fills, intra-bar
  stops, ATR/1%-risk sizing, exposure caps) with the **exit-cost fix**
  (`b4bed55`): stop/take/time/end-of-test exits now pay commission.
- Costs: gate bar 10bp commission + 50bp slippage; 1/5bp shown for the
  controls only (reconciliation).
- Entry thresholds: 0.52/0.48 (the strategy contract's `BUY_THRESHOLD`/
  `SELL_THRESHOLD`), fixed, no tuning. Rule strategies emit 0.9/0.1 event
  probs; ML emits calibrated probabilities.
- Exits: both configs — 3%/6% bracket (`stop_loss_pct=0.03,
  take_profit_pct=0.06`) and no-target/time-12 (`take_profit_pct=0.999,
  max_hold_bars=12`), the exit design that P19 found decisive.
- Every strategy runs through the identical engine (prob series → `model_outputs`
  contract), so fills/costs/sizing are apples-to-apples.
- Evidence driver: `p1_strategy_audit.py` (temp), output
  `p1_strategy_audit.json`; controls reproduced independently by
  `p19_controls.py` re-execution.

## 2. Deliverable 1 — Strategy inventory

All registered, deterministic, causal, and objectively testable:

| name | kind | entry | exit signal |
|---|---|---|---|
| `momentum` | momentum | EMA9/21 up-cross | EMA9/21 down-cross, RSI>70 |
| `trend_following` | trend | EMA9>EMA21>SMA50 stack | reverse stack |
| `breakout` | breakout | close > 20-bar Donchian high | close < 20-bar low |
| `mean_reversion` | mean_reversion | RSI < 30 | RSI > 55 |
| `ml` | ml | fitted 11-feature logistic + Platt (per fold) | prob ≤ 0.48 |
| `always_long` | baseline | const 0.55 (never flat) | bracket/time only |
| `random` | baseline | seeded random sparse entries | seeded random exits |

Registry: `src/qtrader/application/services/strategies/` (commit `bc4e283`).
ML is trained per fold on the training window only (PIT) and is **not**
pre-registered — it is constructed per fold via `MLProbabilityStrategy`
(`src/qtrader/application/services/strategies/ml.py`), identical to
`CalendarWalkForwardValidator.precompute_probs`.

## 3. Deliverable 2 — Per-strategy performance (calendar-PIT OOS, 10/50bp)

Bracket exits (production default):

```
strategy          trades   ret     win     PF     Sharpe   Sortino  MaxDD     expectancy
always_long        360  -29.09%  33.06%  0.867  -0.406   -0.356   -39.73%   -0.891
breakout           309  -40.08%  31.07%  0.777  -0.732   -0.624   -44.67%   -1.464
mean_reversion     361  -23.86%  34.90%  0.928  -0.288   -0.258   -46.07%   -0.456
ml                 392   -3.91%  36.48%  0.987   0.034    0.032   -29.95%   -0.085
momentum           336  -48.26%  30.95%  0.723  -0.962   -0.820   -53.99%   -1.758
random             378  -28.68%  34.92%  0.861  -0.440   -0.391   -40.88%   -0.839
trend_following    341  -20.90%  34.90%  0.898  -0.235   -0.201   -26.62%   -0.666
```

No-target/time-12 exits:

```
strategy          trades   ret     win     PF     Sharpe   Sortino  MaxDD     expectancy
always_long        341   -6.06%  32.26%  0.976   0.023    0.021   -24.52%   -0.164
breakout           281  +15.43%  35.23%  1.092   0.258    0.245   -28.67%   +0.591
mean_reversion     324  -32.39%  31.17%  0.831  -0.338   -0.305   -43.49%   -1.143
ml                 337  +25.39%  32.05%  1.113   0.327    0.316   -25.88%   +0.771
momentum           354  -52.12%  31.64%  0.710  -0.940   -0.841   -55.82%   -1.832
random             332  -26.88%  30.12%  0.883  -0.305   -0.282   -36.48%   -0.738
trend_following    343   -2.25%  32.07%  0.997   0.065    0.058   -23.52%   -0.021
```

Notes:
- `expectancy` is per-trade % of entry notional (signed; `PerformanceMetrics.
  expectancy_formula`); `total_costs` is embedded in the engine, not a
  separate charge, so the `total_costs` field is not populated by this run.
- Win rate now counts only trades with strictly positive PnL (zero-PnL is not
  a win, commit `9f3e691`). The ~32–35% figures are honest and were **not**
  targeted.
- The bracket exit is uniformly bad at 10/50bp; time-12 mostly rescues the ML
  and breakout entries only.

## 4. Deliverable 3 — Best market regime per strategy

Regime engine: `MarketRegimeEngine` (commit `3d5c7dc`) on the equal-weight
index closes over 2022-08..2026-07. Trend axis = close/SMA200 + 50/200
alignment; vol axis = percentile rank of 20-day annualized vol in a 250-day
history. Cold-start (`n/a`) = first ~270 OOS trading days (~2022-08..2023-08).

Regime day counts: `bull-low` 491, `n/a` 269, `bull-high` 116, `sideways-high`
45, `sideways-low` 37, `bear-extreme` 18, `bull-extreme` 13, `bear-high` 10,
`bear-low` 3, `sideways-extreme` 2.

Per-regime total return of the time-12 equity curve (daily returns attributed
to the regime label of the day):

| strategy | best regime | best return | worst regime | worst return |
|---|---|---|---|---|
| `ml` | n/a (cold-start) | +23.6% | sideways-low | −8.0% |
| `breakout` | n/a | +15.8% | sideways-extreme | −6.4% |
| `trend_following` | sideways-high | +8.6% | bear-high | −4.5% |
| `momentum` | bull-extreme | +5.0% | bull-low | −36.7% |
| `mean_reversion` | bull-high | +4.8% | bull-low | −26.1% |
| `always_long` | n/a | +4.5% | bull-low | −6.8% |
| `random` | bull-high | +14.7% | n/a | −23.7% |

Trade-level (time-12) win rates by regime confirm the same picture: every
strategy trades mostly in `bull-low` (134–169 trades) with win rates
28–33% — i.e. **the dominant regime is where all strategies bleed**. The
best returns come either from the unlabeled cold-start window (`ml`,
`breakout`) or from low-volume extreme regimes (`random` +14.7% in bull-high
on 37 trades — noise, not edge).

Caveat: ~27% of OOS days are `n/a` (regime warm-up), and `ml`'s single best
bucket is that unlabeled window — so "ML likes this regime" cannot yet be
claimed. A labelled long enough regime history (the DB now has data to
2026-08-07) is needed.

## 5. Deliverable 4 — Strategies with no edge

At the gate bar, **every** strategy fails to produce a positive, cost-adjusted
edge over holding the index:

- `momentum` (EMA9/21) is decisively dead: −48% bracket / −52% time-12 at
  10/50bp, worst drawdowns (−54/−56%). This is the live fallback path
  (`model=None`), consistent with P19's "momentum-v0 stays dead" finding —
  now even deader under honest exit costs (−75% → −90% bracket re-measured).
- `mean_reversion` (RSI<30) loses in both exit configs (−24%/−32%); its
  pattern-level forward edge (see §6) does not survive bracket/time exits.
- `trend_following` is flat-to-negative (−21% bracket, −2.3% time-12) — no
  persistence.
- `always_long` at 10/50bp loses −29% bracket / −6% time-12. This is the
  **control**, not a candidate — but it also sets the acceptance bar: it is
  itself unviable at real costs, so "beating always-long" is a low bar.
- `random` loses (−29%/−27%), confirming the engine itself doesn't hand out
  free money.

No strategy reaches the previous reports' acceptance criterion (beat
always-long **and** shuffled) **and** the passive index. See §8.

## 6. Deliverable 5 — Pattern testing (real edge?)

There are no candlestick patterns in the pipeline; the tradable signals are
indicator events. `pattern_events.py` + `pattern_metrics.py` (commit
`b27e29d`) measure every event's forward window (next-open entry, ≤12 bars,
60bp round-trip net) pooled across all folds' OOS bars:

```
pattern                 events   win      net ret/event   MFE      MAE
rsi_oversold             5,434   59.27%    +0.80%         +6.27%   -4.91%
momentum_up_cross       10,853   55.51%    +0.32%         +5.54%   -4.71%
breakout_down           26,371   55.79%    +0.43%         +5.94%   -4.88%
breakout_up             39,408   54.26%    +0.16%         +5.16%   -4.46%
momentum_down_cross     10,943   52.93%    +0.02%         +5.48%   -4.81%
rsi_overbought           9,943   53.09%    -0.01%         +5.02%   -4.49%
```

Read honestly:
- A positive 12-bar drift after `rsi_oversold`/breakouts is **real in-sample
  to this window** but small (~+0.2–0.8%/event gross-of-cost edge), and the
  strategies that trade those events (mean_reversion, breakout) lose money
  after execution anyway — the drift does not survive as a tradable edge.
- `breakout_up` at 39k events is the most common signal and the weakest
  (+0.16%); its count also confirms the strategy's churn.
- These event stats are aggregate forward returns **not** chained
  portfolio returns; they do not compound into the strategy numbers in §3,
  and they should not be confused with strategy edge.

Verdict: **no pattern carries a stable, cost-adjusted edge** in the current
pipeline. None should be promoted; none was tuned to be.

## 7. Deliverable 6 — Look-ahead / leakage findings

Audit of the current pipeline (post P19 fixes) for the classes in the work
request:

- **Next-open fills** — no look-ahead. Orders queued at bar `t` fill at
  `t+1` open with slippage; verified by engine test (`flat_bars[34]` entry).
- **Intra-bar stops/targets** — evaluated on the same bar's high/low; a bar
  that gaps through both stop and target fills at the stop (conservative).
- **Regime labels** — `MarketRegimeEngine` uses only data ≤ row `i`
  (rolling SMA/percentile), causality test in suite.
- **Benchmark SMA200** — position earned next day, no same-day look-ahead
  (test asserts this).
- **Strategy probs** — rule strategies read `series` up to and including the
  signal bar; ML features use bars `[i-59..i]`; OOS-only emission.
- **Point-in-time universe** — a symbol trades only in folds whose block
  starts after its listing (P19 fix #2), so no future listing leaks into
  training or trading.
- **Data** — adjusted prices (fix #1) remove split-date discontinuities that
  previously inflated the always-long controls.

Open/remaining integrity caveats (from `04`/`13`, still true):
- The DB validator still rejects genuine crash bars (e.g. GL −53% on
  2024-04-12) as `max_single_bar_move_pct=0.5` — conservative, symmetric
  across all variants.
- The live agent signal path is not fully identical to the backtest path for
  intraday/scan decisions (P7 finding), so backtest ≠ live until that gap is
  closed; this audit measures the backtest engine only.
- `total_costs`/turnover are not separately persisted by the current run;
  costs are embedded in fills.

## 8. Deliverable 7 — Comparison vs baselines

Direct benchmark curves over the identical OOS window (2022-08-01..2026-07-31),
computed by `benchmarks.py` (commit `1e79e56`):

```
benchmark        total return
Buy&Hold 1/N        +109.76%
Equal-weight index  +93.56%
SMA200 filter       +95.06%
```

vs best active strategies (10/50bp): ML time-12 +25.39%, breakout time-12
+15.43%. **The best active result is ~68–84 percentage points below the
passive index over the same dates.** The active side does not add value over
"own the market" — it destroys it, primarily through bracket churn + costs.

## 9. Reconciliation with prior reports

### 9.1 The exit-cost fix changed every absolute number (P19 ↔ now)

P19 (`14-p19-retest.md`) measured on an engine that charged **zero exit
commission**. Re-measured on the fixed engine (identical script, adjusted
bars, same folds):

| control | P19 (zero exit cost) | now (exit cost charged) |
|---|---|---|
| AL bracket 3/6 @ 10/50bp | −5.93% | **−29.09%** |
| AL time-12 @ 10/50bp | −19.25% | −6.06% |
| AL bracket @ 1/5bp | +18.91% | +32.78% |
| MOM time-12 @ 10/50bp | −37.67% | **−85.15%** |
| MOM bracket @ 10/50bp | −75.26% | −89.74% |
| MOM bracket @ 1/5bp | +139.47% | +152.19% |

Two independent scripts reproduce the new values exactly (internal-consistency
check). The changes are not a simple additive cost; exit commission depletes
cash and re-sizes subsequent 1%-risk positions, so the cost × cash-deployment
interaction shifts returns in non-obvious directions (this is itself a finding:
single-point backtest returns are sensitive to the sizing/cash model, so
small-magnitude deltas should not be over-interpreted).

**P19's headline V3 (+58.37% at 0.60/0.40) was measured on the pre-fix engine
and is not reproducible as stated.** A faithful re-run of the P19 protocol
(0.60/0.40, time-12) on the fixed engine is a listed follow-up. At the
audit's non-tuned 0.52/0.48, ML time-12 is **+25.39%** — still ahead of the
always-long (−6.06%) and shuffled-style random (−26.88%) controls, so the
*qualitative* P19 conclusion (ML entry > always-long with time exits at
realistic costs) survives the fix, but the magnitude and the "passes the
acceptance bar" claim do **not** transfer.

### 9.2 Earlier reports

- `04-final-report.md` / `13-final-report.md`: "no OOS edge; bracket math +
  bull beta; shuffled ≈ real" — **confirmed** here on the fixed engine:
  always-long bracket is now decisively negative at gate costs, and nothing
  beats passive.
- `13`'s always-long bracket control (PF 1.685, +38.87% at sub-1bp costs) was
  the classic sub-cost artifact; at 10/50bp with exit costs the same idea is
  −29%.
- "40% win rate" artifact: not reproduced; measured win rates are 31–36% and
  are honest, cost-inclusive outcomes.

## 10. Verdict & follow-ups

**Verdict: the system has no demonstrated real edge. Nothing graduates.**

- No strategy beats the passive benchmarks at realistic costs.
- The only positive results (ML/breakout, time-12) clear a *low* bar
  (always-long) and carry real drawdowns (−25/−29%).
- Win rate was never inflated; at gate costs it is 31–36% and the payout
  ratio is <2:1, matching the breakeven math.

Follow-ups (each is a correctness/measurement fix, not tuning):
1. Re-run the P19 0.60/0.40 protocol (V1/V2/V3 + controls) on the fixed
   engine to re-derive comparable magnitudes.
2. Regime-labelled, out-of-sample test on the extended adjusted data
   (2026-08-08 → present) so the `n/a` cold-start bucket shrinks and the
   per-regime claims can be tested forward.
3. Persist `total_costs`/turnover per run so cost attribution is visible in
   `strategy_performance`.
4. Keep all strategies behind the gate (live trading stays off); re-open
   only with a fresh holdout that beats the passive index net of costs.

## Appendix — Reproducing

- Engine/fixes: `backtest.py` (`b4bed55` exit costs), `strategies/`
  (`bc4e283`), `market_regime.py` (`3d5c7dc`), `performance_metrics.py`
  (`9f3e691`), `benchmarks.py` (`1e79e56`), `pattern_events.py` +
  `pattern_metrics.py` (`b27e29d`).
- Drivers (temp, not committed): `p1_strategy_audit.py` →
  `p1_strategy_audit.json`; `p19_controls.py` re-exec →
  `p19_controls_reexec.log`.
- Unit suite: 385 passed, 27 deselected; ruff + mypy clean on touched
  services (pre-existing `universe.py` mypy errors untouched).
