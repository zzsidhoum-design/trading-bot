# Phase 2 — Automated Strategy Research Engine

Date: 2026-08-12. Work request: build the automated strategy research engine
on top of the Phase 1 research infrastructure (`21-phase1-research-infrastructure.md`)
and the Phase 3 multi-timeframe research (`20-phase3-multitimeframe.md`):
declarative strategy specifications, a validated feature library, a bounded
generator, net-of-cost backtest integration, a strategy registry, anti-data-mining
guards, and the workflow pipeline that ends in the registry. **Research only —
no live trading; no profitability claim is made from any backtest.** Baseline:
`a3d8119`.

## Executive verdict

**COMPLETE AND VERIFIED.** `src/qtrader/application/research/strategy/` now
implements the full Phase 2 workflow: specs → generator → feature library →
initial backtest (net of 10/50 bps costs through the production `BacktestRunner`)
→ metric gate → robustness (complexity / min-trades / extreme performance /
narrow params / parameter instability) → walk-forward OOS → OOS gate → registry
`VALIDATED`. A minimal refactor of `WalkForwardValidator` added a `strategy_label`
so research walk-forwards aggregate under the strategy ID, not the baseline label.
Full suite **589 passed, 27 deselected** (+31 new tests); `ruff check src tests`
clean; `mypy src` clean (141 files). **No new dependencies.** Nothing trades live.

## 1. Strategy specification layer

`src/qtrader/application/research/strategy/specs.py` — declarative, validated
specifications that the rest of the workflow consumes:

- `StrategySpec` (frozen dataclass): `id`, `name`, `version`, `direction`
  (`long`/`short`/`long_short`, validated), `entry`/`exit` rules, optional
  `regime` filter, `timeframes`, `params`, `features`, `complexity` score,
  `description`.
- `Condition` — one rule: `feature`, `op`, and either a numeric `value` or a
  `ref_feature` (validated: `ref_feature != feature`). Operators: `>`, `<`,
  `>=`, `<=`, `cross_above`, `cross_below` (cross ops implemented via lagged
  comparison in the evaluator).
- `EntryRule` (`logic="all"`), `ExitRule` (`logic="any"`), `RegimeFilter`.
- `encode_spec` / `decode_spec` — JSON-safe round-trip used by the registry's
  `export`/`import_`.

## 2. Feature library + centralized factory

`src/qtrader/application/research/strategy/feature_library.py`:

- **28 validated `Feature` entries** across five categories — TREND, MOMENTUM,
  VOLUME, VOLATILITY, EXIT — each tagged with the category and the indicator
  columns it needs.
- `SNAPSHOT_FEATURES` (16 production `IndicatorSnapshot` columns) +
  `PRICE_FEATURES` (11 price-derived features computed in the evaluator:
  `ret_1/5/10/20/60`, `vol_20`, `atr_pct`, `volume_ratio`, `range_ratio`,
  `pos_in_range_20`, `up_ratio_20`).
- `CATEGORY_TEMPLATES` — per-category, pre-validated building blocks (e.g. trend
  `close > ema_21`, momentum `rsi > 50`, exit `rsi > 70`) so the generator only
  assembles meaningful primitives — no indicator soup.
- `FeatureLibrary` accessor (`all`/`names`/`categories`/`by_category`/`has`/`get`).
- No TA-Lib: the existing pandas `IndicatorEngine` covers the full required set
  (avoids the fragile C build; consistent with the Phase 1 dependency audit).

## 3. Constrained generator

`src/qtrader/application/research/strategy/generator.py`:

- `SearchLimits` — the search-space contract: `max_strategies` (60), `max_indicators`
  (5), `max_conditions` (3 entry), `max_exit_conditions` (2), `max_complexity` (8),
  `computational_budget` (60), `intervals`, `regime_gate`, `allow_momentum_entries`.
- `ENTRY_BASES` = curated trend + momentum entries; `FILTER_COMBOS` (volume /
  volatility filters); `EXIT_RULES`; `REGIME_GATE` (`close > sma_200`, applied to
  every other candidate).
