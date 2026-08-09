# Phase 3 — Final Validation & Stress Testing

Date: 2026-08-09. Engine at HEAD `7da9472` (fixed engine: exit costs,
adjusted bars, calendar-PIT validator). Work request: *"final validation —
re-run the system vs the original baseline; out-of-sample on genuinely unseen
data; stress (bull/bear/sideways, low/high/extreme vol, news events, price
gaps); failure testing (provider/API/network/missing/delayed/db/agent — one
agent failure must never cause unsafe trading); risk testing (position size,
exposure, daily loss, unreliable data, duplicate orders, stops); paper trading
only after the previous tests pass; final report with a verdict."*

Everything below is measured on the same fixed engine, calendar-PIT folds,
universe (498 S&P 500, ≥372 D1 bars), and costs (10bp commission + 50bp
slippage) as Phases 1–2 so the numbers are directly comparable. Nothing was
tuned to raise win rate. **Live money stays forbidden.**

## Executive verdict

**NOT READY FOR PAPER TRADING.**

- **The system as it is actually configured loses money.** The deployed
  decision path is `EnsembleDecisionStrategy` with `DEFAULT_WEIGHTS`
  (technical .30 / news .25 / fundamental .20 / prediction .25; news and
  fundamental have no data source). Measured on the OOS window this
  production ensemble returns **−24.44%** (time-12) / **−31.05%** (bracket) —
  *worse than the always-long control* (−6.06% / −29.09%).
- **The only positive configuration** (prediction-only: **+25.39%** time-12)
  was selected from the same window it is measured on; it is **not
  out-of-sample validated**, so it cannot be adopted.
- **The genuinely-unseen holdout shows no alpha.** Only 5 trading days
  (2026-08-03..08-07) exist beyond the fold-3 OOS window. On those 1,991
  unseen bars the prediction signal's buy hit rate (56.28%) equals the
  always-long base rate (56.25%). The prediction agent's ROC-AUC is 0.508 vs
  a shuffled control 0.502 (Phase 2) — the profit is market drift, not skill.
- **Paper trading is correctly denied by the system itself.** Running the
  real `SystemGate` against the persisted performance denies PAPER *and*
  LIVE (win rate 38.78% < 39%, PF 1.19 < 1.20, Sharpe 0.33 < 1.00), audited
  into `system_logs`. Per the work request's own precondition ("only after
  the previous tests have passed") paper trading was **not run**.
- **Risk-manager gaps in the live/paper wiring** (daily-loss and ADV limits
  can never fire; stop-loss orders are never submitted to the broker; missing
  ATR falls back to a 2% proxy; decision-time prices are not checked for
  freshness) are now covered by executable tests and must be fixed before any
  paper-trading attempt.

## 1. Re-run the improved system vs the original baseline

**Definition.** No product-code improvement was implemented by the audits
(changing weights based on in-window results would be overfitting). "Improved
system" is therefore measured as *candidates* on the fixed engine; the
deployed system is the production ensemble, measured for the first time in
§1.2.

### 1.1 Original persisted baseline (`strategy_performance` ids 143–145)

Pre-fix engine, persisted before the engine corrections (exit costs, adjusted
bars, calendar-PIT). Not directly comparable to §1.2 but reproduced verbatim.
Expectancy was not persisted.

| id | config | window | trades | win | PF | Sharpe | Sortino | total ret | max DD |
|----|--------|--------|-------:|----:|----:|-------:|--------:|----------:|-------:|
| 145 | walk-forward | 2021-07..2026-07 | 477 | 38.8% | 1.195 | 0.327 | 0.132 | +9.23% | −6.24% |
| 143 | walk-forward | 2025-07..2026-07 | 636 | 35.9% | 1.003 | 0.412 | 0.294 | +3.34% | −7.29% |
| 144 | technical | 2025-07..2026-07 | 442 | 31.0% | 0.867 | −0.819 | −0.754 | −12.65% | −21.04% |

### 1.2 Fixed-engine measurement of the deployed system + candidates

OOS window 2022-08-01..2026-07-31, calendar-PIT folds, 10/50bp. 8 metrics.
The **production ensemble is the actual current system** (DEFAULT_WEIGHTS;
news/fundamental reserve weight in the coverage denominator but contribute no
evidence — the faithful production gate, coverage 0.55 on bars where
technical and prediction both fire).

