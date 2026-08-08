# Phases 6–20 — Consolidated Findings (framework, patterns, regimes, model, folds, expectancy, costs, benchmarks, fixes)

Date: 2026-08-08. Baseline: `a459406`. This doc consolidates the evidence for
phases that reuse earlier computations. Scripts/artifacts:
`phase5_ablation.py/.out`, `phase7_analytics.py/.out`, `phase10_folds.py`,
`phase4_*.py`.

## P6 — Strategy framework
There is **no pluggable strategy registry**. `backtest_runs.strategy` is a
label (`ensemble` default; the WF validator writes `walk-forward`; one run
`technical`). The live path is the chief's weighted ensemble
(`decision_strategy.py`: technical 0.30 / news 0.25 / fundamental 0.20 /
prediction 0.25, buy 0.15 / sell −0.15 / conflict 0.50, coverage ≥ 0.50). The
backtested path is the fold-trained logistic model — a **third, separate
"strategy"** that exists only inside `walk_forward.py`. Three systems, three
configs, one persisted headline. Costs: engine applies commission_bps +
slippage_bps on notional; the persisted id145 run assumed 1bp + 5bp (the
`backtest_runs` id274 technical run used the same).

## P7 — Pattern statistics (60-symbol sample, 70,313 windows, 2021-07..2026-07)
- Base rate: forward 12-bar up 53.8%, mean +0.79% (bull bias).
- Feature ~ forward-return correlations are all tiny (|r| ≤ 0.11):
  `vol_20` +0.109, `range_ratio` +0.099, `atr_pct` +0.080, `ret_60` +0.033,
  `ret_20`/`ret_10` +0.024, `pos_in_range_20` −0.005, `up_ratio_20` −0.014,
  `ret_1` −0.0001. **No feature has exploitable predictive power.**
- Rule table (mean fwd / win): high-vol `atr_pct>5%` +4.01%/57.5% (n=4,175);
  `ret_20<-10%` +1.79%/59.3%; `ret_5<-5%` +1.67%/57.0%; breakout >0.8 +0.69%;
  low-vol <1.5% +0.41%. All within noise of the +0.79% base.
- **Caveat: computed on raw unadjusted prices** — split artifacts (±50% days)
  contaminate every extreme bucket; even these weak results are optimistic.

## P8 — Regimes
Equal-weight S&P index by year (unadjusted): 2021 +7.9%, **2022 −10.9%**,
2023 +24.0%, 2024 +20.1%, 2025 +14.2%, 2026 +10.7%. The tested OOS window for
full-history names is **2022-04..2023-11** (see P10): a bear year plus a
recovery — not the 2024–26 bull. **The system does not adapt to regimes**;
there is no regime filter anywhere in the decision path (no regime agent/engine
in `agents/`, no regime column in `decision_log`).

## P9 — Model audit
- Fold in-sample accuracy 55.1–57.2% vs ~50% coin-flip base rate → marginal.
- Platt `calib_a` unstable across folds (−0.076 … +0.722) → probabilities not
  reliable; threshold decisions inherit this instability.
- **Shuffled-label control ≈ real model** (PF 0.943 vs 1.294 under the same
  protocol) → the pipeline cannot distinguish signal from noise.
- The trained/promoted registry model is **never used live** (Phase 4: v1–v6
  placeholders, artifact_path=null, predictions always `momentum` v0).
- No train/test overlap in the fold construction (labels come only from the
  past) → no gross leakage in the WF loop itself; the *reported* "edge" is
  selection-on-noise (threshold lottery, P5), not leakage.

## P10 — Walk-forward OOS calendar alignment (verified per symbol)
Same index window `[ts,te)` covers **disjoint calendar periods per symbol**:

| fold | MSFT/AMZN/NVDA (full hist) | GEV/SOLV | VLTO |
|---|---|---|---|
| 1 (validation) | 2022-04..2022-09 | 2024-12..2025-05 | 2024-06..2024-11 |
| 2 | 2022-09..2023-02 | 2025-05..2025-10 | 2024-11..2025-04 |
| 3 | 2023-02..2023-07 | 2025-10..2026-03 | 2025-04..2025-09 |
| 4 | 2023-07..2023-11 | 2026-03..2026-07 | 2025-09..2026-02 |

