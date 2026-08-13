# Phase 4 — Execution-Aware Backtesting & Robustness Verdicts

Date: 2026-08-13. Work request: build the execution layer between a Phase 3
`VALIDATED` strategy and any hypothetical live fill — a deterministic, bar-driven
execution simulator (spread/slippage/impact/latency, order types, partial fills,
rejections, gaps, liquidity participation caps, trading-hours gate) that re-runs
the *identical* research signals through realistic execution and gates the
strategy with a new verdict: `EXECUTION_REJECTED` / `EXECUTION_SENSITIVE` /
`EXECUTION_ROBUST`. Built on `23-phase3-validation.md` (baseline `cca75c8`).
**Research only — no live trading, and no bid/ask or order-book data is ever
fabricated: every microstructure-shaped number comes from documented, explicit
assumptions.**

## Executive verdict

**COMPLETE AND VERIFIED.** `src/qtrader/application/execution/` implements the
full Phase 4 kernel and the Phase 3 pipeline now advances every `VALIDATED`
strategy through it:

```
VALIDATED (phase 3) -> theoretical reference backtest on the OOS window
  -> per-scenario execution-aware backtest (same model_outputs)
  -> ExecutionMetrics + degradation vs theoretical
  -> classify_execution -> EXECUTION_REJECTED / EXECUTION_SENSITIVE / EXECUTION_ROBUST
```

The execution broker reuses the production `BacktestRunner._simulate` fill loop
(pluggable `broker:` seam) with the same signal engine, sizing and risk gates
used during validation — signals are never changed, only how they fill. Full
suite **667 passed** (+36 execution tests); `ruff check src tests` clean;
`mypy src` clean (160 files). **No new dependencies.** Nothing trades live.

## 1. Explicit assumptions only (no fabricated microstructure)

`src/qtrader/application/execution/models.py`:

