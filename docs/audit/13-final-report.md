# Final Audit Report — qtrader Multi-Agent Trading System

Date: 2026-08-08. Baseline freeze: `a459406` (commit `a459406`, 340 tests green,
ruff + mypy clean). Evidence: `docs/audit/07..12` + the reproduction scripts
(`phase5_ablation.py`, `phase7_analytics.py`, `phase10_folds.py`).

---

## Executive verdict

**NOT APPROVED.** The system's headline performance is an artifact of
experimental configuration, not a measured edge. The single most decisive
result of this audit:

> Removing the ML signal entirely and simply staying long with the 3%/6%
> bracket produced **PF 1.685, +38.87%**, while every model-driven variant
> produced less (best +23.4%, tuned baseline **−12.5%**). A model trained on
> **randomly shuffled labels** performs like the real model. The "edge" is the
> bracket math plus the bull market, at sub-1bp costs that real trading does
> not have.

Three disconnected "strategies" exist (live agent ensemble / backtested
logistic model / scanner rankings), none of which has ever been validated on
its own OOS data with realistic costs. The system has never executed a trade.

---

## Verdict table

| Question | Verdict |
|---|---|
| Win rate 40.1% computed correctly? | Formula yes; number is from a non-persisted, gate-DENIED config |
| Agents work / add value? | Execute, but single-day outputs; no measurable value |
| Real strategy or overfitting? | **Overfitting / configuration lottery** |
| Data leakage? | No gross train/test leak; selection-on-noise present |
| Survivorship bias? | **Yes, confirmed** (0 delisted names) |
| Look-ahead bias? | **Yes, by construction** (fold misalignment, unadjusted prices, future-listed names) |
| Overfitting in params/features? | **Yes** (threshold lottery; shuffled ≈ real) |
| OOS performance real? | **No** (see P10 misalignment; P5 controls) |
| Per-strategy performance? | Table below |
| Per-agent performance? | Unmeasurable (1 day, no forward bars) |
| Regime adaptation? | **None** |
| Cost accounting correct? | Engine yes; assumptions unrealistic (1–6bp) |
| Fix recommendations? | Below |
| Baseline-after comparison? | Protocol specified; not run (fixes not implemented) |
| Can it trade with confidence? | **No** |

---

## Answers to the 17 questions

1. **How was 40.1% computed? Is it correct?**
   The arithmetic is correct (wins ÷ closed trades). But the 40.1% figure
   comes from `wf_v2_rev_nocalib.log` — the *uncalibrated, reversal-features*
   experiment (494 trades, PF 1.223, ret −5.9%) that the system's own gate
   **DENIED** (sharpe −0.26 < 1.00, ret < 0) and that was never persisted. The
   persisted result is 38.78% (id145, 477 trades, PF 1.195, +9.23%, from a
   different 11-feature config tuned 0.6/0.4). Different config → different
   headline. Neither is reproducible from the artifacts (re-running the
   documented protocol yields 35–128 trades and −12.5%..+22.2% depending on
   fold-1 selection details).

2. **Do the agents work? Does each add value?**
   They *execute*: signals exist for technical/news/fundamental, predictions
   for the heuristic fallback, decisions for 5 mega-caps + fixtures — but **all
   on a single day (2026-08-06)**. Zero forward-return observations exist, so
   per-agent accuracy is *unmeasurable*. Value: the live ensemble has never
   been backtested; the news agent is inert without an LLM key; fundamental has
   data on 7/502 symbols; the prediction agent is a `momentum` v0 heuristic (the
   "trained" registry model has no artifact and is never used). Risk/execution/
   portfolio produced **no records at all** (0 trades).

3. **Real strategy or overfitting?**
   **Overfitting.** Phase 5 controls: (a) a no-signal always-long rule beats
   every model variant; (b) threshold tuning on fold 1 is a lottery — OOS
   ranges +23.4% (0.52) to −12.5% (0.70) and the "tuned" pick was the worst
   OOS; (c) a shuffled-label model ≈ the real model. There is no measurable
   signal; the appearance of edge is selection-on-noise over a bull window.

4. **Data leakage?**
   No gross leak in the walk-forward loop itself (labels only use past bars;
   features end at the decision bar; fills at next open). The leakage-adjacent
   problems are **selection-on-noise** (threshold tuned on the validation fold)
   and the model-calibration instability — the "edge" survives only because the
   same noise is re-selected. (Live path: no backtest exists at all.)

5. **Survivorship bias?**
   **Confirmed.** All 502 symbols have bars through 2026-07; **zero delisted
   names**; the list is a *current* S&P membership pull with no historical
   point-in-time table. Historical tests only ever see today's survivors. This
   inflates any long-bias result. (Built `point_in_time_universe()`; the real
   fix needs an external constituents/delisted-source history.)

6. **Look-ahead bias?**
   **Present, by construction.** (a) Prices are **unadjusted**: split days appear
   as ±50% moves and corrupt features/labels (CVNA +56%, GL −53%, HOOD +50%,
   ECHO +70% on split days). (b) Fold index windows map to **different calendar
   periods per symbol** (P10): full-history names are OOS-tested only in
   2022-04..2023-11 while GEV/SOLV/VLTO are tested in 2024-2026 — a backtest
   "trades" 2024 listings in 2022. (c) The 2024–26 bull market is never OOS-tested
   for the full-history universe.

