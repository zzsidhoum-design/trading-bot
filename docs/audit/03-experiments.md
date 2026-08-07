# Benchmark, Regime & Breakeven Analysis (Phases 5–8)

Date: 2026-08-07. Baseline commit: `f7cff7b`.

## 1. What the walk-forward OOS actually covered

The walk-forward folds are **bar-index aligned, not calendar aligned**
(`walk_forward.py:_make_folds`). `min_len = min(bars)` over the universe; the
universe contains 2024–2026 listings (GEV 592 bars, SOLV 593), which compress
the whole fold schedule. Test windows map to different calendar dates per
symbol:

| fold | AAPL (full history) | GEV (2024 listing) |
|---|---|---|
| 0 (train) | 2021-11-10..2022-04-08 | 2024-07-11..2024-12-05 |
| 1 (validation) | 2022-04-08..2022-09-07 | 2024-12-05..2025-05-07 |
| 2 (test) | 2022-09-07..2023-02-03 | 2025-05-07..2025-10-03 |
| 3 (test) | 2023-02-03..2023-07-05 | 2025-10-03..2026-03-04 |
| 4 (test) | 2023-07-05..2023-11-29 | 2026-03-04..2026-07-31 |

So despite the nominal period label "2021-07-01..2026-07-31", for the 492
full-history symbols **the true OOS test window is 2022-09-07..2023-11-29
(~14 months)**, and a handful of new listings are tested over a *different*
period (2024–2026). Training sets likewise mix calendar periods across symbols
because train cut-offs are global bar indices.

Consequence: the "5-year" walk-forward result is really a ~14-month test of the
2022 bear market + 2023 recovery, plus an unmatched window for a few symbols.

## 2. Benchmarks over the identical test windows

Equal-weight B&H and SMA200-long-only are evaluated on the same symbols and the
same bar windows the strategy traded (no lookahead; cost 1bp commission + 5bp
slippage per side for SMA200; B&H no cost).

| window (AAPL dates) | B&H | SMA200 | universe index | strategy (rev, uncalib 0.55/0.45) |
|---|---|---|---|---|
| 2022-09-07..2023-02-03 (fold2) | +8.14% | +2.30% | +8.86% | −3.51% |
| 2023-02-03..2023-07-05 (fold3) | +2.52% | +0.31% | +2.39% | +2.22% |
| 2023-07-05..2023-11-29 (fold4) | +0.98% | +0.80% | +1.21% | −4.63% |
| **Aggregate** | **+12.80%** | **+3.01%** | **+12.81%** | **−5.93%** |

Market regime over the OOS aggregate window: mean daily +0.045%, std 1.111%,
up-days 49.5%, annualized Sharpe ≈ +0.65 (a mildly rising, choppy market).

**Verdict:** the strategy's OOS result (−5.9%) is 18.7pp *below* a zero-skill
equal-weight buy & hold (+12.8%) on the same capital and dates. The 40.08% win
rate / PF 1.22 does not correspond to any positive return; the portfolio-level
outcome is a loss in a market that rose.

## 3. Win-rate breakeven math (is the 39% bar right?)

For the configured 3% stop / 6% target bracket:

| costs | breakeven win rate | EV/trade at win 39.3% |
|---|---|---|
| none | 33.33% | +0.54% |
| 1bp comm + 5bp slip per side | 34.67% | +0.42% |

`SystemGate.GateThresholds` derives its floor as `stop/(stop+target) + margin`
= 33.33% + 6pp = **39.33%**. So the 39% bar quoted in the audit brief is not
arbitrary — it is a reward/risk-aware breakeven-plus-margin. That said, the
margin assumes the realized payoff ratio is exactly 2:1.

**Realized payoff ratio is lower.** From the uncalibrated aggregate (win 40.08%,
PF 1.223): implied avg_win/avg_loss = PF × (1−w)/w = **1.83**, not 2.0. Many
trades exit via time/flat, not the target. With payoff 1.83:1, breakeven is
33.3%×2/1.83 ≈ **36.4%** (pre-cost) — the true breakeven is well above the
33.3% the gate's margin is computed from, making the margin thinner than
designed. And at PF 1.008 (calibrated aggregate) the edge is essentially zero
trade-level.

## 4. Why win 40% + PF 1.22 still lost money

`PerformanceMetrics` computes the four headline numbers from *different bases*:

- `win_rate = (n − losses)/n` — pnl==0 counts as a win (performance_metrics.py:99).
- `profit_factor = gross_profit/gross_loss` from closed-trade P/L% (fees included in `pnl_pct` via `entry_cost`).
- `total_return` from the **chained fold equity curve** (each fold re-simulates at 100k, curves rescaled and appended; includes mark-to-market of open positions and forced-flat at fold end).

The calibrated run illustrates the divergence: **PF 1.008 (gross ≈ breakeven) yet
chained total return +25.45%**. Trade-level gross P/L and the compounded equity
curve are not reconciled by any assertion, so "PF > 1" and "win > 33%" give no
guarantee of a positive equity return. The aggregate +25.45% (calibrated,
1262 trades) is dominated by a single fold (fold2: +19.20%, win 43.7%,
PF 1.537) while folds 3–4 were weak/negative — not a robust, replicable edge.

## 5. Calibrated vs uncalibrated, and the tuner

Threshold tuning (0.52/0.48 … 0.70/0.30) selects the candidate maximizing
Sharpe on the fold-1 *validation* window — but in both runs the best validation
Sharpe was **negative** (−0.215 calibrated, +0.435 uncalibrated). The
"best" candidate is selected from a set in which every option failed the gate,
so tuning never rescues the strategy:

| variant | threshold | trades | win | PF | Sharpe | return | gate |
|---|---|---|---|---|---|---|---|
| calibrated | 0.55/0.45 | 1262 | 34.55% | 1.008 | 0.784 | +25.45% | DENIED (win, PF, Sharpe) |
| uncalibrated | 0.55/0.45 | 494 | 40.08% | 1.223 | −0.257 | −5.93% | DENIED (Sharpe, ret) |
| prod. gate floor | — | 30 | 39.33% | 1.20 | 1.00 | 0% | — |

No configuration of this model family passes the gate. Stopping after
experiments; no results persisted (gate DENIED → nothing written).

## 6. Ablation notes

Feature direction canonicalization (rev_60 = −ret_60, etc.) produced
directionally stable fold coefficients (rev_60 > 0 in all folds) and a
monotone feature→win-rate gradient on 2021–2023 train data, which is why the
family was explored. It did not survive OOS economics: the OOS trades still
lose to cash in folds 2 and 4 and to B&H overall, so the in-sample gradient
does not transfer to tradable profit (see 01-baseline-freeze.md §6 for the
original hypotheses — findings 1, 3, 4, 5 confirmed; finding 2 confirmed for
the metric; the reversal edge itself fails out-of-sample).
