# Phase 3 — Multi-Timeframe Research Engine

Date: 2026-08-12. Work request: build a research engine that determines which
timeframes and timeframe combinations are most useful for trading decisions,
using per-timeframe analysis, context→setup→entry combination studies, market
regimes, walk-forward/OOS robustness, transaction-cost sensitivity, and a
ranked recommendation report. This phase is **explicitly research-only** — no
trading strategies are built or traded. Baseline: `bb130fc`.

## Executive verdict

**READY FOR USE (research output).** The engine loads bars for the seven
evaluable timeframes (M1/M5/M15/M30/H1/H4/D1), validates coverage, resamples
derived intervals (H4 from H1), runs single-timeframe studies, enumerates all
valid (context, setup, entry) combinations, simulates long/flat trading with a
commission+slippage cost gate (10/50 bps), splits results by market regime,
measures walk-forward OOS robustness and parameter sensitivity, and ranks
combinations on OOS-first evidence — never on raw historical return. It ships
with settings, container wiring, a scheduled `research_cycle` task, a CLI
runner, and a 43-test unit suite. Full suite **533 passed, 27 deselected**;
`ruff check src tests` clean; `mypy src` clean.

## 1. Design

**Intervals** (`src/qtrader/domain/value_objects/__init__.py`): `Interval` now
has M1/M5/M15/M30/H1/H4/D1; `INTERVAL_MINUTES`, `interval_minutes()`,
`DERIVED_INTERVALS={H4}`, `DERIVED_INTERVAL_SOURCE={H4: H1}`, and
`derived_source()`. Only timeframes the data source can reliably provide are
evaluated — no assumption that M5 (or any timeframe) is optimal.

**Engine** (`src/qtrader/application/services/multitimeframe.py`, ~1660 lines)
is split into pure functions (all analysis) and one I/O class
(`MultitimeframeResearchEngine`) that only touches price/stock/universe
repositories. Ports stay domain ABCs; no new persistence.

## 2. Data quality & resampling

- `timeframe_quality`: coverage on the interval's calendar grid
  (`_on_grid`), gap statistics, and `min_coverage_pct` (default 0.9) gating.
  A timeframe below the coverage floor is excluded from studies.
- `resample_bars`: aggregates OHLCV into a target `Interval`, rejecting
  finer-or-equal targets; H4 is resampled from H1 (via `derived_source`).
  The Yahoo provider guards derived intervals so no unsupported period is
  ever requested (`src/qtrader/infrastructure/data_providers/yahoo.py`).
- `performance_metrics.py` gained annualization keys for all seven intervals;
  `bar_cleaner.py` grids M30/H4 for the intraday backfill path.

## 3. Signals, combinations, simulation

- `signal_series` computes a trend (EMA fast/slow) or reversion (RSI) signal
  per bar; `SignalParams` default trend 9/21.
- `enumerate_combos` builds all (context, setup, entry) triples with strictly
  descending timeframes (context slowest, entry fastest).
- `align_latest` carries slower-timeframe signals forward onto entry bars with
  no look-ahead; `combine_signals` merges them with `all` (slower roles filter
  the entry signal), `majority` (≥2 of 3 agree), or `entry` modes.
- `simulate` is long/flat with next-bar-open fills, equal-weight full sizing,
  and a cost model (`SimParams`: `commission_bps=10`, `slippage_bps=50`,
  optional `max_hold_bars` time stop). Open positions are liquidated at the
  final bar close (extra equity mark). Trades are `ResearchTrade` records with
  net PnL and cost impact.

## 4. Regimes

`regime_labels_for` classifies each trading day using the existing
`MarketRegimeEngine` (trend × volatility axes, causal) and `_assign_regimes`
slices each combination's trade list by regime. `StudyMetrics.regime`
(per-regime `RegimeSlice`: trades, total return %, avg hold) feeds the
`regime_consistency` term in ranking — a combination only gets credit if its
edge holds in the majority of regime slices.

## 5. Robustness

- `walk_forward`: chronological n-fold (default 4) split on entry bars; within
  each fold the best signal params are chosen on train, evaluated only on the
  disjoint OOS slice. `WalkForwardSummary` reports OOS Sharpe/return means and
  positive ratios — the honest, selection-bias-free estimate.
