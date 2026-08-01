# 04 — Data Flow Between Agents

Agents communicate **only through typed domain events** on the `EventBus`. No agent calls another agent's method. This decouples the pipeline (agents can be added/removed/reordered), enables independent testing, and gives us a persisted audit trail (outbox `events` table).

---

## 1. The Main Loop

```
┌────────────┐   PriceUpdated    ┌───────────────┐   ScanCompleted   ┌─────────────────────┐
│ Data Agent │ ────────────────► │ MarketScanner │ ───────────────► │ ChiefAgent           │
└────────────┘  (per interval)   └───────────────┘  (top-K ranked)  │  fan-out to analysts │
                                                                    └──────────┬──────────┘
                                          ┌───────────────────────────────────────┼───────────┐
                                          ▼                                       ▼           ▼
                                 ┌──────────────┐                      ┌───────────────┐ ┌───────────────┐
                                 │TechnicalAgent│                      │  NewsAgent    │ │FundamentalAgent│
                                 └──────┬───────┘                      └──────┬────────┘ └──────┬────────┘
                                        └───────────────┬──────────────────────┴──────────────────┘
                                                        ▼
                                          ┌─────────────────────┐    PredictionGenerated
                                          │   PredictionAgent    │ ────────────────────────┐
                                          └─────────────────────┘                          ▼
                                                        SignalGenerated events      ┌──────────────┐
                                                               ┌──────────────────►│  ChiefAgent  │
                                                               │                    └──────┬───────┘
                                                               │                       DecisionMade
                                                               ▼                           ▼
                                                        (persist signals)         ┌──────────────┐
                                                                                  │ RiskManager  │
                                                                                  └──────┬───────┘
                                                                           RiskApproved / RiskRejected
                                                                                       ▼
                                                                              ┌────────────────┐
                                                                              │ PortfolioAgent │  AllocationProposal
                                                                              └───────┬────────┘
                                                                                      ▼
                                                                              ┌────────────────┐
                                                                              │ ExecutionAgent │ ───► BrokerGateway (mode-gated)
                                                                              └───────┬────────┘
                                                                                      ▼
                                                                           OrderFilled / PositionClosed
                                                                                      ▼
                                                                             ┌────────────────┐
                                                                             │   MemorySystem │  → trades, agent_metrics,
                                                                             │                │    strategy_performance, dashboard
                                                                             └────────────────┘
```

**Pipeline phases:**

| phase | trigger | agents | produces |
|---|---|---|---|
| **Ingest** | arq schedule / websocket feed | Data Agent | `PriceUpdated`, `BackfillCompleted` |
| **Scan** | PriceUpdated batch | Market Scanner | `ScanCompleted` (top-K) |
| **Analyze** | ScanCompleted (parallel, one task per candidate) | Technical, News, Fundamental | `TechnicalSignalGenerated`, `NewsSignalGenerated`, `FundamentalSignalGenerated` |
| **Predict** | any analysis signal, cheap ML inference | Prediction Agent | `PredictionGenerated` |
| **Decide** | all four evidence sources for a symbol ready | Chief Agent | `DecisionMade` (BUY/SELL/HOLD + rationale) |
| **Risk-gate** | DecisionMade (decision ≠ HOLD) | Risk Manager | `RiskApproved(order_plan)` / `RiskRejected` |
| **Allocate** | RiskApproved | Portfolio Agent | `AllocationProposal` |
| **Execute** | AllocationProposal | Execution Agent | `OrderSubmitted/Filled/Rejected`, `PositionClosed` |
| **Learn** | OrderFilled/PositionClosed | Memory System (service) | trade records, metrics updates, model retraining trigger |

---

## 2. Event Catalog

