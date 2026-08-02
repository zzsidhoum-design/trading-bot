# 06 — Testing, Backtesting & Trading-Mode Safety Gate

## 1. Test Pyramid

```
        e2e: one full backtest run end-to-end (few)
      integration: repos + Postgres, Redis, broker gateway, event bus (dozens)
    unit: indicators, risk math, portfolio sizing, agent decision logic, DTOs (hundreds)
```

| layer | tools | what is verified |
|---|---|---|
| unit | pytest + pytest-asyncio | pure logic: every indicator, RiskCalculator numbers, AllocationPolicy, ensemble weights, DTO validation; agent logic with fake ports |
| integration | pytest + asyncpg + `testcontainers` (Postgres), `fakeredis`/redis, real FastAPI TestClient | repositories, migrations (Alembic upgrade on fresh DB), broker adapter recording fills, event bus delivery + outbox, API routes against test DB |
| e2e | pytest + Docker Compose | a full backtest run: seed prices → pipeline → decisions → fills → P/L; then a paper-trading day on synthetic live feed |

**Fakes live in `tests/fakes/`** (FakeBroker, InMemoryRepository, StubLLM, StaticFeedProvider). The composition root makes swapping them trivial — no mocks scattered through tests.

**Coverage gate:** ≥ 90% on `application/` and `domain/`; ≥ 70% overall. Enforced in CI.

### Fault injection & load

Phase 8 adds failure-path coverage (see `docs/07-hardening.md`):

- `tests/unit/test_resilience.py` — circuit-breaker open/half-open/recovery, token bucket, transient-only retries.
- `tests/unit/test_yahoo_resilience.py` — provider retries + breaker trip/recovery against `MockTransport`.
- `tests/unit/test_ws_fanout.py` — concurrent fan-out to 8 WS clients + topic isolation.
- `tests/unit/test_shard.py` — shard determinism and balance.
- `tests/integration/test_hardening.py` — a `FailingBroker` that always raises `BrokerUnavailable`; orders are rejected as `REJECTED` events (graceful degradation) and breaker snapshots are reachable through the live container.
- `scripts/load_test.py` — asyncio/httpx load harness (requests/sec + latency percentiles); run with `--concurrency`/`--duration`/`--path`.

## 2. Backtesting Engine

- `BacktestRunner` replays stored historical bars (via `PriceRepository.history`) through the **production analysis code**: the same `IndicatorEngine` signals and `RiskCalculator` sizing used live — no special-casing in the strategy.
- Deterministic: bars are processed in timestamp order with no randomness; same input → same run (`backtest_runs`). A determinism test replays the same history twice and asserts identical results.
- No look-ahead: `_SignalEngine` computes EMA/RSI only on bars up to and including bar *t* (warm-up of `warmup_bars`); stops/limits are checked against the same bar's OHLC range.
- Fills happen at the **next bar's open** via `BacktestBroker`, with explicit `slippage_bps` (applied against the open) and `commission_bps` (on fill notional). Intrabar exits prefer the stop when both stop and take-profit are touched.
- Outputs: equity curve, trade list (with outcome `signal`/`stop`/`take_profit`/`end_of_test`), and `strategy_performance` metrics (Sharpe, Sortino, max drawdown, win rate, profit factor) computed by `PerformanceMetrics.from_series` — the basis for the graduation gate.
- Each run persists a `backtest_runs` row (status, final capital, metrics JSONB) and upserts the `strategy_performance` row under `(strategy, mode, period_start, period_end)`.
- Scheduled nightly as the `backtest_cycle` arq job (defaults in `Settings`: `backtest_interval`, `backtest_universe`, `backtest_lookback_days`, commission/slippage, warmup).

## 3. Paper Trading Mode

- Identical to live except the broker is `PaperBroker` (in-memory matching against live prices, same order lifecycle, no real money).
- Runs the *live* scheduler and live feeds, so latency/order-flow paths are exercised realistically.
- Paper results feed the same Memory System + metrics; the dashboard labels mode clearly.

## 4. Live Trading Mode & the Graduation Gate (`SystemGate`)

Live trading is a **state machine**, not a config flag alone:

```
backtest ──passes review──► paper ──meets graduation criteria──► live
   ▲                                                              │
   └────────────────────── failures revert ──────────────────────┘
```

`SystemGate.evaluate(strategy, mode)` returns `GateDecision`:

- `BACKTEST` mode is always `GRADUATED` (nothing to gate).
- `PAPER`/`LIVE` pull the latest `BACKTEST` `strategy_performance` row for the strategy and require it to clear every configured floor: `min_trades`, `min_win_rate`, `min_profit_factor`, `min_sharpe`, `max_drawdown`, `min_total_return`. Any failure is collected as a human-readable reason and the decision is `DENIED`.

`ExecutionAgent` consults the gate at order time (`can_trade`); a denial turns the order into a `REJECTED` `OrderStatusChanged` event instead of reaching the broker, so a hot-swap without the gate is impossible. Every decision is recorded in `system_logs` (INFO approved / WARN denied) with the evidence.

## 5. CI Pipeline (GitHub Actions)

`.github/workflows/ci.yml` runs on push to `main` and pull requests:

- `lint & typecheck` — `ruff check src tests` and `mypy src`.
- `unit tests` — `pytest -m "not integration and not e2e" --cov=qtrader --cov-report=term-missing`, enforcing the coverage gate (≥ 90% on `application/`/`domain/`, ≥ 70% overall).
- Integration/e2e suites (marked `integration`/`e2e`) run against Docker Compose (postgres+redis) with `QTRADER_RUN_INTEGRATION=1`, either in CI with services or locally.
