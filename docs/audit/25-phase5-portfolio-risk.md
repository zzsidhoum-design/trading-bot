# Phase 5 — Independent Portfolio & Risk Management Engine

Date: 2026-08-14. Work request: build a portfolio and risk-management engine
that sits authoritatively between strategy/AI decisions and execution —
position sizing, portfolio constraints, drawdown protection + kill switch,
correlation/concentration monitoring, risk-aware strategy allocation, and
risk-adjusted performance metrics — fully independent of the AI agents (AI
outputs are only `ProposedTrade` inputs) and of the Phase 6 AI strategy
selector. Built on `24-phase4-execution.md` (baseline `2a9a335`).
**Research only — never enables live trading: the engine can only gate,
cap and reject orders; nothing in Phase 5 can place a real trade.**

## Executive verdict

**COMPLETE AND VERIFIED.** `src/qtrader/application/portfolio_mgmt/` implements
a new `PortfolioManager` (the only entry point) delegating to a
`PortfolioRiskEngine` — a deterministic gate in front of the Phase 4 execution
simulator:

```
Strategy/AI decision -> ProposedTrade
  -> PortfolioManager.propose(...)                 (only public entry)
       -> PortfolioRiskEngine.evaluate(...)        (gate)
            kill switch
            strategy control status (SUSPENDED rejects; MONITORED/REDUCED factors)
            data-quality guard
            drawdown protection (portfolio + strategy + daily loss + kill switch)
            sizing (volatility / ATR / risk-budget)
            constraints (position/portfolio exposure, sector, correlated, leverage)
            optional Phase 4 execution-liquidity cap
       -> ClearedOrder to execution  |  DecisionRecord (reason + cap/reject)
  -> manager.allocate(...) risk-aware strategy weights (score + controls)
```

Phase 3 `EXECUTION_ROBUST / EXECUTION_SENSITIVE / VALIDATED` strategies feed the
allocator (research reports only); the AI strategy selector (Phase 6) is never
consulted. Full suite **771 passed** (+104 portfolio/risk tests); `ruff check
src tests` clean; `mypy src` clean (171 files). **No new dependencies.**
Nothing trades live.

## 1. Pipeline and independence guarantees

- Pipeline: `Strategy/AI decision -> PortfolioManager -> PortfolioRiskEngine ->
  Execution Simulator -> Execution` (gate before the Phase 4 liquidity cap).
- Gate order: kill switch -> strategy control status -> data quality ->
  drawdown protection -> sizing + constraints -> optional execution-liquidity cap.
- AI outputs enter only as `ProposedTrade` (strategy_id, symbol, side,
  reference price, volume/ATR/vol, sector, correlation hint, intent). Every
  decision is deterministic — no randomness in sizing/constraints/allocation.
- The AI selector (Phase 6) is deliberately not part of this engine; allocation
  consumes only research validation reports via `StrategyAllocator`.

## 2. Models

`src/qtrader/application/portfolio_mgmt/models.py`:

- `PortfolioSnapshot` (dataclass): `equity`, `cash`, `positions_count`,
  `gross_exposure_pct`, `leverage_pct`, `unrealized_pnl_pct`,
  `portfolio_drawdown_pct`, `daily_pnl_pct`, `positions` (Holding tuples).
- `Holding`: symbol, quantity, avg_price, market_value, weight_pct, sector,
  correlation_to_portfolio, unrealized_pnl_pct.
- `ProposedTrade`: strategy_id, symbol, side, intent (ENTER/EXIT/ADJUST),
  reference_price, atr_pct, volatility_pct, sector, correlation_to_portfolio.
- `PositionSizingPolicy`: mode (`atr`/`volatility`/`risk_budget`),
  `risk_per_trade_pct` (default 0.01), `max_position_weight_pct` (0.25),
  `atr_stop_multiple` (2.0), `vol_scale_factor` (1.0), `min_weight_pct`,
  `max_weight_pct`, `min_equity`, `round_lot`.
- `PortfolioConstraints`: `max_position_weight_pct` (0.25),
  `max_portfolio_exposure_pct` (1.0), `max_position_count` (40),
  `max_sector_exposure_pct` (0.40), `max_correlated_exposure_pct` (0.20),
  `correlation_threshold` (0.70), `max_leverage_pct` (0.0 = no leverage).
- `DrawdownProtection`: `max_portfolio_drawdown_pct` (0.20),
  `max_daily_loss_pct` (0.02), `max_strategy_drawdown_pct` (0.25),
  `max_consecutive_losses` (5), `monitor_drawdown_pct` (0.15),
  `reduce_drawdown_pct` (0.20), `suspension_cooldown_days` (30),
  `monitored_weight_factor` (0.75), `reduced_weight_factor` (0.5).
- `SizingVerdict`, `ConstraintVerdict`, `GateVerdict` (approved/rejected,
  reason, optional cap weight), `ClearedOrder`, `DecisionRecord`
  (per-trade outcome), `MonitoringReport`, `KillSwitchState`.
- Strategy control statuses: `ACTIVE`, `MONITORED`, `REDUCED`, `SUSPENDED`
  (with `suspended_until` deadline, cooldown re-arm).

