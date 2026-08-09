# Phase 2 — Agent & ML Audit

Date: 2026-08-09. Baseline: `a459406` (P19), engine at HEAD `b27e29d` (same
engine as Phase 1). Work request: *"audit every agent (technical, news,
fundamental, prediction, pattern, market regime, LLM) and every ML model —
splits, walk-forward, leakage, overfitting, feature importance, calibration —
with per-agent accuracy, latency, failure rate, and contribution; prove or
reject decision traceability."*

This audit measures the **production ensemble decision path** on the same
calendar-PIT engine, costs (10bp + 50bp), universe (498 S&P 500, ≥372 D1
bars), and OOS window (2022-08-01..2026-07-31, 4 folds) as Phase 1, so the
numbers are directly comparable. Nothing was tuned to raise win rate; live
trading remains forbidden.

## Executive verdict

**No agent contributes a cost-adjusted alpha edge; the only positive
contributor is the prediction agent, and it is a beta tilt, not skill.**

- Complete ensemble: **−32.80%** bracket / **−10.27%** time-12. Removing the
  **prediction** agent collapses time-12 to **−38.41%** (worst row) — it is
  the ensemble's *only* positive contributor. Removing **technical**
  (+29.67%), **pattern** (+21.24%), or **regime** (+39.62%) each **improves**
  time-12 return: those three signals are net drags on the fused decision.
- The raw ML baseline alone (**prediction_only**): **−3.91%** bracket /
  **+25.39%** time-12 — strictly better than the complete ensemble in *both*
  exit configs. Fusing the four backtestable agents destroys value relative
  to the single ML signal.
- Bracket exits lose money in **every** configuration (including the
  always-long control −29.09%), so bracket contribution is confounded by the
  exit; the time-12 comparison is the meaningful one and is used below.
- The prediction agent itself has **no ranking skill**: ROC-AUC 0.5080 vs a
  shuffled-label control 0.5017. Its profit is the market's drift — the OOS
  12-day up-rate is 54.7% and mean forward return +0.81% — so its "positive
  contribution" is a long tilt in a rising window, matching the Phase-1
  conclusion that the only edge is the bull market.
- **Production-gate dependency (structural finding):** the live
  `EnsembleDecisionStrategy` always keeps news/fundamental weights in its
  coverage denominator. Under faithful production semantics, removing
  *either* technical *or* prediction drops coverage below the 0.5 floor on
  every bar → **0 trades**. The deployed gate is therefore structurally
  incapable of trading without both sources present — the "attendance"
  effect, not signal skill.
- **Regime cold-start:** fold 0 (2022-08..2023-08) has **zero** regime
  evidence (27% of all OOS bars are `n/a`) — the equal-weight-index trend/vol
  classifier needs warm-up history. Regime coverage is only 73.3% overall.
- News, fundamental, and LLM agents have **no historical data path** (125
  live news rows; 7/502 fundamental snapshots; no LLM key) — their
  contribution is **unmeasurable**, and their "remove" row is identical to
  the complete row. This is a data-path gap, not a measurement.

Bottom line: **nothing graduates.** The ensemble should not promote
technical/pattern/regime into live weighting; the prediction signal is the
only candidate, and it is unproven alpha (AUC ≈ random, profit = beta).

## 1. Protocol

- Data / folds / engine / costs / universe: identical to Phase 1
  (`docs/audit/15-phase1-strategy-audit.md` §1) — adjusted Postgres bars
  2021-08-01..2026-07-31, `CalendarWalkForwardValidator.make_folds`, 4 PIT
  folds, `BacktestRunner._simulate`, 10bp commission + 50bp slippage,
  entry thresholds 0.52/0.48.
