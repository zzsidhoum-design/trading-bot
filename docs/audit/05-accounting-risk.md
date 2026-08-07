# Backtest Accounting & Risk-Sizing Audit (Phase 14–15 findings)

Date: 2026-08-07. Baseline commit: `f7cff7b`. Reproduced in-session.

## 1. The headline metrics disagree with each other — by design

`PerformanceMetrics` computes the four gate metrics from different bases:

| metric | basis |
|---|---|
| win rate | `(n − losses)/n`, **equal-weighted per trade**, pnl==0 counts as a win |
| profit factor | `gross_profit/gross_loss` on **per-trade pnl%** (`pnl/entry_cost`) — equal-weighted per trade |
| total return / Sharpe | dollar-weighted via the **equity curve** (position size × outcome, compounded) |

When position sizes are constant these agree. In this backtest they are not, so
they can point in opposite directions.

### Reproduced on fold3 (calibrated, 0.55/0.45)

```
trades=667  wins=190  losses=477
reported PF (equal-wt pnl%) = 0.741     <- "losing"
dollar-weighted PF          = 1.329     <- "winning"
total pnl $ = 4,274 (≈ +4.27% on 100k)  <- matches equity return +4.27%
avg entry cost wins   = 1,609
avg entry cost losses = 905             <- wins ~1.8x larger than losses
```

The reported PF says the strategy lost; the equity curve gained +4.27%. Both are
"true" — on different bases. The same mechanism explains the uncalibrated
aggregate's contradiction (reported PF 1.223, win 40.08%, but return −5.93%):
there the *losing* trades carried the larger positions.

**Consequence:** the gate (and any reader) cannot trust PF/win-rate and
total-return to be mutually consistent. A decision made on one base may not
hold on the other. Metrics must be recomputed on a single mark-to-market
equity basis.

## 2. "1% risk per trade" is not enforced

Position sizing (`risk_calculator.py:66-74`):
```
atr_stop       = atr * 1.5
position_size  = equity * 0.01 / atr_stop
```
The actual bracket (`backtest.py:536-537`) is a **fixed 3% stop / 6% target**
from `BacktestParams`. The two stops disagree, so the realized risk per trade is:

```
actual_risk = 1% * (3% / (ATR*1.5))
```
- when ATR*1.5 < 3% → position is **oversized**, realized risk > 1% of equity;
- when ATR*1.5 > 3% → position is **undersized**, realized risk < 1%.

Observed extremes: min position ≈ $78, max position ≈ **$43,344 on a $100k
account (43% of equity)**. A 3% stop on that position is ~$1,300 = 1.3% equity
risk — above the stated 1%. The risk engine's computed `take_profit` (2R of the
ATR stop) is also ignored; the backtest closes at the fixed 6% instead.

## 3. Several risk limits are inert in the backtest

`_queue_buy` (backtest.py:496-501) hardcodes:
```
current_exposure_pct=0.0, sector_exposure_pct=0.0,
cooldown_remaining_minutes=0.0, daily_pnl_pct=0.0, trades_today=0
```
Therefore in every backtest these `RiskPolicy` limits are **never triggered**:
`max_portfolio_exposure_pct` (80%), `per_sector_limit_pct` (40%),
`min_cooldown_minutes` (5m), `max_daily_loss_pct` (3%), and
`max_trades_per_day` (10). Only `max_positions` (10) and the ADV liquidity cap
(≤1% of daily dollar volume) actually bind. The backtest risk behavior is
therefore looser than the live risk policy claims.

## 4. Other execution notes

- `_intrabar_exit` resolves stop-before-target when both are touched in one bar
  (conservative), and applies no slippage to the exit leg of an intrabar fill
  (only the queued-next-open fills carry slippage).
- Forced-flat at test end (`outcome="end_of_test"`) is included in trades/PF but
  is a fold artifact, not a real exit.
- No trade-level rows are persisted (see 02 §3.6); these diagnostics required a
  dedicated re-run.

## 5. Recommended fixes (remediation phase, not applied here)

1. **Single basis for metrics**: compute win rate, PF, Sharpe, return from one
   mark-to-market equity path; or report both equal-weighted and
   dollar-weighted PF with labels.
2. **Align sizing stop with the bracket**: either size off the actual 3%/6%
   stop or drive the bracket from ATR*1.5 (one source of truth).
3. **Enforce the real risk cap**: bound position ≤ (risk% / stop%) × equity and
   cap single-position exposure; re-enable exposure/sector/daily-loss/trade
   limits in the backtest by passing live state, not zeros.
