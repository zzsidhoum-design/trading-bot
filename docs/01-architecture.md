# 01 — System Architecture

**Project:** qtrader · Multi-Agent AI Trading System
**Language:** Python 3.12+ (asyncio)
**Version:** 0.1 (design milestone)

---

## 1. Goals & Non-Functional Requirements

| Requirement | Design decision |
|---|---|
| Monitor thousands of stocks in real time | Async I/O throughout, event-driven ingestion, per-interval batching, DB partitioning |
| Multi-source data | Provider port + adapter pattern; every source is a pluggable adapter |
| Extensible (new agent / indicator / model / broker) | Plugin registry + ABC ports (OCP); adding a feature never modifies existing code |
| Testable | Clean Architecture: domain/application depend only on interfaces; fakes injected in tests |
| Safe (no live trading until proven) | `SystemGate` — trading mode is a state machine: backtest → paper → live, with graduation criteria |
| Observable | Structured logs (structlog), event outbox, agent metrics table, OpenTelemetry |
| Secrets safe | All credentials via env / Docker secrets, never in code or git |

---

## 2. High-Level Logical View

```
┌────────────────────────────────────────────────────────────────────────┐
│                            DELIVERY LAYER                              │
│   FastAPI (REST + WebSocket) · CLI · Dashboard (web UI)                │
├────────────────────────────────────────────────────────────────────────┤
│                        APPLICATION LAYER                               │
│   Agents (orchestrators) · Use Cases · Services · DTOs                 │
│   MemorySystem · SystemGate · EventBus (in-process + Redis fan-out)    │
├────────────────────────────────────────────────────────────────────────┤
│                       DOMAIN LAYER (no dependencies)                   │
│   Entities · Value Objects · Enums · Domain Events · Ports (ABCs)      │
├────────────────────────────────────────────────────────────────────────┤
│                      INFRASTRUCTURE LAYER (adapters)                   │
│   Postgres (SQLAlchemy async) · Redis · Brokers · Data Providers       │
│   News feeds · LLM clients · ML models · Schedulers                    │
└────────────────────────────────────────────────────────────────────────┘
```

**Dependency rule:** dependencies point *inward*. Domain knows nothing about SQLAlchemy, Redis, FastAPI, or HTTP. Application knows interfaces (ports). Infrastructure implements them. Interfaces only ever talk to application via use cases.

---

## 3. Module Map (src/qtrader)

```
qtrader/
├── config/            Pydantic settings (env-driven), composition root + DI container
├── domain/
│   ├── entities/      Business entities (Stock, Order, Position, Signal, Prediction...)
│   ├── value_objects/ Immutable primitives (Money, PriceBar, Percentage, RiskParams)
│   ├── events/        Typed domain events (PriceUpdated, DecisionMade, OrderFilled...)
│   └── ports/         ABCs / Protocols: MarketDataProvider, BrokerGateway, NewsProvider,
│                      LLMClient, ModelRepository, EventBus, Cache, UnitOfWork
├── application/
│   ├── agents/        The 10 agents (orchestration + decision logic), AgentBase
│   ├── services/      MemorySystem, SystemGate, RiskCalculator, PortfolioAllocator,
│                      TechnicalIndicators, SentimentScorer, ModelTrainer
│   ├── use_cases/     Request/response handlers used by interfaces (API/CLI)
│   └── dto/           Pydantic models for inputs/outputs across layers
├── infrastructure/
│   ├── database/      SQLAlchemy models, repositories, session factory, migrations (Alembic)
│   ├── brokers/       PaperBroker, AlpacaBroker, IBKRBroker, BacktestBroker (all: BrokerGateway)
│   ├── data_providers/ YahooProvider, PolygonProvider, StaticFeedProvider (test/sim)
│   ├── news/          NewsApiProvider, RSSProvider, EarningsCalendarProvider
│   ├── llm/           OpenAIProvider, AnthropicProvider, LocalProvider (vLLM/Ollama)  → LLMClient
│   ├── ml/            FeatureStore, ModelRegistry, XGBoost/LightGBM/Sklearn wrappers → ModelRepository
│   ├── cache/         RedisCache, RedisLock, RedisRateLimiter, PubSubTransport
│   ├── eventbus/      InProcessBus, RedisBus, OutboxWriter
│   └── schedulers/    Arq workers (market data cadence, news polling, model retraining)
└── interfaces/
    ├── api/           FastAPI app, route modules, WebSocket hub
    ├── cli/           typer commands (run-agent, backtest, trade, graduate)
    └── dashboard/     Static SPA served by FastAPI (charts via ECharts/Plotly)
```