- Evidence per (symbol, bar ts), point-in-time, for the agents with a
  backtest path:
  - **technical** = `score_technical(snapshot)` ∈ [−1, 1] (per bar);
  - **prediction** = `2·prob_up − 1`, Platt-calibrated per-fold logistic on
    the 11 price features (`price_features_from_bars`), window `[i−59..i]`;
  - **pattern** = sum of event scores (`collect_events`; momentum up/down
    cross ±1, breakout up/down ±1, RSI oversold/overbought ±1) on event bars;
  - **regime** = fold-day `MarketRegimeEngine` label on the equal-weight
    index closes, mapped to a score (BULL +0.5, BEAR −0.5, SIDEWAYS 0;
    EXTREME volatility halves magnitude).
- Fusion: the production `EnsembleDecisionStrategy` (weights technical 0.30,
  pattern 0.15, regime 0.15, prediction 0.40; buy/sell ±0.15; conflict 0.5;
  min-coverage 0.5). Decision → prob: BUY 0.9 / SELL 0.1 / HOLD 0.5, then the
  identical `model_outputs` contract.
- **Coverage semantics (methodology note):** `EnsembleDecisionStrategy`
  seeds its weight table from `DEFAULT_WEIGHTS` (which includes news 0.25 +
  fundamental 0.20) and merges on top. For the ablation table, coverage is
  normalized to each config's active agents (inactive agents — and the
  no-backtest news/fundamental slots — are zeroed), so a "remove X" row
  measures X's *signal contribution*, not gate attendance. The faithful
  production-gate reading (denominator keeps news+fundamental weight) is
  reported separately in §3.
- Reference rows: `prediction_only` (raw per-fold calibrated probs — the P19
  ML baseline) and `always_long` (production control). Both reproduce the
  Phase-1 controls exactly (+25.39% / −3.91% ML; −6.06% / −29.09% AL),
  validating the harness.
- Driver (temp, not committed): `p2_agent_ablation.py` →
  `p2_agent_ablation.json`; prior production-gate-semantics run preserved at
  `p2_agent_ablation_prodgate.json`.

## 2. Agent inventory, backtest path, and per-agent evidence

| agent | purpose | backtest path | input | output | OOS accuracy | latency | contribution (time-12, vs complete) | coverage / failure | verdict |
|---|---|---|---|---|---|---|---|---|---|
| technical | composite indicator score (`score_technical`) | yes | 60-bar indicator series | score ∈ [−1,1] | no label-level accuracy; signal-side drift weak (Phase-1 pattern stats) | 15 µs/call | **−39.9 pp** (drag) | 100% / 0% | **remove weight** (evidence-only) |
| prediction | Platt-calibrated logistic, 11 price features | yes | 60-bar feature window | prob_up ∈ [0,1] | ROC-AUC 0.508, OOS acc 0.575 (base 0.547) | 12 µs/call (features 0.78 ms) | **+28.1 pp** (only positive) | 100% / 0% | **keep** (only positive); retrain; cap weight |
| pattern | indicator events (momentum cross, breakout, RSI) | yes | event bars | ±1 event score | Phase-1 drift +0.02..+0.80%/event gross | 1.4 ms/symbol | **+31.5 pp** (drag) | 17.6% / 0% (sparse) | **remove weight** (evidence-only) |
| regime | equal-weight index trend × vol (`MarketRegimeEngine`) | yes | index closes | BULL/BEAR/SIDEWAYS × vol score | no direct; fold-0 entirely n/a | index-scale classify | **+49.9 pp** (drag) | 73.3% / 26.7% missing (fold-0 cold start) | **remove weight**; fix cold start first |
| news | keyword sentiment (no LLM key) | **no** | news feed | sentiment | unmeasurable (125 live rows) | — | not measured | no backtest path | **gap** — cannot validate |
| fundamental | fundamentals for 7/502 symbols | **no** | current snapshots | value scores | unmeasurable (7 snapshots) | — | not measured | no backtest path | **gap** — cannot validate |
| llm | LLM sentiment | **no** | — (no key) | default 0.0 | unmeasurable | — | not measured | no key → always default | **gap** — cannot validate |

