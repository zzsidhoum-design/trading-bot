# 05 — API Design

**API host:** FastAPI app in `interfaces/api`. Serves:
1. REST API (management + dashboard reads + control) — JSON, OpenAPI at `/docs`.
2. WebSocket hub — live event streaming for the dashboard.
3. The dashboard SPA as static files.

**Auth:** every request requires `X-API-Key` header (configurable; OAuth2/PKCE for multi-user later). WebSocket authenticates via `?api_key=` during handshake (rejected otherwise).

**Common conventions:**
- All reads paginated: `?limit=&offset=` or cursor; default `limit=50`.
- Timestamps in ISO-8601 UTC.
- Errors: `{ "detail": "..." }` with proper HTTP status; domain errors map 422/409/503.
- Read endpoints go through repositories; write endpoints go through **use cases** (never direct model access from routes).

---

## 1. System & Health

| method | path | purpose |
|---|---|---|
| GET | `/api/v1/health` | liveness + dependency health (db, redis, broker) |
| GET | `/api/v1/system/status` | mode (backtest/paper/live), agents running/errors, graduation state |
| GET | `/api/v1/system/events?type=&from=&to=` | event journal (outbox) with pagination |
| POST | `/api/v1/system/mode` | toggle {backtest, paper} (live requires separate gate) |

## 2. Dashboard Reads

| method | path | purpose |
|---|---|---|
| GET | `/api/v1/dashboard/summary` | equity, P/L, daily P/L, open positions count, exposure |
| GET | `/api/v1/dashboard/equity` | equity curve series |
| GET | `/api/v1/dashboard/positions` | open positions with unrealized P/L |
| GET | `/api/v1/dashboard/agents` | per-agent metrics + last activity + errors |
| GET | `/api/v1/dashboard/allocation` | capital distribution (per sector/stock) |
| GET | `/api/v1/dashboard/top-stocks?metric=liquidity|volatility|score` | scan rankings |
| GET | `/api/v1/dashboard/trades?from=&to=` | trade history with decision reasons |
| GET | `/api/v1/dashboard/risk` | risk_history recent + current limits usage |
| GET | `/api/v1/dashboard/logs?level=&component=` | system_logs |
| GET | `/api/v1/dashboard/performance?strategy=&mode=` | performance summaries |

## 3. Market Data

| method | path | purpose |
|---|---|---|
| GET | `/api/v1/stocks?q=&sector=` | search universe |
| POST | `/api/v1/stocks` | add symbol(s) to universe (admin) |
| GET | `/api/v1/stocks/{symbol}/price?interval=` | latest bar |
| GET | `/api/v1/stocks/{symbol}/history?interval=&from=&to=` | price history (charts) |
| GET | `/api/v1/stocks/{symbol}/indicators?interval=` | latest indicator snapshot |
| GET | `/api/v1/stocks/{symbol}/news` | analyzed news for symbol |
| GET | `/api/v1/stocks/{symbol}/signals` | all agents' signals for symbol |

## 4. Agent Control

| method | path | purpose |
|---|---|---|
| POST | `/api/v1/agents/{name}/run` | run one agent once (e.g., on demand scan) |
| GET | `/api/v1/agents` | list agents, config, status |
| PUT | `/api/v1/agents/{name}/config` | hot-reload a subset of agent config (validated) |
| POST | `/api/v1/agents/{name}/pause` / `resume` | lifecycle control |

## 5. Portfolio & Orders

| method | path | purpose |
|---|---|---|
| GET | `/api/v1/portfolio` | summary + positions + orders |
| GET | `/api/v1/portfolio/orders?status=` | orders with fills |
| POST | `/api/v1/portfolio/orders` | manual order request (goes through Risk gate too — same safety path) |
| DELETE | `/api/v1/portfolio/positions/{id}` | request close position (via Risk/Execution) |
| POST | `/api/v1/portfolio/rebalance` | trigger rebalance |
| GET | `/api/v1/portfolio/performance?strategy=&mode=` | strategy_performance + agent_metrics |

## 6. Backtest & ML

| method | path | purpose |
|---|---|---|
| POST | `/api/v1/backtest` | submit backtest (params: universe, strategy, dates, capital) → run id |
| GET | `/api/v1/backtest/{id}` | status + metrics + equity curve |
| POST | `/api/v1/backtest/{id}/compare` | compare two runs |
| GET | `/api/v1/models` | model_registry listing |
| POST | `/api/v1/models/train` | trigger retraining |
| POST | `/api/v1/models/{id}/promote` | promote validated model (requires graduation threshold pass) |

---

## 7. WebSocket — `/ws/live`

Broadcasts, as JSON frames, any subscribed event in real time:

```json
{"type": "DecisionMade", "data": {"symbol": "AAPL", "decision": "BUY", "confidence": 0.81,
  "rationale": "EMA cross + ADX>25 + positive earnings revision + prob_up 0.72"}, "ts": "2026-08-01T14:03:00Z"}
```

Topics are requested with `?topics=order,trade,price` and matched as substrings against
the frame `type` (e.g. `order` matches `OrderSubmitted`/`OrderFilled`). No topics means
"all events". `type` is the event kind, `data` the payload, `ts` the UTC timestamp.

---

## 8. Why FastAPI

- Async-native (fits the whole stack), Pydantic validation shared with domain DTOs (one schema language), auto OpenAPI docs for free, mature WebSocket support, high throughput for the dashboard fan-out, and it can be scaled behind a proxy horizontally.