| config | exits | trades | win | PF | Sharpe | Sortino | ret | max DD | expectancy |
|--------|-------|-------:|----:|----:|-------:|--------:|----:|-------:|-----------:|
| **production ensemble** | time-12 | 320 | 30.3% | 0.891 | −0.214 | −0.193 | **−24.44%** | −46.68% | −$73.23 |
| **production ensemble** | bracket | 334 | 32.6% | 0.858 | −0.464 | −0.400 | **−31.05%** | −42.99% | −$96.02 |
| prediction-only (candidate) | time-12 | 337 | 32.1% | 1.113 | 0.327 | 0.316 | **+25.39%** | −25.88% | +$77.13 |
| prediction-only (candidate) | bracket | 392 | 36.5% | 0.987 | 0.034 | 0.032 | −3.91% | −29.95% | −$8.51 |
| always-long control | time-12 | 341 | 32.3% | 0.976 | 0.023 | 0.021 | −6.06% | −24.52% | −$16.42 |
| always-long control | bracket | 360 | 33.1% | 0.867 | −0.406 | −0.356 | −29.09% | −39.73% | −$89.09 |

Findings:
- The deployed system loses money in both exit configs and **underperforms the
  always-long control** — the "improved system" does not exist in the code;
  it was measured as a candidate and not adopted.
- The candidate improvement (prediction-only) is in-window only. Its +25.39%
  time-12 is a long tilt in a rising window (Phase 2: AUC ≈ random), and its
  bracket row still loses.
- Even a passive 3%/6% bracket on the universe loses net of costs (always-long
  bracket −29.09%), i.e. the exit itself destroys value.

## 2. Out-of-sample testing on genuinely unseen data

**Constraint.** The walk-forward OOS window ends 2026-07-31. The price DB ends
2026-08-07, i.e. exactly **5 trading days** (Mon 2026-08-03 .. Fri 08-07) of
genuinely unseen data: never used in training (fold-3 model trains strictly on
bars before 2025-08-01), strategy development, or parameter optimization.

**PnL replay is saturated.** Position sizing is ~33% notional per trade
(1% risk ÷ 3% stop) so equity/exposure caps let only ~3 positions open; every
strategy buys the same first symbols and shows identical +2.59% — the PnL
number is meaningless for comparison.

**Decision-level test** (1,991 unseen bars, forward return close[i]→close[i+1]):

| strategy | BUY | SELL | HOLD | BUY hit rate | BUY mean fwd | base rate |
|----------|----:|-----:|-----:|-------------:|-------------:|----------:|
| always-long | 1991 | 0 | 0 | 56.25% | +0.374% | +0.374% |
| prediction-only | 1990 | 0 | 1 | 56.28% | +0.377% | +0.374% |
| production ensemble | 1029 | 341 | 621 | 56.75% | +0.297% | +0.374% |

On the only unseen data: prediction hit ≈ always-long hit (**zero alpha**);
the production ensemble's buys were more selective (hit 56.75%) but their mean
forward return was *below* the base rate, and its sells also went up (+0.76% —
a rising week). The 1-day horizon also differs from the model's 12-day
training horizon, so this is directional evidence, not a full OOS validation.

**Conclusion:** the system cannot yet be validated on a meaningful holdout —
only 5 days exist. The next validation cycle must **pre-register a fixed
holdout** before any further strategy selection.

## 3. Stress testing

Regime buckets (equal-weight-index trend × volatility) over 2022-08..2026-07
(trading days): BULL-LOW 491, BULL-HIGH 116, BULL-n/a 70, SIDEWAYS-HIGH 45,
SIDEWAYS-LOW 37, BEAR-EXTREME 18, BEAR-HIGH 10, BULL-EXTREME 13,
SIDEWAYS-EXTREME 2, BEAR-LOW 3, n/a-n/a 449.

### 3.1 Deployed system (production ensemble) under stress — cumulative return per regime bucket

| bucket | time-12 | bracket | time-12 trades | bracket win rate |
|--------|--------:|--------:|---------------:|-----------------:|
| BULL-LOW | −2.3% | −6.8% | 146 | 28.8% |
| BULL-HIGH | −3.0% | −7.7% | 47 | 31.9% |
| BULL-n/a | +6.6% | +4.5% | 19 | 47.4% |
| SIDEWAYS-LOW | −4.1% | −6.6% | 10 | 50.0% |
| SIDEWAYS-HIGH | −2.7% | **−14.3%** | 14 | **4.3%** (bracket: 23 trades) |
| SIDEWAYS-EXTREME | −3.9% | −3.8% | 2 | 0% |
| BEAR-HIGH | **−5.6%** | −3.5% | 2 | 50% |
| BEAR-EXTREME | +2.4% | +2.0% | 8 | 50% |
| BULL-EXTREME | +0.8% | +0.8% | 3 | 67% |
| n/a (cold start) | **−8.0%** | +3.5% | 69 | 37% |