Notes:
- Latency is a microbenchmark (50–200 reps, µs/call): `score_technical` 15.0,
  `LogisticModel.predict` 11.6, feature build 780, `collect_events` 1,440
  µs/symbol, `compute_series` ~400 ms/symbol (one-time warm-up, not per-call).
- "Contribution" is the total-return delta from removing the agent under
  time-12 (positive = removal improves the ensemble = the agent is a drag).
  Bracket rows are negative everywhere (see §3) and are not used for the
  verdict.
- Accuracy of technical/pattern/regime is not a label-level classifier
  accuracy: these agents emit continuous/event signals, so the auditable
  quantities are forward-drift (Phase 1 §6), coverage, and ablation
  contribution.

## 3. Agent ablation (calendar-PIT, 10/50bp, normalized coverage)

Bracket exits (3%/6%):

```
config            trades   ret      win     PF      Sharpe   Sortino  MaxDD     exp($)
complete            336  -32.80%  32.44%  0.841   -0.481   -0.407   -40.77%   -106.80
no_technical        387  -38.40%  31.52%  0.824   -0.580   -0.501   -42.31%   -117.40
no_prediction       374  -37.11%  32.09%  0.829   -0.539   -0.469   -39.32%   -113.55
no_pattern          356  -54.97%  28.65%  0.697   -1.058   -0.890   -59.00%   -202.65
no_regime           344  -44.24%  31.10%  0.768   -0.817   -0.705   -46.20%   -153.67
prediction_only     392   -3.91%  36.48%  0.987    0.034    0.032   -29.95%     -8.51
always_long         360  -29.09%  33.06%  0.867   -0.406   -0.356   -39.73%    -89.09
```

No-target / time-12 exits:

```
config            trades   ret      win     PF      Sharpe   Sortino  MaxDD     exp($)
complete            336  -10.27%  32.44%  0.956   -0.031   -0.029   -40.42%    -28.30
no_technical        338  +29.67%  34.02%  1.123    0.363    0.365   -28.99%    +85.24
no_prediction       350  -38.41%  31.71%  0.811   -0.478   -0.455   -58.99%   -117.51
no_pattern          319  +21.24%  35.42%  1.108    0.326    0.306   -33.56%    +66.39
no_regime           322  +39.62%  35.40%  1.176    0.453    0.420   -22.30%   +113.14
prediction_only     337  +25.39%  32.05%  1.113    0.327    0.316   -25.88%    +77.13
always_long         341   -6.06%  32.26%  0.976    0.023    0.021   -24.52%    -16.42
```

Reading honestly:
- **Every** bracket row is negative — the 3%/6% bracket at 10/50bp loses
  money for every config, including the controls. Bracket deltas are
  confounded by the exit and are not evidence of agent skill.
- Under time-12, the complete ensemble is negative (−10.27%) while three of
  the four removals are positive. Removing **prediction** is the single
  change that *destroys* performance (−38.41%), i.e. prediction is the only
  agent whose presence helps.
- `prediction_only` beats `complete` in both exits (−3.91% vs −32.80%;
  +25.39% vs −10.27%): the other three agents' evidence, as fused today,
  subtract value from the ML signal.
- `always_long` (−6.06% time-12) shows the short side loses money at these
  costs; `prediction_only` (+25.39%) clears that low bar but remains ~70pp
  below the passive equal-weight index (+93.56% over the same window).

### Production-gate semantics (what the deployed gate actually does)

The live strategy seeds its weights from `DEFAULT_WEIGHTS`, so the coverage
denominator always includes news (0.25) and fundamental (0.20) even though
those agents have no historical path. Running the ablation under that faithful
semantics (preserved in `p2_agent_ablation_prodgate.json`):

- complete: bracket **+14.13%** / time-12 **+55.86%** (336/325 trades) — the
  gate only passes bars where ~all four backtestable agents are present, a
  far smaller trade set;
- removing **technical** or **prediction** → **0 trades**: their weight is
  required to reach the 0.5 coverage floor on every bar.