7. **Overfitting in features/parameters?**
   **Yes.** Feature correlations are all |r| ≤ 0.11 (P7). Model in-sample
   accuracy 55–57% vs ~50% base rate; Platt `calib_a` swings −0.08..+0.72 per
   fold. Threshold selection is the dominant "parameter" and it is pure noise
   selection. Reversal-feature variants, calibration on/off, sectors on/off
   were each tuned per experiment, producing different headlines — textbook
   researcher-degrees-of-freedom inflation.

8. **Is OOS performance real?**
   **No.** The "2021-07..2026-07 walk-forward" never OOS-tests the full-history
   names past 2023-11 (P10). Even the *compressed* OOS was a rising market
   (+14.3% equal-weight). And within any fixed window, the model adds no value
   over always-long (Q3). The persisted +9.23% is a label, not a measured OOS
   edge.

9. **Per-strategy performance** (test folds 2–4, same protocol):

   | strategy | trades | win% | PF | ret% |
   |---|---|---|---|---|
   | Persisted walk-forward (id145, label) | 477 | 38.78 | 1.195 | +9.23 |
   | Reversal uncalibrated (40.1% config, DENIED) | 494 | 40.08 | 1.223 | −5.9 |
   | Technical only (backtest_runs id274, 1+5bp) | 442 | — | — | −12.65 |
   | Reproduced logistic, tuned threshold | 35 | 22.86 | 0.504 | −12.48 |
   | Always-long bracket control | 98 | 45.92 | **1.685** | **+38.87** |
   | Shuffled-label model (control) | 128 | 33.59 | 0.943 | −4.76 |

10. **Per-agent performance.** Not measurable: agents' outputs are a single-day
    snapshot with no forward bars, and the agents never drove a backtest.
    Only structural facts: news inert (no LLM key), fundamental 7/502,
    prediction = heuristic fallback, risk/execution/portfolio produced 0 records.

11. **Regime adaptation.** **None.** No regime detection exists anywhere in the
    pipeline (no regime agent, no regime field). The strategy is long-biased in
    a market where 2022 was −10.9% and 2023–26 +14..+24%/yr; it would have been
    exposed to the 2022 drawdown with no filter, and the "profit" comes from
    the same bullish beta that a passive hold captured 4–9× better.

12. **Financial accounting.** Engine mechanics (commission/slippage on notional,
    next-open fills, 1% risk sizing, R:R bracket) are correct. The **assumptions
    are not**: 1–6bp is retail-unrealistic. At 10/50bp the strategy goes to
    PF 0.80 / −55%; at 25/100bp to PF 0.50 / −89%. Results are also
    path-chaotic under small execution changes (1/5bp gave *more* than 0bp).
    Position sizing (1% risk) and stops are sound in isolation but rest on a
    strategy with no measured edge.

13. **Fix recommendations (priority order).**
    1. **Data**: adjusted (split/dividend) prices; a real point-in-time
       constituents + delisted universe; BarValidator on all backfills.
    2. **Validation**: calendar-aligned, per-symbol OOS windows (P10) with the
       PIT universe filter (`universe.py`) applied per window.
    3. **Signal**: remove the ML entry (or prove OOS edge vs the no-signal and
       shuffled controls at ≥10bp costs). Treat "momentum v0 heuristic" as the
       only honest baseline until then.
    4. **Exits**: drop the 6% target cap or add time exits (no-target/time-12
       gave PF 1.717); decide deliberately, not via fold-1 tuning.
    5. **Costs**: re-evaluate at ≥10bp round-trip; gate on net-of-cost metrics
       only.
    6. **Records**: unified agent record (`input_data_version`, `latency`,
       per-agent reason, `features_hash`, real `model_version`); persist every
       backtest run as a `backtest_runs` row; a per-decision forward-return
       ledger so agent accuracy becomes measurable.
    7. **Gate**: require regime-aware, net-of-cost, control-adjusted evidence.

14. **Baseline-after comparison.** Baseline frozen at `a459406`. Re-test
    protocol (P19): after fixes, re-run the identical 17-phase audit and report
    A/B (baseline vs fixed) on: OOS PF/win/Sharpe, net-of-cost at 10bp, OOS
    calendar coverage, per-agent accuracy once a forward-return ledger exists.
    Not executed here (fixes are a separate workstream).

15. **Can it run with confidence?** **No.** It has never traded, its "edge" is
    not distinguishable from noise or beta, its costs are unrealistic, and its
    validation windows are misaligned. Flipping to live would be
    irresponsible; the gate correctly DENIED every attempt so far.

16. **Position sizing & risk.** Sizing is 1% risk per trade with a 3% stop and
    6% target (correct R:R discipline, gate min-win 39.3% is the R:R-aware
    breakeven). But with no measured signal, sizing discipline only controls
    losses on a losing strategy; daily/sector exposure caps (10 positions,
    sector caps) are reasonable yet never stress-tested with realistic costs
    or a bear regime.

17. **Executive summary / bottom line.** The system is **well-engineered, badly
    validated**. Infrastructure (clean architecture, tested agents, event bus,
    resilience, parameterized engine, R:R-aware gate) is solid — but the
    "38.78% win / PF 1.195" result is bracket-beta at fantasy costs, the agents
    have never been measured, the data has survivorship/look-ahead corruption,
    and the headline number is not reproducible. The path forward is the fix
    list in Q13, with the honest controls in this report as the acceptance
    bar: **beat always-long, beat shuffled labels, at ≥10bp costs, on
    calendar-correct OOS windows, with a persisted forward-return ledger.**