## 3. Metrics and sizing (pure functions)

`metrics.py` — risk-adjusted performance on a returns series:
`total_return` (product compounding), `annualized_return` (geometric
`(1+r)^periods_per_year - 1`), `annualized_volatility`, `sharpe_ratio` (0.0
when vol ≈ 0), `sortino_ratio` (downside dev below 0), `max_drawdown` (peak-to-
trough), `var_95` (5th percentile of daily returns — negative number),
`expected_shortfall`, `win_rate`, `profit_factor` (∞ when no losing trades),
`calmar_ratio` (0.0 when drawdown ≈ 0), `consecutive_losses` (current streak),
`is_drawdown_period` (below running peak).

`drawdown.py` — `DrawdownGuard`: `portfolio_daily_loss` (loss-limit gate),
`portfolio_drawdown` (drawdown-limit gate), `transition` (portfolio-level
status: `ACTIVE -> MONITORED/REDUCED/SUSPENDED` with kill-switch re-arm
deadline), `strategy_recommendation` (per-strategy status from
strategy drawdown / consecutive losses / cooldown re-arm), `KillSwitch`
(trip/rearm state, monotonic re-arm). `control_state(...)` builds strategy
control records; `weight_factor(protection)` maps status → 1.0 / monitored /
reduced / 0.0.

`sizing.py` — `PositionSizer`: `size(trade, snapshot, policy, market_data)` for
- `atr`: `weight = risk_per_trade / (atr_multiple * atr_pct)` clamped to
  `[min_weight_pct, max_position_weight_pct]`;
- `volatility`: `weight = min(risk_per_trade / volatility, max_position_weight_pct)`;
- `risk_budget`: allocate an equal slice of `risk_per_trade` across up to
  `max_position_count` holdings (budget = max allowed weight, capped by
  `max_position_weight_pct`);
plus `apply_control_weights(weight, status, protection)` (factor scaling),
`floor_quote_quantity`, `notional_capped_size` (cap to `max_notional_pct_adv`),
and `round_to_lot`.

## 4. Constraints and correlation (pure functions)

`constraints.py` — `ConstraintEngine.evaluate(snapshot, trade, size, correlation_provider)`
checks: position weight (`weight_pct > max_position_weight_pct`),
portfolio exposure (`gross + weight > max_portfolio_exposure_pct`),
position count, sector exposure (incl. `sector=None` → no sector constraint),
correlated exposure (trade `correlation_to_portfolio` or provider), and
leverage. Capping logic (see §7) yields `ConstraintVerdict` with `cap_weight`.

`correlation.py` — `correlation_series_from_returns` (Pearson via
`statistics.correlation`, NaN when constant series), `pairwise_correlation`
(average |corr| above threshold), `proposed_correlated_exposure` (existing
correlated weight + new trade weight when correlated), `correlated_exposure` /
`portfolio_concentration` (HHI) for the monitoring report.

## 5. Allocation (research reports only, AI-independent)

`allocation.py` — `StrategyAllocator.allocate(records, control_states, regime_quality)`:
- **Eligibility**: research `EXECUTION_ROBUST / EXECUTION_SENSITIVE /
  VALIDATED` only; others excluded.
- **Score** (weighted, non-negative): `sharpe`, `total_return`,
  `max_drawdown` (inverse), `execution_quality` (robust > sensitive > validated),
  `regime_quality` (vs 0.5 baseline), minus `correlation_penalty` (pairwise
  corr above threshold). All dimensions normalized; floor ensures `score ≥ 0`.
- **Weights**: raw = score × control `weight_factor`; normalized to a target
  `min(1.0, n × max_weight_pct)`; **clamped to `max_weight_pct` (default 0.50)**;
  remainder redistributed to under-cap strategies with **`0.0 < w < max`**
  (zero-weight SUSPENDED strategies never receive redistributed weight).
- Emits `AllocationReport` with per-strategy `score`, `weight_pct`, rationale,
  and `risk_flags` (`"high_correlation"`, `"excluded"`, `"reduced"`, `"suspended"`).

## 6. Engine and manager

`engine.py` — `PortfolioRiskEngine(sizing, constraints, drawdown, correlation,
liquidity_checker, kill_switch)`:
- `evaluate(trade, snapshot, status)` → `DecisionRecord`: kill-switch trip,
  control status gates (SUSPENDED reject), data-quality guard (zero/negative
  price → reject), drawdown gates, sizing, constraint evaluation (approve /
  cap / reject), optional execution-liquidity cap (`LiquidityChecker` on
  `(symbol, price, notional)`; `make_liquidity_checker` builds the Phase 4
  liquidity gate with a real `max_fillable`), and weight-factor scaling of the
  approved quantity via `apply_control_weights`.
- `monitor(snapshot, strategy_states)` → `MonitoringReport` (portfolio
  drawdown, daily loss, concentration HHI, max position weight, correlated
  exposure, drawdown-period flags, per-strategy status transitions).
- `trip_kill_switch()` / `rearm_kill_switch()`.

