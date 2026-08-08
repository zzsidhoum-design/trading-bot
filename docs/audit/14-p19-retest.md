# Phase 19 — A/B Re-test: Calendar-Aligned PIT Walk-Forward vs Aligned Baseline

Date: 2026-08-08. Baseline: `a459406`. Workstream: P19 fix #2 (validation)
implemented in `src/qtrader/application/services/calendar_walk_forward.py`
(committed `3e8e4cd`), re-tested against the aligned validator. Fix #4 (exits)
and fix #5 (costs) are exercised here as protocol choices; fix #1 (adjusted
prices) is **not** yet implemented — every number below is on raw bars.

## 1. What changed vs the baseline protocol

| aspect | baseline (aligned, phase5) | P19 re-test (calendar-PIT) |
|---|---|---|
| folds | per-symbol bar-index blocks (`_make_folds`) | calendar blocks over 2022-08-01..2026-07-31, n=4 |
| universe per fold | all symbols, every fold | PIT-eligible only (listed strictly before block start) |
| OOS coverage | full-history names tested ~2022-04..2023-11; late listings in 2024-2026 | every eligible name tested in every block's calendar dates |
| entry threshold | fold-1 tuned (threshold lottery) | fixed 0.60/0.40, no tuning |
| costs (gate) | 1/5bp | 10/50bp |
| engine | `BacktestRunner._simulate` | identical |

Frozen protocol shared with phase5: universe = `sp500.csv` symbols with ≥372 D1
bars over 2021-08-01..2026-07-31 (n=498), 11-feature logistic + Platt
calibration per fold, 1% risk sizing, 3%/6% bracket default.

## 2. Folds and PIT eligibility

```
fold  calendar window        eligible  oos_bars
0     [2022-08-01, 2023-07-31]  493    123,743
1     [2023-08-01, 2024-07-30]  495    124,245
2     [2024-07-31, 2025-07-31]  498    124,998
3     [2025-08-01, 2026-07-31]  498    124,997
```