- `parameter_sensitivity`: full-history Sharpe spread across the 6-param grid
  (`sharpe_mean/std`, `sharpe_positive_ratio`, best params).
- `cost_sweep`: performance at 0, 5, 10, 25, 50, 100 bps round-trip for each
  timeframe and combination.

## 6. Recommendation

`rank_recommendations` scores each combination as `0.30·tanh(oos_sharpe) +
0.20·OOS-positive + 0.20·param-stability + 0.15·(1−max_drawdown) +
0.15·regime-consistency`; robustness is HIGH/MEDIUM/LOW from OOS Sharpe,
stability, and score. **A combo never ranks on raw historical return.**
`best_roles` extracts the score-weighted best context/setup/entry timeframes
over the top-10 combos. `ResearchReport` bundles timeframes studies,
combinations, recommendations, best roles, and data limitations.

## 7. Settings, wiring, CLI

- `ResearchSettingsMixin` (`src/qtrader/config/settings.py`): lookback 730d,
  min train bars 100, min coverage 0.9, combination mode `all`, max symbols
  20, 4 folds, signal mode/fast/slow/band, 10/50 bps, max hold bars 0,
  `research_intervals` (empty = all evaluable; accepts enum name `D1` or
  value `1d`). `research_settings` property builds the dataclass.
- Container registers `MultitimeframeResearchEngine` from
  `settings.research_settings`.
- `research_cycle` task (`src/qtrader/infrastructure/schedulers/tasks.py`,
  cron `{3}:15`, not market-gated) runs the report and records a `research`
  agent metric.
- `scripts/multitimeframe_research.py`: CLI (`--symbols --days --json`) prints
  per-timeframe studies, cost sweep, combination and recommendation tables.

## 8. Tests & verification

- `tests/unit/test_multitimeframe.py` (43 tests) with `FakeMultiPriceRepo` /
  `FakeUniverseRepository`: quality gates, resampling (incl. H4 rejection of
  D1 target and finer targets), signals, alignment no-look-ahead, combination
  modes, simulator (fills, costs, forced liquidation, flips), metrics,
  regimes, cost sweep, aggregation, single-timeframe study, walk-forward
  (OOS-only honesty, short-data skip), parameter sensitivity, ranking,
  `best_roles`, and engine integration (derived H4, universe resolution,
  no-symbols path, `max_symbols` cap).
- `tests/unit/test_settings.py` covers the research settings mixin (env
  override + interval token parsing).
- Full suite: **533 passed, 27 deselected**; `ruff check src tests` clean;
  `mypy src` clean on all touched files.

## 9. Limitations carried forward

1. **No stop-loss/take-profit in the research simulator.** The frozen cost
   gate (10/50 bps) is implemented, but the 3%/6% stop protocol is not yet a
   `SimParams` field; only the optional `max_hold_bars` time stop exists.
2. Regime slices use the trend×volatility classifier; regime attribution is
   day-granular and does not account for intraday regime shifts.
3. Walk-forward folds are split on entry-bar index (chronological) without an
   explicit purge/embargo gap between train and test.
4. Representative single-timeframe studies run on the symbol with the most
   bars; coverage varies by symbol/interval and `min_coverage_pct` may exclude
   thinly-covered intervals from the report.
5. Research results are descriptive: they identify useful timeframes/combos
   but are not a tradable strategy, and were not validated against live fills.

## References

- Engine: `src/qtrader/application/services/multitimeframe.py`.
- Intervals: `src/qtrader/domain/value_objects/__init__.py`; derived-interval
  guard in `src/qtrader/infrastructure/data_providers/yahoo.py`.
- Settings/container/task: `src/qtrader/config/settings.py`,
  `src/qtrader/config/container.py`,
  `src/qtrader/infrastructure/schedulers/tasks.py`.
- CLI: `scripts/multitimeframe_research.py`.
- Tests: `tests/unit/test_multitimeframe.py`, `tests/unit/test_settings.py`.
- Prior audits: `docs/audit/19-phase2-universe.md` (universe engine consumed
  here), `docs/audit/12-phases6to20.md` (frozen cost/protocol notes).
