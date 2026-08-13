# Phase 3 — Automated Strategy Validation & Edge Detection

Date: 2026-08-13. Work request: build the automated strategy validation and
edge-detection pipeline on top of the Phase 2 strategy research engine
(`22-phase2-strategy-research.md`, baseline `cca75c8`): strict
Dev/Validation/OOS separation, initial filtering on the development window only,
per-fold walk-forward validation, overfitting and multiple-testing statistics,
regime and robustness studies, a benchmark/value gate, and a VALIDATED research
status. **Research only — no live trading; a validated strategy is a research
verdict, never a profit claim.** Baseline: `cca75c8`.

## Executive verdict

**COMPLETE AND VERIFIED.** `src/qtrader/application/research/validation/` now
implements the full Phase 3 stage-gated pipeline:

```
Generator -> register GENERATED -> dev-window backtest (net of 10/50 bps)
-> InitialCandidateFilter -> dev MetricGate
-> robustness (parameter / timeframe / regime / cross-asset / cost)
-> walk-forward over dev+validation -> wf MetricGate
-> validation confirmation -> dev benchmark study -> untouched OOS backtest
-> OOS MetricGate -> OOS benchmark/value gate -> VALIDATED
```

Every stage's outcome is stored per strategy in a JSON-round-trippable research
database (`ValidationRepository`), and only strategies that clear every gate are
`VALIDATED`. One real bug was found and fixed by the new tests (metrics with
missing values serialized as the string `"None"` broke repository round-trips).
Full suite **631 passed, 27 deselected** (+42 new tests); `ruff check src tests`
clean; `mypy src` clean (151 files). **No new dependencies.** Nothing trades live.
A pre-existing time-of-day flaky safety test
(`test_agent_enforces_daily_loss_on_todays_realized_loss`) was anchored to
explicit calendar dates — near UTC midnight its `now - 6 min` SELL fill landed
on the previous day and the daily-loss window missed it.

## 1. Strict window discipline

`src/qtrader/application/research/validation/splits.py`:

- `DataWindow` — half-open-on-calendar `[start, end]` interval with a `label`.
- `split_windows(start, end, dev_fraction, validation_fraction)` — three
  contiguous, disjoint calendar windows: `dev` (first fraction), `validation`
  (next fraction), and everything remaining as the **untouched** OOS window
  (`dev + validation < 1` is enforced; OOS is never used for tuning).
- `slice_bars` / `slice_bars_by_symbol` — slice bar histories down to a window.

## 2. Initial candidate filtering (dev window only)

`src/qtrader/application/research/validation/filters.py`:

- `InitialFilterLimits` — `min_trades` (30), `max_cagr` (2.0), `max_total_return`
  (20.0), `max_drawdown` (−0.5), `max_turnover` (20.0), `max_complexity` (8),
  `min_distinct_indicators` (2), `max_instability` (0.5), `jitter_runs` (4).
- `InitialCandidateFilter.check(spec, dev_summary, jittered)` returns an
  `InitialFilterReport` with per-check verdicts and human-readable reasons:
  min trades, extreme performance, drawdown, turnover, complexity, narrow
  entry-vs-exit parameter ranges, single-indicator dependence, and **parameter
  instability** (population stdev of Sharpe across ±jittered variants,
  step `max(1.0, 0.25·|threshold|)`).
- Cheap rejection **before** any expensive validation; OOS data is never touched.

## 3. Robustness studies (dev window)

`src/qtrader/application/research/validation/robustness.py`:

- `parameter_variants(spec, max_variants, span)` — nearby-parameter copies.
- `ParameterRobustnessChecker` — base + jittered Sharpe distribution:
  `max_instability` (pstdev) and `min_positive_fraction` (0.5).
- `multi_timeframe_report` — consistency across timeframe combinations
  (`best_interval`, `positive_fraction`, `consistency_sharpe_std`).
- `regime_report_from_buckets` — per market/volatility regime slices
  (trades, win rate, total return, Sharpe), best/worst regime.