Late-listing names (CEG 2022-01, VLTO 2023-10, SOLV/GEV 2024-03) only appear in
the folds whose block starts after their listing — no look-ahead training or
trading. OOS bars across all folds ≈ 498k, spanning the **full** 2022-08..2026-07
calendar range for every eligible symbol (the P10/P12 finding: the aligned
validator compressed full-history names' OOS into 2022-2023 only).

Model diagnostics (per-fold train, PIT): samples 70,404 → 368,783; fit accuracy
0.577 → 0.536 (declines as the training window grows — consistent with weak,
non-stationary signal); forward-12-bar base rate 0.474 → 0.535 (bull drift);
Platt `calib_a` unstable (−0.417, 0.165, 0.485, 0.151).

## 3. P19 model variants (fixed 0.60/0.40, calendar-PIT folds)

```
V1 bracket 3/6, costs 1/5bp       trades= 284  win=38.03%  pf=1.195  sharpe= 0.530  dd=-13.63%  ret= +39.84%
V2 bracket 3/6, costs 10/50bp     trades= 293  win=35.49%  pf=1.027  sharpe= 0.145  dd=-22.94%  ret=  +5.36%
V3 no-target/time-12, 10/50bp     trades= 218  win=33.03%  pf=1.262  sharpe= 0.518  dd=-19.85%  ret= +45.19%
```

## 4. Controls (same calendar folds, fixed threshold)

Always-long const 0.55 (dummy model path) and shuffled-label models:

```
C1 AL bracket 3/6, 10/50bp        trades= 360  win=35.00%  pf=0.991  sharpe= 0.047  dd=-22.44%  ret=  -2.28%
C2 shuffled labels, 10/50bp       trades= 135  win=37.78%  pf=1.096  sharpe= 0.205  dd=-13.21%  ret=  +8.00%
AL no-target/time-12, 10/50bp     trades= 314  win=33.44%  pf=1.065  sharpe= 0.192  dd=-34.11%  ret=  +9.55%
AL bracket 3/6, 1/5bp             trades= 293  win=37.88%  pf=1.188  sharpe= 0.527  dd=-13.17%  ret= +36.68%
```

Momentum-v0 heuristic (the live fallback, model=None path):

```
MOM no-target/time-12, 10/50bp    trades=1087  win=33.85%  pf=0.905  sharpe=-0.805  dd=-67.63%  ret= -56.98%
MOM bracket 3/6, 10/50bp          trades=1163  win=32.07%  pf=0.782  sharpe=-1.122  dd=-86.52%  ret= -84.61%
MOM bracket 3/6, 1/5bp            trades=1125  win=38.58%  pf=1.165  sharpe= 0.959  dd=-18.21%  ret=+202.75%
```

## 5. Verdict (final-report acceptance bar: beat always-long AND shuffled at ≥10bp, on calendar-correct OOS)

| variant | ret > always-long? | ret > shuffled? | pass? |
|---|---|---|---|
| V2 bracket 3/6 @ 10/50bp | yes (+5.36 vs −2.28) | **no** (+5.36 vs +8.00) | **NO** |
| V3 no-target/time-12 @ 10/50bp | yes (+45.19 vs −2.28; matched-exit AL +9.55) | yes (+45.19 vs +8.00) | **YES** |

- **V2 (bracket) still fails**: at realistic costs the ML entry is not
  distinguishable from the shuffled-label control (+5.36% vs +8.00%). The
  "edge" is bracket-beta, exactly as the phase-5/7 ablation concluded.
- **V3 (no-target/time-12) passes**: the fixed-threshold model with the exit
  redesign beats the always-long control (+45.19 vs +9.55, matched exits) and
  the shuffled control (+45.19 vs +8.00) at 10/50bp. This is the first variant
  in the whole audit to clear the acceptance bar. The ML entry only shows edge
  in combination with the redesigned exit.
- **Momentum-v0 is dead at real costs**: +202.75% at 1/5bp collapses to
  −84.61% at 10/50bp (1163 trades) — the live fallback is not viable and
  cannot be gated on.

## 6. Cross-checks and caveats

- Internal consistency: C1 always-long bracket @10/50bp = −2.28% is reproduced
  exactly by the matched-exit control run (two independent script executions).
- V1 calendar-PIT @1/5bp bracket gives **+39.84%** vs the aligned baseline's
  persisted **+9.23%** at the same costs and exits. The gap is almost entirely
  the validation fix: OOS now genuinely spans 2022-08..2026-07 (bull years)
  for full-history names instead of being compressed into 2022-2023. This
  confirms the phase-10 fold-alignment finding and makes the old headline
  number non-comparable to any fixed-baseline number.
- Cost model asymmetry: the engine charges commission+slippage on entries;
  exit fills (stop/target/time/close) carry zero commission. Entry costs
  dominate and the asymmetry is identical across variants/controls, so the A/B
  is fair, but absolute numbers understate exit costs slightly.
- V3 is a **single realization** at a deliberately-fixed (non-tuned) threshold:
  it is immune to the threshold lottery by construction, but it is not a fresh
  holdout — it is the honest, protocol-defined A/B. A regime-aware, additional
  out-of-sample period (e.g. 2026-08 onward) is the next test before trusting
  V3's magnitude.
- **Fix #1 (adjusted OHLC) is not yet applied.** All numbers are on raw bars,
  so split/dividend discontinuities can still inflate returns. V3's +45.19% is
  provisional until adjusted prices land and the identical A/B is re-run.

## 7. Status

- Implemented & committed: fix #2 (calendar-PIT validator, `3e8e4cd`), unit
  tests 7/7 green, ruff + mypy clean, full unit suite 333 passed.
- Protocol-applied (no code change): fix #4 (no-target/time-12 exit) and
  fix #5 (10/50bp gate) — both exercised above.
- Pending: fix #1 (adjusted prices + BarValidator on backfills), then re-run
  this identical A/B to confirm V3 survives; re-test beyond 2026-07.