- `StrategyGenerator.generate(limits)` returns `GenerationResult(specs, rejections,
  candidates_considered)`. Every candidate is checked by `_constraint_violation`:
  indicator count (incl. `ref_feature`s), entry/exit condition caps, complexity
  score, redundant conditions, and **narrow parameter ranges** (entry vs exit
  thresholds on the same feature within `NARROW_PARAM_EPSILON = 2.0`).
- A generated strategy is a *hypothesis only*; it is never assumed profitable and
  only reaches `VALIDATED` after every stage.

## 4. Rule-driven evaluation (research backtests are net-of-cost)

`src/qtrader/application/research/strategy/evaluator.py`:

- `StrategyEvaluator(warmup_bars=30)` builds a per-symbol feature frame
  (production snapshot columns + price-derived features) and evaluates the spec's
  entry/exit/regime conditions vectorized per bar.
- Output follows the backtest engine's `model_outputs` contract
  (`strategies/base.py`): `EVENT_BUY = 0.9` when entry fires, `EVENT_SELL = 0.1`
  when exit fires, `HOLD = 0.5` otherwise; warm-up bars always HOLD so indicators
  have converged; missing indicator series ⇒ HOLD.

`engine.py::_backtest` reuses the **production `BacktestRunner._simulate(..., model_outputs=probs, series=series)`**,
so research backtests get the same execution semantics as production: fills,
commission + slippage (**10/50 bps**, from `ResearchPlan`), ATR position sizing,
bracket/time exits, portfolio constraints. Gross returns are never ranked.

## 5. Metrics and backtest accounting

Aggregation is delegated to the existing `PerformanceMetrics`/`PerformanceSummary`
(no duplicate metric code): total return, CAGR, Sharpe, Sortino, max drawdown,
profit factor, expectancy, win rate, avg win, avg loss, trades count, turnover,
total costs. Research runs persist only validated OOS summaries (ephemeral
`_NoopBacktestRepository`); nothing else leaks into the production backtest store.

## 6. Strategy registry

`src/qtrader/application/research/strategy/registry.py`:

- `StrategyRecord` — spec + lifecycle `status` (`GENERATED`, `REJECTED`,
  `INITIAL_BACKTEST`, `VALIDATED`, `FAILED`) + `universe`, `dataset_version`,
  `backtest_period`, net-of-cost `metrics`, `robustness` report, `enabled`,
  `created_at`, `notes`.
- `StrategyRegistry` (ABC) + thread-safe `InMemoryStrategyRegistry`: `register`,
  `get`, `list_all` (filter by status/enabled), `update`, `set_status`,
  `set_enabled`, `compare`, `export`/`import_` (round-trips specs via
  `encode_spec`/`decode_spec` and metrics with `TradingMode` + dates).
- Every strategy keeps its unique `id` + `version`, making experiments
  reproducible and comparable.

## 7. Anti-data-mining guards

`src/qtrader/application/research/strategy/robustness.py`:

- `RobustnessChecker` flags, per `RobustnessLimits`:
  - **complexity** — score above `max_complexity`;
  - **min trades** — too few trades to be meaningful (`min_trades=30`);
  - **extreme performance** — CAGR > 200% or drawdown worse than −50% (or
    total return > 2000% with trades);
  - **narrow parameters** — razor-thin entry-vs-exit thresholds;
  - **parameter instability** — `pstdev` of Sharpe across ± jittered variants
    (`_jitter_spec`, step = `max(1.0, 0.25·|threshold|)`) above `max_instability`.
- The engine **never optimizes on the OOS window**; OOS is walked forward with a
  fixed protocol and the aggregate OOS summary must pass the same `MetricGate`.

## 8. Workflow pipeline

`src/qtrader/application/research/strategy/engine.py`:

```
Feature Library -> Generator -> register GENERATED -> initial backtest (net of costs)
-> MetricGate -> robustness (incl. jitter) -> Walk-Forward/OOS -> OOS MetricGate
-> registry VALIDATED
```

- `MetricGate` (min `sharpe` 0.0, `profit_factor` 1.0, `win_rate` 0.4, `trades` 30,
  max drawdown −0.5); `ResearchPlan` (search limits, gate, robustness limits,
  capital 100k, 10/50 bps, warmup 30, instability budget 12, folds 4);
  `ResearchRequest`; `ResearchReport` (generated / rejected / passing_initial /
  validated / backtests_run / rejected_reasons).