The deployed system **loses money even in the calm-bull majority regime**
(BULL-LOW, 491 days) and collapses in choppy/high-vol periods
(SIDEWAYS-HIGH: win rate 4.3%). It only does well in regimes where the
technical signal happened to coincide with drift (BULL-n/a, BEAR-EXTREME) —
small samples. Drawdowns exceed −40% in both exits.

The prediction-only candidate is better in every major bucket (e.g. BULL-LOW
time-12 **+2.7%** vs −2.3%; BEAR-EXTREME +8.4% vs +2.4%) but still loses in
SIDEWAYS-EXTREME (−6.0%) and is not OOS-validated (§2).

### 3.2 Price gaps

498,577 OOS bars scanned: **13,864** single-bar moves >3%, **5,018** >5%,
**1,188** >10%. The 3% bracket stop is routinely gapped through; the backtest
fills stops at the stop price (idealized — no gap-through-stop slippage), so
live losses on gap days would exceed the modeled bracket loss. Live there is
no stop order at all (§5).

### 3.3 News events

The news agent has **no historical data path** (live-only feed, 125 rows), so
news-event stress cannot be backtested; high/extreme-volatility days above are
the only proxy. This is a data-path gap, unchanged from Phase 2.

## 4. Failure testing

**Ran:** existing resilience suite (circuit breaker, retry, token bucket),
Yahoo fault injection, hardening integration (broker outage → order REJECTED +
`OrderStatusChanged`, breaker snapshot), risk/execution/gate/decision tests:
**62 passed, 2 skipped**. New Phase 3 safety tests (`tests/unit/test_phase3_safety.py`):
**12 passed**. Full unit suite: **397 passed**. E2E full-pipeline
(production container, mocked external feeds, real DB/Redis): **1 passed** —
the integrated system executes data→scan→analysis→chief→risk→portfolio→
execution→fill.

