# 08 — Operations: Logging, Monitoring, Security (Review Phases 5–12)

This document covers the production-readiness review work delivered after the
Phase 1–8 foundation: structured logging, typed error envelopes, monitoring
endpoints, dashboard metrics, paper-trading resilience, performance hot spots,
and the security audit.

## 1. Structured Logging

`src/qtrader/config/logging.py` — structlog everywhere, JSON on stdout when
`QTRADER_JSON_LOGS=1` (default for the docker image), pretty console in dev.

- `configure_logging(settings)` — one call at each process entry point
  (api `app.py`, worker `on_startup`, CLI). Safe to call more than once.
- `get_logger("qtrader.<component>")` — context-bound loggers.
- **`_json_default`** — the JSON renderer never crashes on non-primitive values:
  `Money`/`Decimal` render as strings (e.g. `"1000.000000"`), datetimes as ISO
  8601, enums by value, everything else via `str()`.
- `LoggingMiddleware` — per-request `correlation_id` + `duration_ms`, bound to
  the request contextvars and cleared afterwards.
- Worker jobs bind `job`, `job_id` and `correlation_id=job:<id>` in
  `on_job_start` and clear them in `on_job_end` (arq reuses one process for
  every job — without clearing, context from one job leaks into the next).

Logs never contain secrets: structured fields are limited to paths, methods,
symbols, run ids, and error messages.

## 2. Error Envelope Contract

Every API error response has the shape `{"error": "<code>", "detail": ...}`:

| code | HTTP | source |
|---|---|---|
| `validation_error` | 422 | pydantic or domain `ValidationError` |
| `http_error` | per status | Starlette (auth, routing) |
| `not_found` / `no_price_data` | 404 | `NotFoundError` family |
| `conflict` | 409 | `ConflictError` |
| `order_rejected` | 422 | risk gate rejection (reason list) |
| `external_service` | 503 | upstream provider failure |
| `internal_error` | 500 | catch-all — detail is always `"internal server error"`, the real traceback goes to the `qtrader.http` logger only |

Handlers live in `interfaces/api/app.py`; the catch-all guarantees no internal
details are ever leaked to API clients.

## 3. Monitoring Endpoints

| endpoint | fields / purpose |
|---|---|
| `GET /api/v1/health` | `database`, `cache`, `worker`, `mode` — each `"ok"`/`"down"` |
| `GET /api/v1/system/metrics` | process snapshot: `uptime_seconds`, `database`, `cache`, `worker`, `events_by_type` (counts of the most recent 1000 outbox events, grouped by type), `circuit_breakers` |
| `GET /api/v1/system/logs?level=&component=&limit=` | recent audit/journal rows (`system_logs`) — gate decisions, backtest runs |
| `GET /api/v1/system/resilience` | circuit-breaker snapshots per external service |
| `GET /api/v1/system/events?type=&from=&to=&limit=` | the event journal (outbox) |

**Worker heartbeat:** the scheduler writes `worker:heartbeat` (TTL 300s) every
second. `/health` and `/system/metrics` report `worker: "ok"` while that key
exists — a dead worker shows `"down"` within 5 minutes. `Container.worker_healthy()`
is the single implementation.

**`events_by_type` is cheap:** `EventRepository.count_by_type()` runs one SQL
`GROUP BY` over the most-recent-N window; the metrics endpoint never loads
event rows.

## 4. Dashboard Agent Metrics

The `agent_metrics` table is written by the worker cycles (best-effort — a
failed write logs `agent_metric.record_failed` and never fails the job):

| cycle | metrics |
|---|---|
| `scan_cycle` | `scanner/candidates` |
| `train_cycle` | `trainer/accuracy`, `trainer/promoted` |
| `backtest_cycle` | `backtester/total_return` |

Read via `GET /api/v1/dashboard/agents`. The write path is
`AgentMetricRepository.record` (implemented by the dashboard repository,
registered under both ports in the container).

## 5. WebSocket Hub

`/ws/live?api_key=...` — fan-out of domain events as JSON frames
(`{"type", "data", "uuid", "ts"}`):

- `?topics=order,trade,price` — substring filter on the event type.
- `?since=<event_uuid>` — replay the journal from that point before live
  streaming resumes.
- Auth is constant-time (`secrets.compare_digest`) and the `change-me` default
  always closes the socket with code 4401.

## 6. Resilience & Paper Trading

- `PaperBroker.get_order_status` raises a typed `NotFoundError` for unknown or
  cancelled broker order ids (never a raw `KeyError`).
- `ExecutionAgent.execute_order` treats a failed status poll like a broker
  rejection: the order is persisted as `REJECTED` and an `OrderStatusChanged`
  event is published — the agent never crashes on a broker-side failure.
- `OpenAILLMClient` rate-limits every call through a `TokenBucket`
  (30 burst / 1 per second, injectable) — a protection against provider 429s.
- The circuit breaker / retry / rate-limit primitives are documented in
  `docs/07-hardening.md`; `/api/v1/system/resilience` exposes their state.

## 7. Security Posture

- **Auth**: every REST route requires `X-API-Key`; comparison is constant-time
  in both the HTTP guard (`require_api_key`) and the WebSocket handshake.
  The default key is `change-me` and is always rejected.
- **Secrets**: `.env` / `*.key` are gitignored; settings load from the
  environment only. No secret ever appears in structured logs or error bodies.
- **SQL**: all queries are parameterized (SQLAlchemy); the only raw SQL is the
  `SELECT 1` health probe.
- **Dependencies**: `pip-audit` is clean (no known vulnerabilities); run it as
  `python -m pip_audit` after any dependency bump.
- **DoS notes**: the API itself is not yet rate-limited per client; the LLM
  provider budget is enforced client-side (see §6).

## 8. Verification

```bash
ruff check src tests
python -m mypy src
python -m pytest -q                        # unit suite
$env:QTRADER_RUN_INTEGRATION=1; python -m pytest -q --cov   # + Postgres/Redis
python -m pip_audit                        # dependency audit
```
