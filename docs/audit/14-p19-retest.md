# Phase 19 — A/B Re-test: Calendar-Aligned PIT Walk-Forward vs Aligned Baseline

Date: 2026-08-08. Baseline: `a459406`. Workstream: P19 fix #2 (validation)
implemented in `src/qtrader/application/services/calendar_walk_forward.py`
(committed `3e8e4cd`), re-tested against the aligned validator. Fix #4 (exits)
and fix #5 (costs) are exercised here as protocol choices. Fix #1 (adjusted
prices) is now implemented (adjclose merge in the Yahoo parser + heal-capable
upsert) — every number below is on split/dividend-adjusted bars.

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

Model diagnostics (per-fold train, PIT): samples 70,404 → 368,783 (unchanged by
fix #1 — adjusted prices preserve bar counts); fit accuracy 0.579 → 0.542;
forward-12-bar base rate 0.480 → 0.542 (bull drift); Platt `calib_a` unstable
(−0.407, 0.208, 0.526, 0.072).

## 3. P19 model variants (fixed 0.60/0.40, calendar-PIT folds, adjusted bars)

```
V1 bracket 3/6, costs 1/5bp       trades= 261  win=37.55%  pf=1.159  sharpe= 0.395  dd=-18.78%  ret= +28.67%
V2 bracket 3/6, costs 10/50bp     trades= 264  win=33.71%  pf=0.948  sharpe=-0.067  dd=-27.46%  ret=  -8.99%
V3 no-target/time-12, 10/50bp     trades= 233  win=31.33%  pf=1.329  sharpe= 0.564  dd=-22.57%  ret= +58.37%
```

## 4. Controls (same calendar folds, fixed threshold, adjusted bars)

Always-long const 0.55 (dummy model path) and shuffled-label models:

```
C1 AL bracket 3/6, 10/50bp        trades= 380  win=34.21%  pf=0.989  sharpe= 0.004  dd=-28.96%  ret=  -5.93%
C2 shuffled labels, 10/50bp       trades= 128  win=29.69%  pf=0.773  sharpe=-0.390  dd=-30.98%  ret= -19.44%
AL no-target/time-12, 10/50bp     trades= 328  win=32.62%  pf=0.929  sharpe=-0.167  dd=-39.36%  ret= -19.25%
AL bracket 3/6, 1/5bp             trades= 296  win=36.82%  pf=1.104  sharpe= 0.326  dd=-20.81%  ret= +18.91%
```

Momentum-v0 heuristic (the live fallback, model=None path):

```
MOM no-target/time-12, 10/50bp    trades=1057  win=32.92%  pf=0.932  sharpe=-0.423  dd=-49.85%  ret= -37.67%
MOM bracket 3/6, 10/50bp          trades=1108  win=33.57%  pf=0.816  sharpe=-0.897  dd=-79.27%  ret= -75.26%
MOM bracket 3/6, 1/5bp            trades=1143  win=37.88%  pf=1.133  sharpe= 0.841  dd=-20.57%  ret=+139.47%
```

## 5. Verdict (final-report acceptance bar: beat always-long AND shuffled at ≥10bp, on calendar-correct OOS)

| variant | ret > always-long? | ret > shuffled? | pass? |
|---|---|---|---|
| V2 bracket 3/6 @ 10/50bp | **no** (−8.99 vs −5.93) | yes (−8.99 vs −19.44) | **NO** |
| V3 no-target/time-12 @ 10/50bp | yes (+58.37 vs −5.93; matched-exit AL −19.25) | yes (+58.37 vs −19.44) | **YES** |

- **V2 (bracket) fails on clean data**: at realistic costs the ML entry is not
  distinguishable from the always-long control (−8.99% vs −5.93%). On raw bars
  it beat the always-long control only because split-date discontinuities were
  present in the OOS window; once prices are adjusted the bracket variant shows
  no edge over simply being long. Consistent with the phase-5/7 ablation.
- **V3 (no-target/time-12) passes, and the pass is robust to the data fix**:
  the fixed-threshold model with the exit redesign beats the always-long
  control (+58.37 vs −19.25, matched exits; vs −5.93 bracket-exit control) and
  the shuffled control (+58.37 vs −19.44) at 10/50bp on adjusted bars. The
  clean-data re-run *improves* V3 (+45.19 → +58.37) while *harming* the
  always-long control (+9.55 → −19.25): split artifacts were inflating the
  buy-and-hold control more than the model. The ML entry edge only shows in
  combination with the redesigned exit, and it is now measured on clean prices.
- **Momentum-v0 stays dead at real costs**: +139.47% at 1/5bp collapses to
  −75.26% at 10/50bp (1108 trades) — the live fallback is not viable and
  cannot be gated on.

## 6. Cross-checks and caveats

- Internal consistency: C1 always-long bracket @10/50bp = −5.93% is reproduced
  exactly by the matched-exit control run (two independent script executions,
  same as the raw-data run reproduced −2.28%).
- V1 calendar-PIT @1/5bp bracket gives **+28.67%** on adjusted bars (raw:
  +39.84%). The old aligned baseline persisted +9.23% on raw bars at the same
  costs/exits — non-comparable, since that OOS was compressed into 2022-2023.
- **Fix #1 materially changes the numbers**: always-long at 1/5bp drops
  +36.68 → +18.91 and at 10/50bp drops −2.28 → −5.93; shuffled labels drop
  +8.00 → −19.44; the model variants move in the same direction but V3
  *improves* (+45.19 → +58.37). Raw data was inflating the controls (which ride
  split discontinuities with no way to react) more than the model. The
  acceptance-bar conclusion is unchanged and now sits on clean prices.
- Cost model asymmetry: the engine charges commission+slippage on entries;
  exit fills (stop/target/time/close) carry zero commission. Identical across
  variants/controls, so the A/B is fair, but absolute numbers understate exit
  costs slightly.
- Validator still rejects genuine crash bars: e.g. GL's −53% single-day crash
  (2024-04-12, a real move, not a split) exceeds `max_single_bar_move_pct=0.5`
  and is dropped on both raw and adjusted data. This is conservative and
  symmetric across variants/controls.
- V3 is a **single realization** at a deliberately-fixed (non-tuned) threshold:
  immune to the threshold lottery by construction, but not a fresh holdout. A
  regime-aware, additional out-of-sample period (e.g. 2026-08 onward) is the
  next test before trusting V3's magnitude.

## 7. Status

- Implemented & committed: fix #2 (calendar-PIT validator, `3e8e4cd`).
- Implemented (fix #1, this commit): `parse_chart_response` merges `adjclose`
  and scales OHLC by `adjclose/close` (fallback to raw when `adjclose` is
  absent/null); `SQLAlchemyPriceRepository.upsert_bars` now heals existing rows
  on conflict instead of ignoring them, so a re-backfill rewrites raw bars to
  adjusted ones in place. Unit suite 337 passed (3 new parser adjustment
  tests); integration suite green incl. new upsert-heal test; ruff + mypy
  clean.
- Applied: full D1 re-backfill 2021-08-01→2026-08-07 for all 502 S&P 500
  symbols through the production `DataAgent` path (cleaner + validator); rows
  624,820 → 625,348 (+528 split-date bars previously rejected on raw data).
- Protocol-applied (no code change): fix #4 (no-target/time-12 exit) and
  fix #5 (10/50bp gate) — both exercised above.
- Re-test on adjusted bars confirms V3 passes the acceptance bar; momentum-v0
  and the bracket variant fail. Next: out-of-sample test beyond 2026-07.