**Single-agent failure is safe (new tests):**
- No evidence → HOLD ("no signals available").
- Prediction agent down → coverage (0.30/1.0) < 0.5 → HOLD ("insufficient
  evidence coverage").
- A dead prediction agent only ever makes the system **less** aggressive
  (BUY → HOLD), never more.
- News/fundamental absent (the normal state) still trades when technical +
  prediction fire.

**Provider/API/network:** provider exceptions are caught by the data agent
(refresh returns None / backfill drops the run as failed); broker outages
reject the order and publish an event; circuit breakers degrade the feed.

**Gaps found (documented, not fixed):**
- **Decision-time staleness:** freshness is enforced only at ingestion
  (`BarCleaner` `reject_stale=True`); at decision/risk time a months-old bar
  is accepted as the entry price (new test `test_stale_price_not_checked_at_decision_time`).
- **No DB-outage integration test.** A repository failure in the live path
  propagates (fails closed: no order executes if the DB save fails before
  broker submit; if the broker succeeds but the DB write fails an orphan
  order can exist — mitigated by the idempotency key + unique constraint).

## 5. Risk testing

Verified against `RiskCalculator.assess` (backtest path) and the live
`RiskAgent` wiring:

**Enforced (tested):** position size = 1% risk ÷ ATR-stop; max portfolio
exposure 80%; max 10 positions; per-sector 40%; 5-minute cooldown; 10
trades/day; no re-buy while a position is open (add-to-position off);
duplicate orders prevented (risk no-rebuy + `idempotency_key` + DB unique
constraint); long-only (SELL only closes an existing position); no-price-data
rejects the order.

**NOT enforced in the live/paper path (new tests + code evidence):**
- **Daily-loss limit can never fire.** `RiskAgent` hardcodes
  `daily_pnl_pct=0.0` (`agents/risk.py:114`); the calculator enforces the
  limit only when a real negative value is passed
  (`test_daily_loss_limit_enforced_when_reported` vs
  `test_daily_loss_never_fires_with_live_default_zero`).
- **ADV liquidity check can never fire.** `adv_daily=None` (`agents/risk.py:112`);
  the check is conditional on a non-None value (`risk_calculator.py:126`).
- **Stop losses are never submitted.** The execution path sends exactly one
  MARKET order; `OrderPlan.stop_loss/take_profit` are persisted but no stop
  order is placed and `PaperBroker` does not model stops
  (`test_paper_broker_receives_no_stop_order`,
  `test_execution_submits_single_market_order_despite_brackets`).
- **Unreliable indicator data does not halt trading.** Missing ATR silently
  falls back to 2% of price (`risk_calculator.py:66`) and the order is
  approved (`test_missing_atr_falls_back_to_two_percent_and_trades`).

**Concentration note:** with 1% risk ÷ 3% stop, each position is ~33% notional,
so the portfolio holds ~2–3 concentrated positions — `max_positions=10` is
unreachable and diversification is minimal. Conservative (safe) but
undiversified.

## 6. Paper trading

**Not run.** The work request requires prior tests to pass; they do not
(§1 deployed system loses; §5 risk gaps; §2 no OOS edge).

The graduation gate enforces this mechanically: running the real `SystemGate`
against the persisted performance denies **PAPER** and **LIVE** —
`win rate 38.78% < min 39%`, `profit factor 1.19 < min 1.20`,
`sharpe 0.33 < min 1.00` — audited into `system_logs`.

Paper-readiness requirements (before any paper run):
1. Re-enable daily-loss and ADV limits in the live `RiskAgent` (feed real
   intraday PnL and ADV).
2. Submit stop/take-profit orders to the broker (or implement stop simulation
   in `PaperBroker`) so the backtest exit model matches paper behavior.
3. Add a decision-time data-freshness check.
4. Graduate a *pre-registered* OOS-validated configuration through the gate
   (no current configuration does).

## 7. Final report — system status and disposition

| agent / strategy | status | disposition |
|------------------|--------|-------------|
| prediction (ML) | no skill: AUC 0.508 vs 0.502 shuffled; OOS buy-hit 56.28% ≈ base 56.25% | **remove from weighting / keep at 0** until OOS-validated |
| technical | net drag on fused decision (Phase 2: −39.9pp) | **remove / demote** |
| pattern | no skill (Phase 2: −31.5pp) | **remove** |
| market regime | cold-start, 73.3% coverage, negative | **redesign or remove** |
| news / fundamental / LLM | no data path — unmeasurable | **data-path gap; do not weight** |
| production ensemble (deployed) | −24.44% time-12 / −31.05% bracket | **disable** |
| prediction-only (candidate) | +25.39% time-12 (in-window only) | **do not adopt** (unvalidated) |
| always-long control | −6.06% time-12 / −29.09% bracket | reference only |

**Performance summary (fixed engine, 10/50bp):** original persisted baseline
id 145 +9.23% (old engine, not comparable); deployed system −24.44% time-12;
best in-window candidate +25.39% time-12 (unvalidated OOS); genuinely-unseen
holdout: zero alpha; paper trading: not run (gate denied).

**Risks carried forward:** (1) system loses money as configured; (2) no
meaningful OOS evidence of edge; (3) bracket exit destroys value net of costs
in every configuration; (4) live risk wiring does not enforce daily-loss/ADV
and never submits stops; (5) gaps can slip the 3% stop; (6) news stress
untestable; (7) DB-outage behavior untested; (8) 2–3 position concentration.

**Verdict: NOT READY FOR PAPER TRADING.** No live money, ever, until: the
deployed ensemble is replaced by an OOS-validated configuration, the live risk
gaps are closed with passing tests, and the system graduates the `SystemGate`
on a pre-registered holdout.

## References

- Tests: `tests/unit/test_phase3_safety.py` (12), `tests/unit/test_system_gate.py`,
  `tests/integration/test_hardening.py`, `tests/unit/test_resilience.py`,
  `tests/unit/test_yahoo_resilience.py`, `tests/e2e/test_full_pipeline.py`.
- Evidence (temp workspace `C:\Users\User\AppData\Local\Temp\opencode\`):
  `p3_measure.py` / `p3_integrated.json` (production-ensemble sims, stress,
  risk code facts), `p3_oos_decisions.py` / `p3_oos_decisions.json` (unseen
  holdout), `p3_gate_check.py` (gate denial), `p3_curves.pkl` (curves/trades).
- Prior audits: `docs/audit/15-phase1-strategy-audit.md`,
  `docs/audit/16-phase2-agent-ml-audit.md`, `docs/audit/07-baseline-freeze-v2.md`.
