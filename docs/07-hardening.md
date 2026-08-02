# 07 — Hardening: Resilience & Sharding (Phase 8)

Phase 8 turns the Phase 1–7 foundation into a system that degrades gracefully when
external services fail and scales horizontally across workers. It adds resilience
primitives (circuit breaker, rate limiter, retries), wires them into every external
adapter, shards the worker universe by symbol, and backs it all with fault-injection
tests + a load-test harness.

## 1. Resilience Primitives

Lives in `src/qtrader/infrastructure/resilience/`:

- **`CircuitBreaker`** — classic closed/open/half-open state machine.
  - *closed:* calls pass through; `failure_threshold` consecutive failures trip it open.
  - *open:* calls fail fast (`CircuitOpenError`) for `reset_timeout_seconds`.
  - *half-open:* first probe is allowed after the timeout; success closes it, failure reopens.
  - `consecutive_failures` is reset by any success while closed.
  - `CircuitBreakerRegistry` keeps one breaker per external service and exposes
    `CircuitBreakerSnapshot` (`name`, `state`, `consecutive_failures`, `reset_timeout_seconds`)
    for observability.
- **`TokenBucket`** — thread/async-safe rate limiter (`acquire()` / `acquire_nowait()`),
  the primitive for provider rate-limit budgets (per-provider `rpm` config).
- **`retry_async`** — thin wrapper over `tenacity` with:
  - exponential backoff + jitter,
  - `attempts=` (the tenacity *attempts* stop, not `stop_after_attempt`),
  - **transient-only retry policy**: network errors, 5xx, and HTTP 429 are retried;
    4xx are never retried. `is_transient()` / `retry_if_transient()` are exported.

### Where each primitive is wired

| adapter | retry | circuit breaker |
|---|---|---|
| `YahooFinanceProvider` (chart requests) | `_request_chart` | yes — `failure_threshold`/`reset_timeout` from settings; open breaker raises `RuntimeError("yahoo circuit open …")` |
| `OpenAILLMClient` (`_post`) | yes | — |
| `AlpacaBroker` (submit/cancel/modify/status) | yes | — |

`DataAgent.backfill` catches the provider's `RuntimeError` (open circuit) and returns
`0` bars instead of crashing the ingest cycle — degraded mode, not failure.

## 2. Circuit-Breaker Configuration

New `Settings`:

```toml
provider_failure_threshold = 5        # consecutive failures before breaker opens
provider_reset_timeout_seconds = 30.0 # how long it stays open before probing
```

Configured in the DI container (`Container.circuit_breakers()`), which returns a
`CircuitBreakerRegistry` shared by every consumer. Exposed read-only at:

```
GET /api/v1/system/resilience   →   list[CircuitBreakerSnapshot]
```

## 3. Worker Sharding

`src/qtrader/application/services/shard.py`:

- `shard_for(symbol, num_shards)` — deterministic MD5-hash mod sharding (first 8 bytes,
  big-endian), so the same symbol always lands on the same shard.
- `shard_for_cached` — memoized, since cycles call it per-symbol repeatedly.
- `owned_symbols(symbols, shard_id, num_shards)` — filters a universe down to the
  current worker's slice. Returns **everything** when `num_shards <= 1`
  (the default — single worker behaves exactly as before).

Two new settings control it:

```toml
worker_shards  = 1   # total number of workers (used to size the shard space)
worker_shard_id = 0  # this worker's slice, validated to be in [0, worker_shards)
```

Every periodic task filters its universe through `_owned(...)` before working:
`backfill`, `technical_cycle`, `prediction_cycle`, `chief_cycle`, and `risk_cycle`
(the last via the candidates owned by this worker). Scanner + ranking are already
shared through the Redis sorted set, so rankings remain global while compute is
split across workers.

## 4. Fault-Injection Tests

- `tests/unit/test_resilience.py` — circuit breaker state machine (open/half-open
  probe/recovery), registry snapshots, token bucket (burst + refill + no-overdraft),
  and retry behavior (transient retried, 4xx not retried, attempts exhausted).
- `tests/unit/test_yahoo_resilience.py` — provider-level: transient 5xx retries then
  succeeds; persistent failures trip the breaker; an open breaker short-circuits
  without hitting the transport; recovery after `reset_timeout`.
- `tests/integration/test_hardening.py` — against the real stack: a `FailingBroker`
  whose `submit_order` always raises `BrokerUnavailable`; the order is rejected
  gracefully (a `REJECTED` `OrderStatusChanged` event recorded, not a crash), and
  the container's breaker registry is reachable and reports snapshots.
- `tests/unit/test_ws_fanout.py` — 8 concurrent WS clients all receive a broadcast;
  topic-filtered clients are isolated under load.
- `tests/unit/test_shard.py` — `shard_for` determinism, `owned_symbols` correctness
  and balance across shards, and single-worker passthrough.

## 5. Load-Test Harness

`scripts/load_test.py` — asyncio + httpx, no extra deps:

```bash
python scripts/load_test.py --concurrency 50 --duration 10 \
    --path /api/v1/health --api-key <key>
```

Reports total requests, requests/sec, avg/p50/p95/max latency, and error count.
Target a 200 endpoint (e.g. `/api/v1/health`); read endpoints that 404 for an empty
DB are still correctly counted as errors.