---

## 4. SOLID Mapping (explicit)

| Principle | How it is honored |
|---|---|
| **S**ingle Responsibility | One agent = one responsibility. One repository per aggregate. Indicators are one class per indicator. |
| **O**pen/Closed | New indicator, broker, data source, LLM, model, or agent = add a new class + register it. No existing code modified (registry pattern). |
| **L**iskov Substitution | `BrokerGateway` is interchangeable: Paper / Alpaca / IBKR / Backtest all satisfy the same contract; the Execution Agent cannot tell them apart. Same for data providers. |
| **I**nterface Segregation | Small focused ports: `MarketDataProvider`, `NewsProvider`, `LLMClient`, `BrokerGateway`, `ModelRepository`. No god-interfaces; adapters implement only what they support (e.g., a provider that has no news does not implement `NewsProvider`). |
| **D**ependency Inversion | Domain defines ports; infrastructure implements; application consumes ports. FastAPI/CLI/Dashboard all depend on use cases, never on infrastructure. |

---

## 5. Dependency Injection

- **Composition root:** `qtrader/config/container.py` builds the full object graph at startup using `punq` (lightweight, explicit) or plain manual wiring in a single module.
- All agents/services receive their dependencies via constructor (never service-location or globals).
- Tests build a *separate* composition root where real adapters are replaced with fakes (in-memory repo, fake broker, canned news, stub LLM returning scripted sentiment).
- Configuration is `pydantic-settings` (`.env` + environment + Docker secrets). Nothing is hard-coded.

---

## 6. Async, Events & Coordination

- Everything is `asyncio`. DB via SQLAlchemy async + `asyncpg`; Redis via `redis.asyncio`.
- **Event-driven core:** agents do not call each other directly. They publish and subscribe to typed domain events on an `EventBus`.
  - In-process transport for single-process dev/tests.
  - Redis Pub/Sub transport for horizontal scaling (multiple workers).
  - Outbox: every domain event is also persisted to the `events` table → audit trail + replay + crash recovery.
- **Scheduling:** `arq` workers for periodic jobs (price polling per interval, news polling, fundamentals refresh, nightly model retraining, backtest reports).
- **Idempotency:** events carry `event_id`; consumers dedupe. Order submission guarded by Redis distributed lock (per portfolio+symbol).
- **Backpressure:** bounded in-process queues; slow consumers drop noisy events (`PriceUpdated` aggregated into a per-minute tick batch) rather than blocking the ingest path.
- **Retries & circuit breakers:** `tenacity` with exponential backoff + jitter on external HTTP calls; circuit breaker per external provider to fail fast instead of hammering a dead API.

---

## 7. Error Handling

- Domain raises typed exceptions (`DomainError`, `RiskRejected`, `InsufficientLiquidity`, `BrokerUnavailable`).
- Application catches, logs structured (structlog, includes `event_id`, `stock_id`, `agent`), and emits error events.
- Infrastructure translates provider-specific exceptions into domain ones. External provider degradation → degraded-mode flag → Chief Agent excludes that signal source.

---

## 8. Observability

- **Structured logs:** structlog → JSON lines → Docker log driver (or Loki).
- **Metrics:** Prometheus (counter: events processed, orders submitted/filled/rejected; histograms: pipeline latency, LLM call latency, indicator compute time; gauges: exposure, cash).
- **Traces:** OpenTelemetry spans for the full decision pipeline (Data → Scan → Analyze → Predict → Decide → Risk → Execute).
- **Events table** doubles as the immutable system journal surfaced in the dashboard timeline.

---

## 9. Security

- No secrets in code. `BROKER_API_KEY` etc. only via environment / `.env` (git-ignored) / Docker secrets.
- API requires API-key auth (header `X-API-Key`); WebSocket subscribes after handshake auth.
- Live trading is a separate, explicitly-enabled mode with its own env gate (`QTRADER_MODE=live` + `ENABLE_LIVE_TRADING=true`) and is rejected by the app if graduation criteria have not been recorded.
- Minimal broker API scopes; execution gateway never exposes account credentials to other layers.

---

## 10. Scaling Path

1. **Single process (dev/test):** in-process event bus, local Postgres/Redis via Docker Compose.
2. **Distributed:** multiple `arq` workers, Redis Pub/Sub bus, partitioned Postgres (`prices` partitioned by month), read replicas for dashboard queries.
3. **Many universes:** the worker pool is sharded by symbol hash; the scanner runs per-shard; results merged in Redis sorted set (top movers).
