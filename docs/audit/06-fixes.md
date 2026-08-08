# Risk-Sizing & Metrics Remediation (Phase 14–15 fixes)

Date: 2026-08-08. Applies the fixes recommended in `05-accounting-risk.md` to the
frozen baseline (`f7cff7b`), then re-runs the walk-forward gate protocols to see
whether the verdict changes.

## 1. What changed

Three edits, all backward compatible (defaults preserved), all on top of the
frozen baseline:

### 1.1 Sizing aligned to the actual bracket — 1% risk is now real

`RiskInputs` gained an optional `atr_stop_distance` (`risk_calculator.py`); when
present it replaces the ATR-derived stop for sizing, stop and target. The
backtest now passes the bracket it will actually fill at
(`backtest.py:566`: `entry * stop_loss_pct`), so:

```
position_size = equity * risk_per_trade_pct / (entry * stop_loss_pct)
             = 100000 * 1% / (entry * 3%)        = equity / (3 * entry)
```

A 3% stop on a `equity/3` position is exactly 1% of equity. Verified on a 12
symbol / 1255 bar smoke run: first position notional was $33,385 on $100k
(33.4% of equity), i.e. a 3% stop loses ~1%. The old code sized off
`ATR * 1.5` while the stop filled at the fixed 3%, so realized risk ranged
0.5%–1.3% and positions reached $43k (43% of equity).

### 1.2 Inert risk limits re-enabled with real state

`_queue_buy` no longer hardcodes `current_exposure_pct=0.0`,
`sector_exposure_pct=0.0`, `cooldown_remaining_minutes=0.0`,
`daily_pnl_pct=0.0`, `trades_today=0`. A `_SimContext` (`backtest.py:136`)
threads real, mark-to-market state into every candidate buy:

- `current_exposure_pct` — live sum of open-position notional / equity;
- `sector_exposure_pct` — max sector notional / equity, from an optional
  symbol→sector map (the walk-forward universe supplies one from the CSV);
  when no map is given the sector limit stays unenforceable by construction
  and only the total-exposure cap binds;
- `daily_pnl_pct` — return of today's equity vs. the day-open mark (resets per
  trading day, `backtest.py:411/419`);
- `trades_today` — executed fills that day (both legs, `backtest.py:433/438/461`);
- `cooldown_remaining_minutes` — minutes since that symbol's last exit.

This mirrors what the live `RiskAgent` observes, so
`max_portfolio_exposure_pct` (80%), `per_sector_limit_pct` (40%),
`min_cooldown_minutes` (5m), `max_daily_loss_pct` (3%) and
`max_trades_per_day` (10) now actually bind in the backtest.

### 1.3 Profit factor on a single dollar-weighted basis

`PerformanceMetrics.from_series` accepts parallel `trade_pnl_amounts`;
`backtest.py:518` and `walk_forward.py` pass `[t.pnl ...]`, so PF is
`gross_profit_$ / gross_loss_$` — the same dollar-weighted basis as total
return and Sharpe. Win rate is unchanged (sign is identical). The old PF summed
per-trade `pnl%`, which let oversized winners/losers distort the ratio (fold3:
reported 0.741 vs dollar 1.329).

## 2. Walk-forward re-runs (same protocol, same folds, same trained models)

Models are fit on raw bars, so the fixes only touch the execution and metrics
layer. `wf_v2_rev.py` → `wf_v2_rev_fixed.py` (added sectors + dollar amounts).

### Calibrated protocol (`0.52/0.48…` tuned on fold1)

| metric | baseline (old engine) | fixed engine |
|---|---|---|
| trades | 1262 | **67** |
| win rate | 34.55% | **40.30%** |
| profit factor | 1.008 (pnl% basis) | **1.210 (dollar basis)** |
| Sharpe | 0.784 | **0.300** |
| max DD | −8.05% | −6.70% |
| total return | +25.45% | +8.61% |
| gate | DENIED (win, PF, Sharpe) | **DENIED (Sharpe 0.30 < 1.00)** |

### Uncalibrated protocol (`--no-calib`)

| metric | baseline (old engine) | fixed engine |
|---|---|---|
| trades | 494 | **123** |
| win rate | 40.08% | **40.65%** |
| profit factor | 1.223 (pnl% basis) | **0.995 (dollar basis)** |
| Sharpe | −0.257 | **0.011** |
| max DD | −10.44% | −9.85% |
| total return | −5.93% | −0.94% |
| gate | DENIED (Sharpe, ret) | **DENIED (PF, Sharpe, ret)** |

## 3. Verdict: the gate does NOT flip

The remediation moves real numbers, but both protocols still **DENY**:

- calibrated: win rate (40.30% ≥ 39.33%) and dollar PF (1.210 ≥ 1.20) now pass
  their thresholds; the only remaining blocker is **Sharpe 0.30 < 1.00**;
- uncalibrated: win rate stays passing (40.65%) but dollar PF drops to 0.995
  (the pnl%-basis 1.223 was inflated by the small-winner/large-loser skew the
  old metric hid) and return is still negative.

Trade counts collapse (1262→67, 494→123) because 1%-risk / 3%-stop sizing on
$100k makes each position ~⅓ of equity, and the 80% exposure + 40% sector caps
limit the book to ~2 concurrent positions (fold4 calibrated: 0 fills).

## 4. Persisted gate row (unchanged by this work)

The gate evaluates `latest_for_strategy` (`period_end DESC, id DESC`). Both
persisted `walk-forward` rows end 2026-07-31; the higher-id row is the stale
5y aggregate:

```
477 trades, win 38.78%, PF 1.195, Sharpe 0.327, ret +9.23% → DENIED
(win 38.78 < 39.33 derived, PF 1.195 < 1.20, Sharpe 0.327 < 1.00)
```

These fixes are a prerequisite for the gate to measure the system honestly, but
they do not flip the live verdict, and they do not rewrite already-persisted
rows.

## 5. Scope / not fixed here

Out of scope for this remediation (documented separately): backtest signals ≠
live 4-agent ensemble (`03-experiments.md`), registered model never used by the
prediction agent, bar-index (not calendar) fold alignment compressing OOS for
2024 listings, and the pnl==0-counts-as-win edge in win rate.
