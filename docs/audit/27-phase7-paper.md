# Phase 7 — Paper Trading & Shadow Deployment

Date: 2026-08-15. Work request: deploy the full decision pipeline (Market Data →
Data Validation → Universe → Market Regime → Validated Strategies → AI Agents →
Selector → Portfolio & Risk → Execution → Paper Broker) into a **controlled
paper-trading environment** with shadow-mode support, real-time operational
telemetry, recovery safety, acceptance criteria and the 11 required audit
outputs below. Built on `26-phase6-ai.md` (the Phase 5 portfolio/risk engine
remains the **final authority** on every cleared order).
**Real-money trading stays disabled**: `QTRADER_MODE=backtest` and
`ENABLE_LIVE_TRADING=false` in `.env`, and `Settings.live_enabled` requires
BOTH the mode and the explicit flag. Shadow mode records decisions and never
submits anything — not even a paper order.

## Executive verdict

**COMPLETE AND VERIFIED.** New package `src/qtrader/application/paper/`
implements all 11 required outputs (see §1). Every proposed/simulated order is
recorded in an append-only JSON-lines ledger (`PaperOrderLedger`, same pattern
as the Phase 6 `DecisionLedger` — **no new Alembic migration required**),
operational telemetry lands in `agent_metrics` + `system_logs`, and the broker
installed in the container is `PaperExecutionBroker` (records every lifecycle
event) in paper/live mode, `ShadowBroker` (never submits) in shadow mode, and
the unchanged `PaperBroker` in backtest mode.

```
decision (Chief agent) / order intent
  -> PaperTradingService.route_decision    (dedupe by decision_ref)
       -> risk verdict recorded on the PaperOrderRecord
       -> paper mode:   PaperExecutionBroker -> PaperBroker/Alpaca (paper URL)
            records submit latency, fill, slippage, rejection reason
       -> shadow mode:  ShadowBroker -> records SHADOW_ONLY, never submits
  -> PaperOrderLedger (jsonl, reloaded on restart)  -> ledger_stats()
       -> PaperVsResearchComparator  (paper vs backtest/WF/exec-aware sim)
       -> AcceptanceEvaluator        (operational criteria, not profit-based)
  -> OperationalTelemetry -> agent_metrics / system_logs
```

Full suite **918 passed** (+48 Phase 7 tests), 27 skipped (pre-existing
optional-path skips); `ruff check src tests` clean; `mypy src` clean (191
source files). Container smoke-verified for backtest/paper/shadow broker wiring;
`tests/integration/test_pipeline_phase7.py` still passes against live Postgres.
**No new dependencies, no schema migrations.** Nothing trades live.

## 1. The 11 required outputs

| # | Output | Module | Entry point |
|---|--------|--------|-------------|
| 1 | Paper-trading architecture (env + pipeline routing) | `paper/service.py`, `config/container.py` | `PaperTradingService.route_decision` |
| 2 | Paper execution statistics | `paper/ledger.py` | `ledger_stats` |
| 3 | Strategy performance (paper) | `paper/comparison.py` | `PaperVsResearchComparator` |
| 4 | Agent contribution during paper trading | `paper/comparison.py`, `paper/telemetry.py` | `signal_frequency_*` / `_paper_signals` |
| 5 | Backtest vs paper discrepancies | `paper/comparison.py` | `PaperVsResearchComparator.compare` |
| 6 | Execution statistics (latency/slippage/fill/rejections) | `paper/ledger.py` | `ledger_stats` → `PaperRunStats` |
| 7 | Risk-engine intervention statistics | `paper/service.py` | `PaperTradingService.risk_intervention_stats` |
| 8 | Operational failures (reliability) | `paper/telemetry.py` | `OperationalTelemetry` / `operational_summary` |
| 9 | Recovery-test results | `paper/service.py` + `tests/unit/test_paper_service.py` | `PaperTradingService.recover` |
| 10 | Acceptance criteria (not profit-based) | `paper/acceptance.py` | `AcceptanceEvaluator.evaluate` |
| 11 | Files created/modified | this document (§11) | — |

## 2. Paper-trading architecture (output 1)

`src/qtrader/application/paper/` provides the controlled environment:

- **`PaperOrderLedger`** (`ledger.py`) — in-memory store keyed by decision
  reference with JSON-lines persistence (`data/paper/orders.jsonl`). Reloading
  an existing file after a process restart is exactly how the recovery path
  guarantees stale orders are re-polled without ever duplicating a trade.
- **`PaperExecutionBroker`** (`brokers.py`) — decorates `PaperBroker` or the
  Alpaca broker and records every lifecycle event: submit latency, broker id,
  fill, slippage (fill − requested, per share), commission and rejection reason
  (the original exception is re-raised after recording, so callers still see
  broker errors). Never fabricates a fill.