- `SlippageAssumptions` — per-scenario friction: `base_spread_bps` (assumed
  half-spread), `base_slippage_bps` (queueing/latency), `impact_coefficient`
  (market impact ∝ order participation in the symbol's ADV dollars),
  `volatility_multiplier` × ATR% × latency scaling (adverse drift),
  `gap_threshold_pct`, `max_slippage_bps` (per-fill cap), `partial_fill_scale`.
- `default_slippage_assumptions()` — four named scenarios:
  `optimistic / baseline / conservative / stress`. Baseline is calibrated to be
  *at least as expensive* as the research 10 bps commission / 50 bps slippage
  assumption, so execution-aware results are never rosier than research ones
  (verified: baseline degradation ≈ 0 on the smoke fixture).
- `LiquidityAssumptions` — `max_participation_rate` (0.10), `max_notional_pct_adv`
  (0.01), `min_avg_volume` / `min_avg_dollar_volume` floors, `adv_window_bars`.
- `ExecutionPlan` — scenarios, per-scenario slippage, liquidity, commission,
  and the classification gates: `min_fill_rate` (0.90), `max_rejected_rate`
  (0.25), `min_net_sharpe` (0.0), `max_absolute_sharpe_degradation` (0.5),
  `max_return_degradation` (0.5), `seed`.
- `TradingHoursPolicy` — defaults to `always_open=True` (Yahoo emits session
  bars only, so every bar is executable); session times supported for intraday.

## 2. Slippage, liquidity and cost models (pure, unit-testable)

- `slippage.py::SlippageModel` — `slippage_bps(order_notional, adv_dollar, atr_pct)`
  = base spread + base slippage + impact + adverse drift, capped at
  `max_slippage_bps`; `fill_price(side, reference, ...)` adjusts buy-up/sell-down.
- `liquidity.py::LiquidityModel` — `adv_for(bars)` (avg volume + dollar volume
  over the window straight from OHLCV), `check_size(...)` (submit-time gate:
  volume/dollar floors + notional-vs-1%-ADV budget → "unrealistic trade size"),
  `max_fillable(bar)` (participation-rate cap for partial fills).
- `costs.py::TransactionCostModel` — commission = bps × notional rounded to
  cents, optional minimum — mirrors the research commission convention.

## 3. Execution simulator

`src/qtrader/application/execution/simulator.py::ExecutionSimulator` — pure,
seeded (`random.Random(seed)`), bar-driven:

- Same execution convention as research: an order queued on a signal bar becomes
  actionable at the **next bar's open**.
- Order types: `MARKET` (fills at the next open + friction), `LIMIT` (only when
  the bar trades through the limit), `STOP` → market once triggered; **gap
  through** fills at the (worse) opening price, otherwise at the trigger.
- Partial fills: a fill may be capped by `max_fillable` and keep working for
  later bars (`partial_fill_scale`); `stats` records `filled` / `partial_fills`
  correctly when the order eventually completes.
- Rejections: unrealistic sizes / below-floor symbols are rejected at submit
  (`unrealistic_orders` counted); a submitted-but-unfilled order stays working.
- Last-signal-wins replacement per symbol+side; `cancel_side` for bracket exits
  (stop fired first cancels the take-profit), `cancel_all` at end-of-test.
- Stats: fills, partials, rejected, canceled, slippage bps list, deviation bps
  list (vs. reference), total commission, total slippage.

## 4. Broker and backtest integration

- `backtest.py::ExecutionBroker(BacktestBroker)` — adapter that routes every
  research order through the simulator; `exit_fill` covers bracket/end-of-test
  exits (cancel working side, fill with friction); `pending` exposes open orders.
- `ExecutionAwareBacktestRunner(BacktestRunner)` — builds a fresh simulator per
  run for one scenario; **reuses `BacktestRunner._simulate` unchanged** through
  the new pluggable `broker:` kwarg, so signals, sizing, warmup and risk gates
  are identical to validation. After a run: `last_stats()`, `last_assessments()`,
  `last_adv_seen()`.
- `services/backtest.py` (modified): `_simulate` accepts `broker:`; the stock
  `BacktestBroker` gained `exit_fill`; `_open_position` / `_close_position`
  support partial fills (position averaging + slice realizations) while the
  default research behavior is unchanged (18/18 existing broker tests pass).

## 5. Metrics and classification

`src/qtrader/application/execution/metrics.py` (pure functions):

- `compute_execution_metrics` — per-scenario: `expected_slippage_bps`,
  `avg_execution_deviation_bps`, `fill_rate`, `partial_fill_rate`,
  `rejected_rate`, `transaction_costs`, `turnover`, `net_return` / `net_sharpe` /
  `net_sortino` / `max_drawdown`, `trades`, **degradation vs theoretical**
  (`degradation_return`, `degradation_sharpe`), liquidity flags
  (`unrealistic-order-size-rejected`, `SYMBOL:below-min-avg-volume`,
  `SYMBOL:below-min-avg-dollar-volume`, per-symbol assessment reasons), and
  **human-readable `rejection_messages`** (e.g. `REJECTED: {symbol} average daily
  volume 1,000 shares below floor 50,000`).
- `classify_execution` — the gate: reject on fill rate / rejected rate / negative
  baseline net Sharpe; sensitive on worst-scenario Sharpe or return degradation;
  otherwise robust.
- `verdict_message` — composes the full human-readable verdict reason for the
  report (`REJECTED: …` for every failing gate, `SENSITIVE: …` for degradation,
  `EXECUTION ROBUST: …` otherwise); machine flags stay in `liquidity_flags`.

## 6. Engine and pipeline integration

- `engine.py::StrategyExecutionEngine` — per `StrategyRecord` + `ExecutionRequest`:
  loads bars for the OOS window, computes the theoretical reference backtest
  (research 10/50 bps assumptions), then runs every plan scenario through the
  execution-aware runner, collects `ScenarioResult`s and metrics, and emits a
  `StrategyExecutionReport` (`status`, theoretical summary, per-scenario
  metrics, worst scenario, notes). No price history → `EXECUTION_REJECTED`
  (`strategy_id = spec.id`). Runs are analysis-only (`_NoopBacktestRepository`).
- `research/validation/engine.py` (modified): after a strategy clears OOS +
  benchmark gates → `VALIDATED`, the engine immediately runs the execution
  verdict on the OOS window and **replaces** the stage with `EXECUTION_*`; the
  registry `StrategyStatus`, `ValidationStage`, `FinalStatus` and report counters
  all gain the three execution outcomes; `_rank_validated` now includes
  `EXECUTION_ROBUST` records.
- `research/strategy/registry.py` (modified): `StrategyStatus` gains
  `EXECUTION_REJECTED`, `EXECUTION_SENSITIVE`, `EXECUTION_ROBUST`.
- `research/validation/records.py` (modified): `ValidationStage` + 3,
  `FinalStatus` + 3, `ValidationReport` + `execution_rejected/sensitive/robust`,
  `ValidationRecord.execution_report` JSON round-trips via
  `encode_execution_report` / `decode_execution_report`.

## 7. Settings and DI

- `settings.py::StrategyExecutionSettingsMixin` — `strategy_execution_*` knobs
  (commission, gates, liquidity floors, seed) and `strategy_execution_plan`
  building an `ExecutionPlan`.
- `container.py` — registers `StrategyExecutionEngine` + `StrategyExecutionAdapter`
  and injects the same engine into `StrategyValidationEngine(execution_engine=…)`
  so the production pipeline actually gates validated strategies.
- `adapters.py` / `research/__init__.py` — `StrategyExecutionAdapter` exported
  (execution imports kept lazy to avoid the `research` ↔ `execution` import cycle).

## 8. Files added / modified

- `src/qtrader/application/execution/` (new package): `__init__.py`, `models.py`,
  `slippage.py`, `liquidity.py`, `costs.py`, `simulator.py`, `backtest.py`,
  `metrics.py`, `engine.py`.
- `src/qtrader/application/services/backtest.py` — pluggable `broker:`, `exit_fill`,
  partial-fill position accounting.
- `src/qtrader/application/research/strategy/registry.py` — 3 new statuses.
- `src/qtrader/application/research/validation/{records,engine}.py` — execution
  stage/counters/report + verdict hook.
- `src/qtrader/application/research/{adapters,__init__}.py`,
  `src/qtrader/config/{settings,container}.py` — adapter + settings + DI.
- `tests/unit/test_execution_models.py` (new) — 31 tests.

## 9. Tests performed

- **New** `tests/unit/test_execution_models.py` (36 tests): slippage (buy/sell
  adverse fills, impact scaling, max cap, ATR drift), liquidity (ADV window,
  volume/dollar floors, unrealistic-size rejection, participation cap), costs
  (bps scaling, minimum), simulator (market next-bar fill, rejection, partial
  fill across bars, buy-stop gap fill, untriggered stop, limit pass-through,
  same-side replacement, cancel, stats), metrics (rates/degradation/turnover,
  liquidity flags, human-readable rejection messages), classification (robust,
  reject on fill/sharpe, sensitive on degradation), verdict message (robust,
  reject/sensitive reasons, carries liquidity messages).
- Updated Phase 3 engine test to the new semantics: the lenient-plan full-pipeline
  test now asserts all strategies reach `EXECUTION_*` with
  `execution_report` populated and `execution_rejected + execution_sensitive +
  execution_robust == validated`.
- **Full suite**: **667 passed** (baseline 631 → +36).
- **Lint/type**: `ruff check src tests` clean; `mypy src` clean (160 files).

## References

- Phase 4 package: `src/qtrader/application/execution/*`.
- Workflow: `src/qtrader/application/execution/engine.py`.
- Phase 3 gate: `src/qtrader/application/research/validation/engine.py`.
- Settings/DI: `src/qtrader/config/{settings,container}.py`.
- Tests: `tests/unit/test_execution_models.py`, `tests/unit/test_strategy_validation.py`.
- Prior audits: `docs/audit/23-phase3-validation.md`,
  `docs/audit/22-phase2-strategy-research.md`.