- `cross_asset_report` — per-symbol and per-sector outcomes, flagging
  **single-symbol / single-sector dependence**.
- `cost_sensitivity_report` — edge retention across execution levels,
  `edge_retained_at_realistic` at 10/50 bps, break-even level.

## 4. Statistical edge and multiple-testing correction

`src/qtrader/application/research/validation/edge.py`:

- `compute_edge_stats` — the full risk/return picture (expectancy, Sharpe,
  Sortino, max drawdown, profit factor, win rate, avg win/loss, turnover,
  total costs, trade-return mean/std/skew/kurtosis, walk-forward stability).
  **Win rate is never used alone.**
- `expected_max_sharpe` / `deflated_sharpe` — Bailey/Lopez de Prado correction:
  subtract the expected best-of-N null Sharpe from the observed Sharpe and
  re-derive `prob_real`.
- `multiple_testing_report` — risk bands from `prob_real`
  (`low` ≥ 0.95, `medium` ≥ 0.80, else `high`); `hypotheses_tested` is the
  number of generated strategies, so surviving strategies pay for the search
  they were selected from.

## 5. Benchmark / value gate

`src/qtrader/application/research/validation/benchmarks.py` — does the added
complexity actually beat naive baselines on the same window and costs?

- `buy_and_hold_curve` — equal-weight mark-to-market basket, net of entry/exit
  frictions.
- `sma200_filter_spec` — buy above / exit below the 200-day SMA, replayed
  through the production engine as a rule strategy.
- Momentum — the engine's default ema9/21 crossover (`momentum=True` path).
- `random_permutation_result` — permutation control over the strategy's **own**
  trade population (ordering edge vs. exposure).
- `build_benchmark_report` → `value_added = beats_buy_and_hold and beats_sma200
  and beats_random_mean` (index is informational in the equal-weight universe).

## 6. Research database

`src/qtrader/application/research/validation/records.py` + `repository.py`:

- `ValidationRecord` — one complete, reproducible history per hypothesis: spec,
  stage, final status, `hypotheses_tested_before` (its rank in the search),
  universe, dataset version, windows, dev/validation/walk-forward/OOS results,
  robustness reports, benchmark report, multiple-testing correction, edge,
  robustness flags, notes, created_at.
- `ValidationStage` lifecycle: `GENERATED`, `REJECTED_INITIAL_FILTER`,
  `REJECTED_DEVELOPMENT`, `REJECTED_ROBUSTNESS`, `REJECTED_WALK_FORWARD`,
  `REJECTED_VALIDATION`, `RESEARCH_FURTHER`, `REJECTED_OOS`, `VALIDATED`, `FAILED`.
- `FinalStatus` verdicts: `REJECTED`, `RESEARCH_FURTHER` (cleared the dev
  pipeline, awaiting OOS / did not clear the OOS gate or benchmarks),
  `VALIDATED`.
- `encode_record`/`decode_record` + `InMemoryValidationRepository`
  `export`/`import_` — the entire database round-trips through JSON, so any
  experiment can be replayed from the stored record alone.
- **Bug fixed by the new tests:** `_encode_summary` serialized missing metrics
  as the string `"None"`, which `_decode_summary` could not parse; it now emits
  `null` for missing values.

## 7. Validation engine

`src/qtrader/application/research/validation/engine.py`:

- `ValidationPlan` — search limits, initial filter limits, three `MetricGate`s
  (dev / walk-forward / OOS), split fractions (0.5 / 0.25), capital, 10/50 bps,
  warmup, folds (4), lookback/horizon bars, intervals, cost levels,
  `param_robustness_runs`, `random_benchmark_seeds`, `benchmark_gate`,
  `max_ranked`.
- `StrategyValidationEngine.run(request)` executes the full pipeline per
  generated strategy and stores every stage result; the final OOS verdict only
  requires the untouched OOS gate **and**, when the benchmark gate is on, the
  OOS benchmark `value_added`.
