# Phase 5 — Strategy Ablation & Win-Rate Decomposition

Date: 2026-08-08. Baseline: `a459406`. Method: controlled ablation on the frozen
protocol — same universe (498, sp500.csv, ≥372 bars), same folds
`[(72,175)..(484,587)]`, same per-fold calibrated logistic model (11 price
features, Platt scaling), same simulation (1% risk sizing, 3%/6% bracket, 1bp
commission + 5bp slippage, sector exposure caps). Model probabilities are
precomputed once per fold and reused; only the ablating variable changes.
Production-faithful config = calibrated + sectors (matches
`walk_forward._simulate_fold`).

## 1. Ablation results (test folds 2–4, aggregate)

| variant | trades | win% | PF | Sharpe | DD% | ret% |
|---|---|---|---|---|---|---|
| **A baseline** (fold1-tuned 0.7/0.3) | 35 | 22.86 | 0.504 | −0.490 | −12.48 | **−12.48** |
| C threshold 0.52/0.48 | 106 | 42.45 | 1.368 | 0.654 | −5.81 | +23.39 |
| C threshold 0.55/0.45 | 133 | 38.35 | 1.199 | 0.486 | −7.09 | +16.01 |
| C threshold 0.60/0.40 | 128 | 41.41 | 1.294 | 0.521 | −8.01 | +22.20 |
| C threshold 0.65/0.35 | 41 | 43.90 | 1.504 | 0.712 | −1.72 | +12.13 |
| C threshold 0.70/0.30 | 35 | 22.86 | 0.504 | −0.490 | −12.48 | −12.48 |
| **B no-signal** (constant prob 0.55, always long when flat) | 98 | 45.92 | **1.685** | 0.662 | −5.81 | **+38.87** |
| D bracket 3/6 + time 12 | 136 | 43.38 | 1.275 | 0.519 | −8.01 | +21.79 |
| D bracket 3/6 + time 5 | 184 | 47.28 | 1.187 | 0.463 | −6.85 | +16.04 |
| D **no target, time 12** | 92 | 38.04 | **1.717** | 0.606 | −10.43 | **+42.91** |
| D trailing 3 (target off) | 207 | 45.41 | 1.004 | 0.034 | −10.61 | −0.29 |
| D trailing 5 (target off) | 87 | 42.53 | 1.619 | 0.433 | −11.81 | +26.18 |
| **F shuffled-label model** (fold1-tuned 0.55/0.45) | 128 | 33.59 | 0.943 | −0.064 | −16.43 | −4.76 |

## 2. Findings

1. **The signal has no positive value. The "edge" is bracket math + market beta.**
   The no-signal control — buy every name whenever flat, exit only at the 3%/6%
   bracket — produced the **best** result of any variant (PF 1.685, +38.87%).
   Every model-driven configuration was worse. The model contributes no
   information; it actively subtracts value by gating entries worse than a
   coin-flip long bias.
2. **Win rate is a product of the 1:2 R:R bracket, not prediction.**
   38.78% win with avg-win/avg-loss ≈ 1.76–1.89 (close to the 2.0 bracket cap)
   is exactly what a nearly-random directional signal *with* a 2:1 bracket
   produces: it needs only ~33% wins to break even before the 6% margin, and
   the ~0.55 model accuracy is barely above the ~0.50 coin-flip base rate.
   EV math: EV/trade ≈ +0.16R (R=3% risk) minus ~6bp round-trip costs — the
   whole headline depends on this slim bracket-margin arithmetic.
3. **Threshold tuning is a lottery.** The identical OOS window swings from
   +23.4% (0.52) to −12.5% (0.70) purely with the entry threshold. The
   fold1-tuning pick (0.7/0.3) produced the *worst* OOS (−12.5%) — classic
   selection-on-noise. The "38.78%" baseline is one draw from this lottery.
4. **The 6% target cap is a drag.** Removing the target and exiting at 12 bars
   (let winners run) improves PF to 1.717 and return to +42.9% — nearly double
   the baseline. The engineered bracket, not the model, is doing all the work
   — and it caps the winners doing so.
5. **A shuffled-label model ≈ the real model.** Trained on randomly permuted
   labels, the model still produced a similar trade flow and near-baseline
   results under the same tune+test protocol (PF 0.943 vs 1.294 at the default
   threshold). The pipeline cannot separate signal from noise: **the strategy
   is not distinguishable from an overfit artifact.**
6. **Model diagnostics confirm weak signal.** Per-fold in-sample accuracy:
   55.1–57.2% vs a ~50% base rate (coin flip); Platt calibration slope is
   unstable across folds (calib_a −0.08 to +0.72), so probabilities are not
   reliable. Coefficients are consistent in sign (ret_20 momentum +, ret_60
   reversal −) — a real but *tiny* statistical tendency, not a tradable edge.

## 3. Why "40.1%" and "38.78%" and why they are not reproducible

- **40.1% = the uncalibrated reversal-feature experiment**
  (`wf_v2_rev_nocalib.log`): trades=494, win=40.08%, PF 1.223, ret −5.9%,
  GATE **DENIED** (sharpe −0.26 < 1.00; ret < 0). Never persisted.
- **38.78% = the persisted baseline** (`strategy_performance` id145: 477
  trades, PF 1.195, ret +9.23%, from `wf_v2.log`, 11-feature model tuned
  0.6/0.4). Also a non-persisted CLI run.
- Prior experiments were **configuration-inconsistent**: calibration on/off,
  sectors on/off, feature sets changed per experiment, thresholds tuned per
  experiment. Identical models + folds with sectors on vs off yield 44 vs 307
  fold-1 trades; calibration on/off flips the aggregate between +9.2% and
  −12.5%. **No single headline survives reproduction from the artifacts.** The
  win-rate *arithmetic* (wins/closed trades) is correct; the number is simply
  meaningless as evidence of edge.

## 4. Answers for the brief

- Was the 40.1% win rate calculated correctly? The **formula** is correct
  (wins ÷ closed trades). But 40.1% comes from an unpersisted, gate-DENIED,
  configuration-specific experiment, not the persisted system result.
- Which component generates the return? **The 1:2 bracket + equity beta in a
  bull OOS window + near-zero costs.** The ML signal, threshold, and feature
  engineering contribute nothing measurable (and the tuned threshold is
  actively harmful out-of-sample).