- **`ShadowBroker`** (`brokers.py`) — records each intended order as
  `SHADOW_ONLY` with a simulated reference price and returns a synthetic
  `shadow-` id. Nothing is ever sent to any venue.
- **`PaperTradingService`** (`service.py`) — routes decisions with duplicate
  protection (`decision_ref`), attributes the PortfolioRiskEngine verdict to
  each order, reconciles fills from the broker status, and exposes `recover()`
  and `risk_intervention_stats()`.
- **Container wiring** (`config/container.py`) — broker selection by mode:
  backtest → raw `PaperBroker` (unchanged); paper/live → `PaperExecutionBroker`;
  `paper_shadow_mode=True` → `ShadowBroker`. Ledger, telemetry, service,
  comparator and evaluator are registered so the running pipeline feeds them
  during continuous operation.

Real-time operation: the arq worker (heartbeat, `backfill_cycle` 15 min,
`scan_cycle` 5 min, `execute_cycle`, `train_cycle` — market-hours gated)
drives the pipeline; in paper mode every executed order flows through the
recording broker into the ledger automatically. `.env` currently runs in
backtest mode, so recording is deliberately inactive until the mode is PAPER.

## 3. Paper execution statistics (output 2)

`ledger_stats` produces `PaperRunStats` over the ledger:

- `total_orders`, `proposed`, `submitted`, `filled`, `partial`, `canceled`,
  `rejected`, `shadow_only`
- `fill_rate` = filled ÷ (filled + submitted)
- `avg_slippage_bps` = mean of `(fill − requested)/fill × 10_000`
- `avg_execution_latency_ms` = mean submit-to-fill latency
- `total_commission` (summed across records)
- risk verdict counts (`risk_approved / risk_capped / risk_rejected /
  risk_not_gated`)
- `earliest` / `latest` timestamps

## 4. Strategy performance (output 3)

`PaperVsResearchComparator` computes paper total return from the `trades`
repository (sum of PnL ÷ initial capital), paper max drawdown from the equity
curve, and paper trade frequency. This is what the acceptance evaluator and the
audit compare against the research evidence.

## 5. Agent contribution during paper trading (output 4)

- `OperationalTelemetry.signal_frequency(agent, count)` records a
  `signal_frequency_<agent>` metric per decision.