Two consequences: (1) the deployed gate *structurally cannot trade* without
both technical and prediction evidence present — a robust "attendance"
requirement, not evidence either signal is good; (2) absolute performance is
extremely sensitive to which bars pass the gate, so neither the +55.86% nor
the −10.27% complete figure should be over-interpreted. Both are reported
because they answer different questions (deployed-gate behavior vs
signal-contribution measurement).

## 4. Per-agent contribution (time-12 deltas vs complete)

| removed agent | Δ total return | Δ Sharpe | Δ trades |
|---|---|---|---|
| no_technical | +39.94 pp | +0.394 | +2 |
| no_prediction | −28.14 pp | −0.448 | +14 |
| no_pattern | +31.51 pp | +0.356 | −17 |
| no_regime | +49.90 pp | +0.484 | −14 |

## 5. ML validation

Per-fold fit (train window = bars strictly before each fold start; label =
`close[i+12]` vs `close[i]`; features exclude the decision bar at train and
include it at inference — a one-bar shift, no target leakage):

| fold | train samples | fit acc | base rate (up) |
|---|---|---|---|
| 0 | 70,404 | 0.579 | 0.480 |
| 1 | 169,563 | 0.553 | 0.493 |
| 2 | 268,686 | 0.542 | 0.525 |
| 3 | 368,783 | 0.542 | 0.542 |

- Fit accuracy (54.2–57.9%) barely exceeds the base rate (48.0–54.2%) — the
  model is a weak long-tilt, not a skilled classifier.
- **Leakage:** none found. Folds are calendar blocks; training bars are
  strictly before the OOS window; features use only `[i−60..i]`; universe is
  point-in-time (listed strictly before block start).
- **Overfitting:** the 0.58→0.50–0.51 AUC collapse from fit to OOS
  (shuffled-label OOS AUC 0.5017 ≈ real 0.5080) confirms no OOS ranking
  skill.
- **Feature importance** (mean standardized |coef| share across folds):

```
vol_20       0.203    up_ratio_20  0.102    pos_in_range_20  0.064
atr_pct      0.200    ret_10       0.098    volume_ratio     0.058
ret_60       0.135    range_ratio  0.076    ret_5 0.031 / ret_20 0.018 / ret_1 0.016
```

  Fold 0 is dominated by vol_20 (0.435) + atr_pct (0.286) — a pure-volatility
  model in the 2022 bear; later folds spread weight across trend features
  (ret_60, ret_10, up_ratio_20). The model is not stable across folds, and its
  top features are risk/sizing inputs, not directional predictors.

## 6. Prediction quality (pooled OOS, n = 492,007 bars)

- ROC-AUC **0.5080** (shuffled-label control 0.5017) — no ranking skill.
- Base up-rate 0.5471; BUY/SELL/HOLD accuracy 0.5754; mean 12-day forward
  return **+0.81%** (the drift the model is harvesting).
- Up side: precision 0.550, recall 0.775, F1 0.643. Down side: precision
  0.557, recall 0.142, F1 0.227 — the model essentially cannot call downs.
- Calibration: ECE 0.0472, but the **lowest decile is inverted**
  (mean prob 0.450 → realized up-rate 0.569) and the top decile under-predicts
  (0.657 → 0.575); intermediate deciles are fair.
- Confidence bins: 98.4% of predictions sit in the 0.40–0.60 band (realized
  up-rate 54.3–55.5%); high-confidence bins are tiny (0.70–0.80: n=84, up
  0.357; 0.80+: n=39, up 0.615) and statistically meaningless.

