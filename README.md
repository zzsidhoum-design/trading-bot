# qtrader — Multi-Agent AI Trading System

Professional, scalable, event-driven trading platform. Python 3.12+, Clean Architecture, SOLID, async everywhere.

> **Status: REVIEW PHASES 1–12 DELIVERED** — the Phase 1–8 foundation (architecture, DI container, event-driven worker, ten agents, backtest engine + SystemGate, API + dashboard, resilience/hardening) shipped with all CI gates, followed by a 12-phase production-readiness review: structured logging + typed error envelopes (Phases 5–6), monitoring endpoints (`/system/metrics` with event counts, `/system/logs`, worker heartbeat in `/health`), dashboard agent-metrics writer, backtest failure-path + API hardening tests, paper-broker/execution resilience (REJECTED on poll failure, typed broker errors), performance work (SQL event counts, LLM rate limiting), and a security audit (constant-time auth, no secrets in logs, `pip-audit` clean). Suite: **285 unit / 311 with integration**, coverage **92.5%**, ruff + mypy clean. E2E order lifecycle verified live: Chief → Risk (ATR-sized, bracket stops) → Portfolio → Execution (paper fills, positions, trades, cash). See `docs/01–08`.

## Capabilities

- Real-time monitoring of thousands of symbols (partitioned `prices` table, event-driven ingestion).
- Multi-source data: any provider is a pluggable adapter.
- 10 collaborating AI agents (Data, Scanner, Technical, News, Fundamental, Prediction, Risk, Portfolio, Execution, Chief).
- LLM-powered news/earnings/central-bank analysis with schema-validated structured output.
- ML probability-of-movement models with a versioned registry.
- Automatic risk management (no unapproved order can reach a broker).
- Memory System: every trade, decision rationale, outcome, per-agent accuracy, per-strategy performance.
- Backtest → Paper → Live with an enforced safety gate.

## Tech Stack

| area | choice |
|---|---|
| Language / runtime | Python 3.12, asyncio |
| API / dashboard backend | FastAPI, Uvicorn, WebSocket |
| ORM / migrations | SQLAlchemy 2.0 (async), asyncpg, Alembic |
| DB / cache | PostgreSQL 16 (partitioned), Redis 7 |
| Jobs / scheduler | arq workers |
| Indicators | pandas + custom indicator classes (ta-lib optional) |
| ML | scikit-learn / XGBoost / LightGBM behind a `ModelRepository` port |
| LLM | provider-agnostic `LLMClient` (OpenAI / Anthropic / local) |
| Logs / metrics / traces | structlog, Prometheus, OpenTelemetry |
| Tests | pytest, pytest-asyncio, testcontainers, fakeredis |
| Infra | Docker Compose (postgres, redis, api, worker, dashboard) |

## Repository Layout

```
docs/            architecture · agents · database · data-flow · api · testing · hardening · operations
src/qtrader/
  config/        settings + DI composition root
  domain/        entities, value objects, events, ports (no dependencies)
  application/   agents, services, use cases, dto
  infrastructure/database, brokers, data_providers, news, llm, ml, cache, eventbus, schedulers
  interfaces/    api (FastAPI + WS), cli, dashboard
tests/           unit / integration / e2e + fakes
alembic/         migrations
scripts/         dev/ops helpers
```

## Docs

| doc | content |
|---|---|
| `docs/01-architecture.md` | layers, SOLID, DI, event bus, scaling, security |
| `docs/02-agents.md` | the 10 agents: responsibilities, I/O, algorithms, ports |
| `docs/03-database.md` | all tables, indexes, partitioning, Redis keyspaces |
| `docs/04-data-flow.md` | pipeline, event catalog, sequences, memory loop |
| `docs/05-api.md` | REST + WebSocket API contract |
| `docs/06-testing.md` | test pyramid, backtesting, paper trading, live gate |
| `docs/07-hardening.md` | resilience, circuit breakers, sharding, load testing |
| `docs/08-operations.md` | logging, error envelopes, monitoring endpoints, agent metrics, security posture (review phases 5–12) |

## Roadmap (each phase ends with review + tests green)

1. **Foundation** — package config, pyproject, docker-compose, DI container, DB models + first Alembic migration, repositories, event bus, structured logging. ✅ *(complete)*
2. **Data + Scanner** — providers (Yahoo), Data Agent, cleaning, Market Scanner, Redis rankings. ✅ *(complete)*
3. **Analysis agents** — Technical, News (LLM), Fundamental + their signal tables. ✅ *(complete)*
4. **Prediction + Chief** — FeatureStore, model training/inference, ensemble decision engine. ✅ *(complete)*
5. **Risk + Portfolio + Execution** — RiskCalculator, allocation policies, broker adapters (Paper/Alpaca), Memory System wiring. ✅ *(complete)*
6. **Backtesting + graduation gate** — replay engine, SystemGate, CI pipeline. ✅ *(complete)*
7. **API + Dashboard** — FastAPI routes, WebSocket hub, web UI. ✅ *(complete)*
8. **Hardening** — load test, fault injection, sharding, docs review. ✅ *(complete)*
9. **Error handling review** — typed error envelopes, 422/401/500 contract, internal-error leak containment. ✅ *(complete)*
10. **Monitoring review** — `/system/logs`, `/system/metrics` event counts, worker heartbeat in `/health`. ✅ *(complete)*
11. **Dashboard metrics + backtest coverage** — `agent_metrics` writer, backtest failure path, API/router tests. ✅ *(complete)*
12. **Paper trading resilience + performance** — REJECTED-on-poll-failure, typed broker errors, SQL event counts, LLM rate limiting. ✅ *(complete)*
13. **Security audit** — constant-time auth, secrets hygiene, SQL parameterization, `pip-audit` clean. ✅ *(complete)*
14. **Docs update** — this README + `docs/08-operations.md` + API doc sync. ✅ *(complete)*

## Safety

Live trading is gated by `SystemGate` (CI green + out-of-sample backtests + paper-trading track record + explicit env enable). See `docs/06-testing.md`.