- The comparator aggregates `_paper_signals` (agents attached to each paper
  record's context) vs `_frequency_signals` from the signal repository, and
  reports the distinct contributing agents on each side.

## 6. Backtest vs paper discrepancies (output 5)

`PaperVsResearchComparator.compare(ComparisonInput)` emits one `ComparisonRow`
per dimension with `paper_value`, `research_value` and `divergence`:

- `total_return` — paper PnL return vs research `total_return`
- `avg_slippage_bps` — paper execution slippage
- `fill_rate` — paper fills vs the execution-aware simulation assumption
- `trade_frequency_per_day` — paper trades/day vs research trades ÷ 252
- `max_drawdown` — paper equity drawdown vs research
- `strategy_selection` — strategies selected on each side (+ set divergence)
- `agent_signal_frequency` — contributing agents on each side

Divergence is relative `|paper − research| / |research|` (guarded against
zero). `ComparisonInput.research_fill_rate` lets callers inject the Phase 4
execution-plan fill-rate assumption.

## 7. Execution statistics (output 6)

Covered by `PaperRunStats` (§3) plus the per-record rejection reason and
`OperationalTelemetry.latency("submit"|"fill_poll"|"cancel"|"modify_brackets")`
latencies. Rejection reasons are stored verbatim on each `PaperOrderRecord`
(`rejection_reason`) so broker refusals are fully explainable.

## 8. Risk-engine intervention statistics (output 7)

`PaperTradingService.risk_intervention_stats()` aggregates the verdict recorded
on each order: `decisions_evaluated`, `approved`, `capped`, `rejected`,
`intervention_rate = (capped + rejected) ÷ evaluated`, and a `reasons` counter
keyed by the rejection/cap reason string (e.g. "KILL SWITCH TRIPPED", "size
capped to N shares"). The risk verdict is attached in `route_decision` from the
Phase 5 `GateDecision` so intervention statistics need no extra queries.

## 9. Operational failures (output 8)

`OperationalTelemetry` (write-only, **never raises** — a telemetry failure must
not break the trading loop) records into `agent_metrics` and `system_logs`:

- `api_failure` metric + ERROR log (provider + error message)
- `missing_data` / `invalid_data` counters (no silent stale-data fallback)
- `reconnection` metric + WARN log
- `latency_ms_<stage>` metrics

`operational_summary(metrics)` aggregates a window into `OperationalSummary`:
`api_failures`, `missing_data`, `invalid_data`, `reconnections`, per-stage
average latencies, per-agent signal frequency, `data_reliability`
(1 − missing+invalid over data events) and `failure_rate` (failures ÷ total
calls).

## 10. Recovery-test results (output 9)

`PaperTradingService.recover()` reloads the ledger, re-polls every `SUBMITTED`
order **exactly once** and returns a `RecoveryReport` (`reloaded / repolled /
filled / still_pending / failed`). The tests in `tests/unit/test_paper_service.py`
+ `test_paper_brokers.py` cover the operational-reliability scenarios:

| Scenario | Test | Result |
|----------|------|--------|
| Process restart (ledger reload) | `test_recover_repolls_stale_orders_without_duplicates` | stale SUBMITTED repolled; `filled=1`; ledger count unchanged — no duplicate orders |
| Duplicate order (same decision redelivered) | `test_route_decision_duplicate_is_suppressed` | 2nd delivery returns the existing record; broker called once |
| Broker rejection | `test_route_decision_rejection_is_recorded` / `test_recording_broker_submit_rejection_is_audited` | REJECTED + reason recorded; exception re-raised |
| Disconnection / poll failure | `test_recover_handles_poll_failures` | `failed=1`, no crash, WARN logged |
| Invalid data / API failure telemetry | `test_operational_telemetry_never_raises` | telemetry never raises |
| Timezone/clock | ledger timestamps stored UTC; `PriceBar` requires tz-aware ts | n/a (covered by existing bar validation) |

## 11. Acceptance criteria (output 10)

`AcceptanceEvaluator.evaluate(stats, operational, comparison)` checks seven
criteria — **deliberately not profit-based**:

| Criterion | Threshold (default) | Failure mode |
|-----------|--------------------|--------------|
| fill_rate | ≥ 0.90 | orders not filling |
| slippage | ≤ 50 bps avg | execution cost creep |
| execution_latency | ≤ 5000 ms avg | slow fills |
| drawdown | ≤ 20% paper drawdown | risk-control break |
| paper_research_divergence | ≤ 10% total-return divergence | pipeline drift |
| data_reliability | ≥ 95% | data gaps |
| failure_rate | ≤ 5% | api instability |

A losing-but-operationally-clean paper run still passes (verified by
`test_acceptance_is_not_profit_based`). Thresholds are configurable through
`PaperSettingsMixin` (`paper_accept_*`).

## 12. Files created / modified (output 11)

**Created — `src/qtrader/application/paper/`:**
`__init__.py`, `models.py` (`PaperOrderRecord`, `PaperOrderStatus`,
`PaperRunStats`, `RiskInterventionStats`), `ledger.py` (`PaperOrderLedger`,
`ledger_stats`), `telemetry.py` (`OperationalTelemetry`, `NullTelemetry`,
`OperationalSummary`, `operational_summary`), `brokers.py`
(`PaperExecutionBroker`, `ShadowBroker`), `comparison.py`
(`PaperVsResearchComparator`, `ComparisonReport`), `acceptance.py`
(`AcceptanceEvaluator`, `AcceptanceThresholds`), `service.py`
(`PaperTradingService`, `RecoveryReport`).

**Created — tests:** `tests/unit/fakes_paper.py`,
`test_paper_ledger.py`, `test_paper_telemetry.py`, `test_paper_brokers.py`,
`test_paper_service.py`, `test_paper_comparison.py`, `test_paper_acceptance.py`,
`test_paper_settings.py`, `test_alpaca_env.py` (48 tests).

**Modified:**
- `src/qtrader/config/settings.py` — added `PaperSettingsMixin` (ledger path,
  shadow flag, acceptance thresholds) + `paper_acceptance_thresholds` builder.
- `src/qtrader/config/container.py` — paper imports; broker selection
  (shadow / recording / raw by mode); ledger + telemetry + service + comparator
  + evaluator registration.
- `src/qtrader/infrastructure/brokers/alpaca.py` — env-var unification: reads
  `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` (project canonical names) with
  `APCA_API_KEY_ID`/`APCA_API_SECRET_KEY` compatibility fallback; `ALPACA_LIVE`
  env override; error message updated.

**Live-trading safety:** `.env` unchanged (`QTRADER_MODE=backtest`,
`ENABLE_LIVE_TRADING=false`); Alpaca defaults to the paper URL; shadow mode
never submits; recording broker only activates in PAPER/LIVE modes.