| event | payload (core fields) | produced by | consumed by |
|---|---|---|---|
| `PriceUpdated` | symbol, interval, ts, ohlcv | Data Agent | Scanner, (dashboard ws) |
| `BackfillCompleted` | symbol, interval, range | Data Agent | Scanner, Metrics |
| `ScanCompleted` | candidates[{symbol, score, liquidity, volatility}], ts | Scanner | Chief Agent |
| `TechnicalSignalGenerated` | symbol, interval, score, signal_type, sub_scores | Technical Agent | Prediction, Chief, Dashboard |
| `NewsSignalGenerated` | symbol, score, sources[], impact, window | News Agent | Prediction, Chief, Dashboard |
| `FundamentalSignalGenerated` | symbol, score, rating, sub_scores, as_of | Fundamental Agent | Prediction, Chief, Dashboard |
| `PredictionGenerated` | symbol, prob_up/down/trend, confidence, exp_return, model, version | Prediction Agent | Chief, Dashboard |
| `DecisionMade` | decision_uuid, symbol, decision, confidence, rationale, agent_scores | Chief Agent | Risk Manager, Memory, Dashboard |
| `RiskApproved` | decision_uuid, order_plan{size, sl, tp, exposure} | Risk Manager | Portfolio Agent, Memory, Dashboard |
| `RiskRejected` | decision_uuid, symbol, reasons[] | Risk Manager | Memory, Dashboard |
| `AllocationProposal` | decision_uuid, final_size, weight | Portfolio Agent | Execution Agent, Memory |
| `OrderSubmitted` | order_id, idempotency_key, symbol, side, qty, price, mode | Execution Agent | Memory, Dashboard |
| `OrderFilled` | order_id, broker_order_id, fill_price, fill_qty, fees | Execution Agent | Memory, Positions, Dashboard |
| `OrderRejected` | order_id, reason | Execution Agent | Memory, Dashboard |
| `PositionClosed` | position_id, pnl, pnl_pct, reason | Execution Agent | Memory, Portfolio, Dashboard |
| `AgentError` | agent, error, context | any | Watchdog, Dashboard |

---

## 3. Decision-to-Execution Sequence (happy path)

```
Chief          RiskMgr        Portfolio      Execution      Broker
  │ DecisionMade │              │              │             │
  │─────────────►│              │              │             │
  │              │ compute size │              │             │
  │              │ SL/TP/limits │              │             │
  │              │ check daily  │              │             │
  │              │ loss/exposure│              │             │
  │              │──────────────│──────────────│─────────────│
  │              │ RiskApproved │              │             │
  │              │─────────────►│              │             │
  │              │              │ allocation   │             │
  │              │              │─────────────►│             │
  │              │              │              │ submit      │
  │              │              │              │────────────►│
  │              │              │              │◄────────────│ ack/fill
  │              │              │              │ persist     │
  │              │              │              │─────────────│ Memory + ws broadcast
  │              │              │              │             │
```

Every arrow is an event; every event is persisted in `events` (outbox) so the whole flow is replayable and auditable.

---

## 4. Ordering, Dedup & Consistency

- **Per-symbol ordering:** analysis consumers key on `(symbol, interval)`; Chief Agent aggregates only evidence with `created_at` within its decision window → no stale-signal mixing.
- **Idempotency:** `OrderSubmitted` carries `idempotency_key` (UUID) — Execution Agent never double-submits after retries (Redis lock per portfolio+symbol + unique constraint).
- **At-least-once delivery:** consumers are idempotent; the outbox tracks `processed_at`; a crashed consumer re-processes safely.
- **Backpressure:** `PriceUpdated` is coalesced into per-minute batches by the Scanner; LLM/News jobs run in a bounded worker pool; the Chief only fan-outs to the top-K candidates each cycle to bound cost.

---

## 5. The Memory/Learning Loop

```
OrderFilled / PositionClosed
        │
        ▼
┌───────────────────────────────┐
│ MemorySystem                  │
│ 1. write trade record (reason │
│    snapshot = decision_log +  │
│    all agent_scores)          │
│ 2. after outcome known (close │
│    or N days):                 │
│    - update agent_metrics     │
│      (hit = signal direction  │
│       matched outcome)        │
│    - update strategy_performance
│    - publish TradeClosed      │
│ 3. flag stale model for retrain│
└───────────────────────────────┘
        │
        ▼
Daily (arq): ModelTrainer trains on labeled history,
             validates, writes model_registry; promoted
             only if offline metrics beat thresholds.
```

The Memory System is a **service**, not an agent — it's the read/write layer that records decisions, outcomes, per-agent accuracy, and per-strategy performance, and feeds the dashboard and model retraining.

---

## 6. Failure & Degradation

- Provider outage → Data Agent retries (backoff), emits `AgentError`; scanner uses last-known prices with staleness flag; Chief Agent logs **data-quality risk** and refuses decisions on stale data.
- LLM outage → News Agent marks items unanalyzed; NewsSignal excluded from the ensemble (weight re-normalized) — decisions still proceed on other evidence.
- Broker outage → Execution Agent circuit-breaks, orders stay PENDING, retried with idempotency; never silently dropped.