- `StrategyWalkForwardValidator(WalkForwardValidator)` overrides `_fit_model` to
  return the spec and `_simulate_fold` to evaluate rules with `StrategyEvaluator`,
  restricting trading to the held-out window `[ts, te)` — the 3%/6% stop and
  calendar-aligned OOS protocol are inherited from the frozen baseline.
- Budget enforcement: `computational_budget` caps total backtests; the
  `instability_budget` caps jitter runs; failures are logged to the
  `SystemLogRepository` and counted in `rejected_reasons`.

### Validation criteria

A strategy is `VALIDATED` only if: generation constraints pass **and** initial
net-of-cost backtest clears the `MetricGate` **and** robustness passes **and** the
walk-forward OOS summary clears the `MetricGate`. Anything else is `REJECTED`
(with the reason) or `FAILED` (no price history / no OOS). No strategy leaves the
research scope.

## 9. Integration, settings and DI

- `WalkForwardValidator` (research refactor): new `strategy_label: str = STRATEGY_LABEL`
  param, stored as `self._strategy_label`, used in `PerformanceMetrics.from_series(...)`
  so research walk-forwards persist under the strategy ID. Default behaviour is
  unchanged for the ML baseline.
- `settings.py::StrategyResearchSettingsMixin` (mounted on `Settings`): 12 knobs
  (max strategies/budget, max indicators/conditions, intervals, 10/50 bps costs,
  capital, min trades/sharpe, instability budget, regime gate) and a
  `strategy_research_plan` property building the `ResearchPlan`.
- `container.py`: registers the `InMemoryStrategyRegistry`,
  `StrategyResearchEngine` (wired from settings), and the
  `StrategyResearchAdapter` (research seam — `run(request)` + `registry`).
- `adapters.py` / `research/__init__.py`: `StrategyResearchAdapter` exported.

## 10. Files added / modified

- `src/qtrader/application/research/strategy/` (new package): `__init__.py`,
  `specs.py`, `feature_library.py`, `generator.py`, `registry.py`, `evaluator.py`,
  `robustness.py`, `engine.py`.
- `src/qtrader/application/services/walk_forward.py` — `strategy_label` refactor.
- `src/qtrader/application/research/adapters.py`, `research/__init__.py` —
  `StrategyResearchAdapter`.
- `src/qtrader/config/settings.py` — `StrategyResearchSettingsMixin`.
- `src/qtrader/config/container.py` — registry/engine/adapter wiring.
- `tests/unit/test_strategy_research.py` (new) — 31 tests.

## 11. Tests performed

- **New** `tests/unit/test_strategy_research.py` (31 tests): feature library
  (categories, required features), spec encode/decode round-trip + validation
  errors, generator (max-strategy cap, well-formedness, constraint rejection,
  budget), evaluator (warm-up holds, entry/exit fire, regime gate, cross
  operator, missing-series ⇒ HOLD), registry (CRUD, status flow, export/import,
  unknown-id errors), robustness (pass, min trades, extreme CAGR, narrow params,
  instability), `MetricGate`, `StrategyWalkForwardValidator`
  (OOS persists under the strategy label), `StrategyResearchEngine` (full
  pipeline counts, budget caps backtests, strict gate rejects everything,
  net-of-cost 10/50 bps assertion, jitter budget), settings → plan defaults.
- **Full suite**: **589 passed, 27 deselected** (baseline 558 → +31).
- **Lint/type**: `ruff check src tests` clean; `mypy src` clean (141 files).

## References

- Phase 2 package: `src/qtrader/application/research/strategy/*`.
- Workflow: `src/qtrader/application/research/strategy/engine.py`.
- Refactor: `src/qtrader/application/services/walk_forward.py` (`strategy_label`).
- Settings/DI: `src/qtrader/config/settings.py`, `src/qtrader/config/container.py`.
- Tests: `tests/unit/test_strategy_research.py`.
- Prior audits: `docs/audit/21-phase1-research-infrastructure.md`,
  `docs/audit/20-phase3-multitimeframe.md`, `docs/audit/12-phases6to20.md`
  (frozen research protocol, 3%/6% stops, calendar-aligned OOS).