- Full-history names are OOS-tested only in **2022-04..2023-11** (~19 months,
  including the 2022 bear); the 2024–26 bull is **never** OOS-tested for them.
- New listings (GEV/SOLV/VLTO) are OOS-tested on a **different calendar period
  (2024–2026)**. The aggregate mixes regimes and periods per symbol; the
  "2021-07..2026-07 walk-forward" label is not what the test actually covered.

## P11 — Ranking engine
The scanner scores active symbols and persists a Redis top-K
(`scanner.py`); 477 `agent_metrics.candidates` rows (avg 5.79). Rankings feed
the live pipeline but **not** the backtested strategy — another
live/backtest disconnect (Phase 4).

## P12 — Expectancy
Phase-5 EV math on the baseline path: win 22.9–41.4% across thresholds,
avg_win/avg_loss ≈ 1.76–1.89 vs the 2.0 bracket cap; EV/trade ≈ +0.16R before
costs (R = 3% risk), i.e. a paper-thin margin fully consumed by realistic
costs. Breakeven win rate for 3/6 bracket = 33.3% (+6% margin → 39.3%),
which is why the gate's min-win is 39.3%.

## P13/P20 — Costs & stress
Always-long strategy, folds 2–4, fixed threshold, no sectors:

| commission/slippage bp | trades | win% | PF | ret% |
|---|---|---|---|---|
| 0 / 0 | 565 | 36.1 | 1.130 | +42.8 |
| 1 / 5 | 506 | 39.1 | 1.211 | +71.5 |
| 5 / 20 | 474 | 38.8 | 1.138 | +42.1 |
| 10 / 50 | 516 | 32.2 | 0.798 | **−55.2** |
| 25 / 100 | 502 | 24.7 | 0.500 | **−89.3** |

- Realistic retail costs (10–50bp) **destroy the strategy**. It only shows a
  profit at sub-1bp assumptions.
- Non-monotonicity (1/5bp > 0bp) shows the simulation is path-chaotic: small
  execution changes flip stop/target hits and compounding; results are not
  stable under perturbation.

## P14 — Paper trading / P15 — Audit trail
`orders` = 4, `trades` = 0, `risk_history` = 0 (Phase 4): the system has never
executed a trade; risk/execution/portfolio agents produced no records. The
unified agent record is incomplete (missing `input_data_version`, `latency`,
per-agent reason, populated `features_hash`); `system_logs` has 25 rows only.

## P16 — Counterfactuals
Live-path counterfactuals are impossible (all decisions are one day with no
forward bars — Phase 4). Strategy-level counterfactuals ARE the Phase 5
ablation: no-signal > model, shuffled ≈ real, threshold lottery. The decisive
counterfactual: **remove the model entirely and the system improves.**

## P17 — Benchmarks
- Passive equal-weight S&P (unadjusted): **+84.97%** (2021-07..2026-07) vs
  persisted strategy **+9.23%** → the strategy underperforms buy-and-hold by
  ~76pp over the label window (and the label window itself excludes the best
  years for full-history names — P10).
- True OOS 2022-09..2023-11: equal-weight **+14.29%** — even the compressed
  OOS was a rising market.
- Always-long bracket control (Phase 5): +38.87% (with 10-position cap and
  sectors) — 4× the model strategy, still below passive.

## P18/P19 — Fixes & re-test (summary; detailed in final report)
Priority order: (1) point-in-time universe + adjusted prices; (2) calendar-aligned
per-symbol walk-forward windows; (3) remove the ML signal (or prove OOS edge
with proper controls); (4) realistic costs (≥10bp) and remove the 6% target cap
or time exits; (5) unified decision record (P15 fields); (6) persist every run
as a `backtest_runs` row; (7) gate on net-of-cost, regime-aware metrics. Re-test
protocol: rerun the full 17-phase audit against the fixed baseline and compare
A/B (P19/P1 freeze discipline).