**Contrarian (inverted) control** — is the low-probability tail mis-signed? The
engine is long-only (a SELL signal only closes a long; `backtest.py` §SELL,
`risk_calculator.py` "SELL = close an existing position"), so `prob → 1−prob`
is not "short the model" — it is **long-only buying of the model's own
SELL-region bars** (prob ≤ 0.48), the same bars whose pooled up-rate the
calibration shows as inverted. Under time-12 this bought-set wins on paper
(**+47.57%** vs the model's own +25.39%, 132 trades, PF 1.50) while
shuffled same-rate controls lose (−25…−29% across seeds), which superficially
suggests the tail is systematically mis-signed. Fold attribution kills the
claim: the **entire** profit is 42 trades in fold 2 (+$67,966 of $100k);
folds 0/1 lost (−$8.1k/−$4.5k) and fold 3 produced zero trades. Prediction
only, by contrast, earns across folds 0–2 (+27.7k/+1.6k/+4.0k, fold 3 −7.3k).
The contrarian read is a **single-fold, small-sample artifact**, not an edge —
but it does confirm the model's tail calibration is unreliable and worth
re-examining per regime.

## 7. Decision traceability

Traceable today: decision, participating agents, confidence (per trade, from
the decision's prob). **Not traceable:** model version, feature-set hash,
market-regime label at decision time, and risk inputs — `DecisionRecord`
persists `decision/confidence/agent_scores` but no
`features_hash/model_version/regime`, and `ClosedTrade` persists neither.
Sampled trace entries (complete/time-12, 25 trades) are all fold-0 BUYs
(prob 0.9, conf 0.88) with `regime_score: null` — the fold-0 regime cold
start is visible in the trace itself. Fix: extend `DecisionRecord` with
`model_version`, `features_hash`, `regime_label`, and `risk_inputs`.

## 8. Verdicts & follow-ups

**Verdict: no agent adds a cost-adjusted alpha edge; nothing graduates.**

- Prediction is the only positive contributor (+28.1 pp time-12) and the
  closest thing to a signal — but its AUC is statistically indistinguishable
  from random and its profit is the market's drift.
- Technical, pattern, and regime are net drags in the fused ensemble as
  weighted today (−39.9 / −31.5 / −49.9 pp) and should not be promoted.
- News/fundamental/LLM contribution is unmeasurable (no historical path) —
  a data gap, not a result.
- Bracket exits remain uniformly lossy; time-12 is the only configuration
  where the ML long-tilt shows value, and even that is far below passive.

Follow-ups (correctness/measurement fixes, not tuning):
1. Re-weight or gate out technical/pattern/regime and re-validate the
   prediction-only ensemble on a fresh holdout before any promotion.
2. Fix the regime cold start (fold 0 = 0% coverage): extend the equal-weight
   index history so the trend/vol classifier is labelled from fold 0.
3. Persist `model_version`, `features_hash`, `regime_label`, `risk_inputs`
   on `DecisionRecord` to make decisions fully traceable.
4. Investigate the inverted low-probability decile (0.45 → 0.57); consider
   regime-conditional Platt calibration.
5. Re-run with the extended adjusted data (DB now holds to 2026-08-07) so the
   OOS window shrinks the `n/a` regime bucket.
6. Keep everything behind the gate; the live agent path ≠ backtest path
   (Phase-7 finding) and must be closed before any graduation.
7. Re-check the low-probability tail per fold/regime: the inverted-tail
   profit is concentrated in fold 2 (42 trades) and does not replicate; if a
   regime-conditional recalibration ever fixes the tail, re-validate on a
   fresh holdout before treating it as a signal.

## Appendix — Reproducing

- Harness (temp, not committed): `p2_agent_ablation.py` →
  `p2_agent_ablation.json` (+ caches `p2_bars/series/models/evidence/sims/
  pairs/trades*.pkl`, log `p2_ablation_norm_console.log`).
- Production-gate-semantics run: `p2_agent_ablation_prodgate.json`.
- Contrarian control: `p2_contrarian.py` → `p2_contrarian.json`,
  fold attribution in `p2_contrarian_folds.py` (temp).
- Cross-checks: `prediction_only` and `always_long` rows reproduce the
  Phase-1 controls exactly; `pattern`/`regime`/`technical` coverage and the
  fold-0 regime absence are visible in the JSON `agent_report` and
  traceability sample.
- All results are on the backtest engine with fixed 10/50bp costs; live
  trading remains forbidden.
