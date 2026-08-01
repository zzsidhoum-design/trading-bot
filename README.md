# qtrader — Multi-Agent AI Trading System

Professional, scalable, event-driven trading platform. Python 3.12+, Clean Architecture, SOLID, async everywhere.

> **Status: DESIGN MILESTONE** — architecture and full design are frozen in `docs/`. Implementation proceeds phase by phase (see Roadmap). No live trading is possible until the graduation gate is satisfied (see `docs/06-testing.md`).

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
docs/            architecture · agents · database · data-flow · api · testing
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

## Roadmap (each phase ends with review + tests green)

1. **Foundation** — package config, pyproject, docker-compose, DI container, DB models + first Alembic migration, repositories, event bus, structured logging. *(in progress after design)*
2. **Data + Scanner** — providers, Data Agent, cleaning, indicator engine, Market Scanner, Redis rankings.
3. **Analysis agents** — Technical, News (LLM), Fundamental + their signal tables.
4. **Prediction + Chief** — FeatureStore, model training/inference, ensemble decision engine.
5. **Risk + Portfolio + Execution** — RiskCalculator, allocation policies, broker adapters (Paper/Alpaca), Memory System wiring.
6. **Backtesting + graduation gate** — replay engine, paper mode, SystemGate, CI pipeline.
7. **API + Dashboard** — FastAPI routes, WebSocket hub, web UI.
8. **Hardening** — load test, fault injection, sharding, docs review.

## Safety

Live trading is gated by `SystemGate` (CI green + out-of-sample backtests + paper-trading track record + explicit env enable). See `docs/06-testing.md`.
