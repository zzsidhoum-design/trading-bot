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

## 2. Backtesting Engine

- Replays stored historical bars through the **same agent pipeline** (Data→Scan→Analyze→Predict→Decide→Risk→Allocate→Execute) — the only difference is the `BacktestBroker`, which fills orders at the next bar's OHLC (configurable: open/next-open/slippage model + commission).
- Deterministic: fixed seed, same input → same run (stored in `backtest_runs`).
- No look-ahead: indicators/predictions computed only on data up to bar *t*; a point-in-time test guards this.
- Slippage & costs modeled explicitly (bases, commission, borrow for shorts).
- Outputs: equity curve, trade list, and `strategy_performance` metrics (Sharpe, Sortino, max drawdown, win rate, profit factor) — the basis for the graduation gate.

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

`SystemGate.graduated_to_live()` checks, and refuses otherwise:

1. Latest CI run green (tests + coverage thresholds).
2. ≥ N backtest runs on out-of-sample windows with metrics above configured floors (Sharpe ≥ X, max DD ≤ Y).
3. ≥ M weeks of paper trading with live-feel data and tracked metrics above floors.
4. Risk policy validated (all limits parse, invariant checks pass at startup).
5. `ENABLE_LIVE_TRADING=true` explicitly set and `QTRADER_MODE=live` — both required, or the app refuses to start execution.

Every transition is recorded (who/when/evidence) in `system_logs`. The Execution Agent reads mode from `SystemGate` at order time, not from process env alone, so a hot-swap without the gate is impossible.

## 5. CI Pipeline (GitHub Actions)

- `lint` — ruff; `type` — mypy; `test-unit`; `test-integration` (compose: postgres+redis); `test-e2e` (compose full stack, backtest smoke run).
- A merge into `main` requires all green → the auto-generated `graduation` status in the dashboard reflects it.