- `ValidationWalkForwardValidator(StrategyWalkForwardValidator)` records every
  held-out fold (`FoldResult`) alongside the chained aggregate, so stability is
  judged per-period, not just on the aggregate.
- `ValidationReport` carries the required counts: `total_generated`,
  `rejected_by_generator`, `rejected_initial_filter`, `rejected_development`,
  `rejected_robustness`, `rejected_walk_forward`, `rejected_validation`,
  `reached_oos`, `rejected_oos`, `research_further`, `validated`, `failed`,
  `best_validated` (from the ranker), `rejected_reasons`.

## 8. Ranking

`src/qtrader/application/research/validation/ranking.py`:

- `StrategyRanker` ranks **only VALIDATED** strategies by a weighted percentile
  blend across expectancy, Sharpe, Sortino, drawdown, profit factor, OOS Sharpe,
  walk-forward stability, trade count, complexity (lower is better) and
  multiple-testing risk. `RankingWeights` must sum to 1.0.

## 9. Integration, settings and DI

- `settings.py::StrategyValidationSettingsMixin` (mounted on `Settings`):
  `strategy_validation_*` knobs and a `strategy_validation_plan` property
  building the `ValidationPlan` from `SearchLimits` + `InitialFilterLimits` +
  the dev/wf/OOS `MetricGate`s (enum/name interval parsing).
- `container.py`: registers the `InMemoryValidationRepository`,
  `StrategyValidationEngine` (wired from settings), and the
  `StrategyValidationAdapter` after the strategy-research block, reusing the
  `RiskCalculator`, `IndicatorEngine` and `strategy_registry`.
- `adapters.py` / `research/__init__.py`: `StrategyValidationAdapter` exported.

## 10. Files added / modified

- `src/qtrader/application/research/validation/` (new package): `__init__.py`,
  `splits.py`, `filters.py`, `records.py`, `robustness.py`, `benchmarks.py`,
  `edge.py`, `repository.py`, `ranking.py`, `engine.py`.
- `src/qtrader/application/research/adapters.py`, `research/__init__.py` —
  `StrategyValidationAdapter`.
- `src/qtrader/config/settings.py` — `StrategyValidationSettingsMixin`.
- `src/qtrader/config/container.py` — repository/engine/adapter wiring.
- `tests/unit/test_strategy_validation.py` (new) — 42 tests.

## 11. Tests performed

- **New** `tests/unit/test_strategy_validation.py` (42 tests): splits
  (contiguity/disjointness, slicing), initial filter (pass, min trades,
  single-indicator, parameter-instability rejection), edge (expected max
  Sharpe, deflated Sharpe, multiple-testing risk bands, trade-distribution
  stats), benchmarks (SMA200 spec, buy & hold curve, random permutation,
  `value_added` pass/fail), robustness (variants cap/empty, parameter checker,
  multi-timeframe, regime buckets, cross-asset single-symbol flag, cost
  sensitivity), repository (CRUD, duplicate/unknown errors, JSON round-trip,
  export/import), ranking (weights, ordering, empty), walk-forward
  (per-fold results recorded), engine (full pipeline → VALIDATED, strict dev
  gate rejects all, no price history → FAILED, report-count consistency,
  benchmark gate at OOS, bad plan fractions), settings → plan defaults.
- **Full suite**: **631 passed, 27 skipped** (baseline 589 → +42).
- **Lint/type**: `ruff check src tests` clean; `mypy src` clean (151 files).

## References

- Phase 3 package: `src/qtrader/application/research/validation/*`.
- Workflow: `src/qtrader/application/research/validation/engine.py`.
- Settings/DI: `src/qtrader/config/settings.py`, `src/qtrader/config/container.py`.
- Tests: `tests/unit/test_strategy_validation.py`.
- Prior audits: `docs/audit/22-phase2-strategy-research.md`,
  `docs/audit/21-phase1-research-infrastructure.md`,
  `docs/audit/20-phase3-multitimeframe.md`.