`manager.py` — `PortfolioManager` (the only entry point): `propose(trade,
snapshot, status)` → `ClearedOrder` or rejected `DecisionRecord`;
`allocate(...)` delegates to the allocator; `monitor(...)` delegates to the
engine and returns status transitions; `trip_kill_switch()` /
`rearm_kill_switch()` forward to the engine.

`adapters.py` — lazy `import` of the Phase 4 execution `LiquidityChecker` to
avoid a hard `portfolio_mgmt -> execution` dependency at import time.

## 7. Leverage semantics and capping logic (final)

- `projected_leverage = max(0.0, projected_exposure - 1.0)`,
  `projected_exposure = snapshot.gross_exposure_pct + size.weight_pct`.
- `remaining_leverage = max_leverage_pct - snapshot.leverage_pct`; default
  `max_leverage_pct=0.0` means no exposure beyond 100% equity.
- `hard_blocks` = violations that are not `"position size"` and not
  (`"projected leverage"` with `remaining_leverage > 0.0`). Any hard block
  → `REJECT`.
- Cappable when `position_breach` **or** (`leverage_breach` and
  `remaining_leverage > 0.0`): `cap_weight = max_position_weight_pct`; when
  `leverage_breach`, `cap_weight = min(cap_weight, max(0.0, remaining_leverage))`.
- Oversized-position-only caps to `max_position_weight_pct` (e.g. 250);
  leverage-only caps to the remaining budget (e.g. 50).

## 8. Settings and DI

- `settings.py::PortfolioRiskSettingsMixin` — `portfolio_*` knobs
  (constraints, drawdown protection, sizing policy, allocation policy, kill
  switch) and `portfolio_risk_engine()` building the engine with the Phase 4
  `make_liquidity_checker`.
- `container.py` — registers `PortfolioManager` + `PortfolioRiskEngine` +
  `StrategyAllocator`, wiring the settings mixin; verified by the settings
  snapshot test (no existing settings/container tests broken).

## 9. Files added / modified

- `src/qtrader/application/portfolio_mgmt/` (new package): `__init__.py`,
  `models.py`, `metrics.py`, `sizing.py`, `constraints.py`, `correlation.py`,
  `drawdown.py`, `allocation.py`, `engine.py`, `manager.py`, `adapters.py`.
- `src/qtrader/config/settings.py`, `src/qtrader/config/container.py` —
  `PortfolioRiskSettingsMixin` + engine/manager/allocator registration.
- `tests/unit/fakes_portfolio_mgmt.py` (new) — fake validation records
  (`ValidationRecord`, `FinalStatus`, `StrategySpec` + `EntryRule` + `Condition`)
  used by allocator/manager tests.
- `tests/unit/test_portfolio_metrics.py`, `test_position_sizing.py`,
  `test_portfolio_constraints.py`, `test_portfolio_correlation.py`,
  `test_drawdown_protection.py`, `test_strategy_allocation.py`,
  `test_portfolio_risk_engine.py`, `test_portfolio_manager.py` (new).

## 10. Tests performed

- **New** (104 tests): metrics (returns, annualized 1.01^252-1, vol, sharpe,
  sortino, max drawdown 0.945, VaR 0.05, ES, win rate, profit factor, calmar,
  consecutive losses, drawdown period), sizing (ATR/vol/risk-budget modes,
  clamps, control weights, floor/round, notional cap), constraints (position
  size reject/cap, portfolio exposure, sector, correlated, leverage reject vs
  cap, cap-to-max-weight), correlation (series, pairwise, proposed exposure,
  concentration), drawdown (portfolio daily loss, drawdown gate, per-strategy
  monitor→reduce→suspend, consecutive losses, cooldown re-arm, deadline range,
  weight factors, kill switch), allocation (risk-adjusted score ordering,
  SUSPENDED → 0 weight, REDUCED less weight, max-weight clamp, regime quality,
  execution robustness, correlation penalty, exclusion, rationale/risk flags),
  risk engine (approve, cap, reject paths; weight-factor scaling; data-quality
  guard; execution-liquidity cap; sizing-method switch; monitoring report;
  kill switch; stress scenarios: gap/vol/liquidity/stress — caps then rejects),
  manager (propose → ClearedOrder, reject → None, gate forward, allocate
  delegation, monitor + status downgrade, kill-switch trip/rearm via manager).
- **Full suite**: **771 passed** (baseline 667 → +104), 27 skipped (pre-existing
  skips for optional Phase 2/3 data/AI paths).
- **Lint/type**: `ruff check src tests` clean; `mypy src` clean (171 files).

## References

- Phase 5 package: `src/qtrader/application/portfolio_mgmt/*`.
- Workflow: `src/qtrader/application/portfolio_mgmt/manager.py`.
- Settings/DI: `src/qtrader/config/{settings,container}.py`.
- Phase 4 liquidity gate: `src/qtrader/application/execution/liquidity.py`.
- Tests: `tests/unit/test_portfolio_*.py`, `tests/unit/test_strategy_allocation.py`,
  `tests/unit/test_portfolio_manager.py`, `tests/unit/fakes_portfolio_mgmt.py`.
- Prior audits: `docs/audit/24-phase4-execution.md`,
  `docs/audit/23-phase3-validation.md`.
