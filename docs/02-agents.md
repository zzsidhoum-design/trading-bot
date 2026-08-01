# 02 — Agent Specifications

All agents extend `AgentBase`:

```python
class AgentBase(ABC):
    name: ClassVar[str]
    consumes: ClassVar[tuple[type[DomainEvent], ...]]
    produces: ClassVar[tuple[type[DomainEvent], ...]]

    async def run(self, ctx: AgentContext) -> None: ...   # event-driven entry
    async def on_event(self, event: DomainEvent) -> None: ...
```

- Agents never touch infrastructure directly; they receive **ports** via constructor DI.
- Every decision an agent makes is persisted (signals / predictions / decisions) → the **Memory System** and dashboard.
- Each agent is independently unit-testable with fakes and independently runnable via the CLI (`qtrader run-agent technical --symbol AAPL`).

---

## 1. Data Agent

**Responsibility:** reliable, clean market data in the DB.

- Ingests real-time quotes/bars and historical bars from one or more `MarketDataProvider` adapters.
- Runs on a schedule per interval (1m/5m/15m/1h/1d) driven by the arq scheduler; backfills gaps detected by comparing expected vs. stored bars.
- **Cleans** data: dedup, out-of-range/zero/negative prices, stale-bar rejection (lateness vs. expected ts), volume spikes sanity, OHLC consistency (H ≥ max(O,C,L)), timestamp normalization to UTC.
- Persists via `PriceRepository` (bulk upsert). Publishes `PriceUpdated` / `BackfillCompleted`.
- **Ports:** `MarketDataProvider`, `PriceRepository`, `Cache` (write-through latest quote).
- **Config:** intervals, universes (watchlist), provider priority order, staleness thresholds, retry policy.

---

## 2. Market Scanner Agent

**Responsibility:** find the most tradeable candidates from the whole universe.

- Continuously scans stored/cached prices for all symbols; computes per-symbol metrics: liquidity proxy (dollar volume), volatility (ATR%), momentum (N-day change), spread/range.
- Ranks candidates and keeps a **Redis sorted set** (`scan:top:<rank_type>`) of top-K symbols — cheap to query for the dashboard and Chief Agent.
- Excludes: illiquid (dollar-volume floor), halted, pre/post-market noise unless configured, already-open positions when relevant.
- Publishes `ScanCompleted` with the candidate list. This is the *gate* that limits heavy analysis to a small set (cheap filter before expensive analysis).
- **Ports:** `PriceRepository`, `IndicatorRepository` (read), `Cache` (rankings), `StockRepository`.
- **Config:** top-K, liquidity/volatility thresholds, refresh cadence.

---

## 3. Technical Analysis Agent

**Responsibility:** compute indicators + generate *preliminary* signals.

- Indicator engine (`TechnicalIndicators` service, pure functions over price frames — pandas but free of I/O): **RSI, EMA, SMA, MACD, ATR, VWAP, Bollinger Bands, ADX, Stochastic, Ichimoku, Volume Profile**. One class per indicator (OCP), vectorized over the frame.
- Produces one composite `TechnicalSignal` per symbol/interval:
  - trend (up/down/sideways) from EMA/SMA alignment + ADX strength,
  - momentum from RSI + MACD + Stochastic,
  - volatility regime from ATR%/BB width,
  - a weighted **score in [-1, 1]** and a label (STRONG_BUY..STRONG_SELL).
- Persists indicator rows (`indicators` table) and `signals` rows. Publishes `TechnicalSignalGenerated`.
- **Ports:** `IndicatorRepository`, `SignalRepository`.
- **Config:** indicator parameters, weights, timeframe(s).

---

## 4. News Agent

**Responsibility:** turn unstructured text into scored, structured market signal via an LLM.

- Collects candidate articles/events: company news, **earnings releases**, **economic data**, **central-bank statements** — from `NewsProvider` adapters (RSS, NewsAPI, earnings calendar).
- Per item, calls the `LLMClient` with a strict prompt producing **JSON**: `sentiment_score [-1..1]`, `summary`, `expected_market_impact` (LOW/MEDIUM/HIGH + direction), `relevant_symbols`, `categories`, `confidence`.
- LLM output is **schema-validated** (Pydantic); failures/out-of-schema output are dropped and logged (never crash the pipeline).
- Aggregates per-symbol/news-window into `NewsSignal` (signed impact score weighted by recency & confidence). Persists `news` rows + aggregated `signals`.
- Publishes `NewsSignalGenerated`.
- **Ports:** `NewsProvider`, `LLMClient`, `NewsRepository`, `SignalRepository`.
- **Config:** LLM model, prompt templates, aggregation window, per-source weights, rate limits.
- **Provider-agnostic:** OpenAI / Anthropic / local models are all `LLMClient` implementations; no vendor code leaks into the agent.

---

## 5. Fundamental Analysis Agent

**Responsibility:** valuation & financial-health score.

- Pulls financial statements / valuation metrics (revenue, EPS, P/E, debt, cash flow, ROE, ROA, gross/operating/net margins, YoY growth) from `FundamentalProvider` adapters (or DB for offline).
- Computes a normalized composite score per sector (percentile ranks), penalizing leverage/negative cash flow, rewarding growth & margins.
- Output: `FundamentalSignal` with sub-scores and overall rating. Persists `fundamentals` + `signals`. Publishes `FundamentalSignalGenerated`.
- **Ports:** `FundamentalProvider`, `FundamentalRepository`, `SignalRepository`.
- **Config:** sector peer-group mapping, scoring weights, staleness threshold (skip if fundamentals older than N months).

---

## 6. Prediction Agent

**Responsibility:** probability-of-movement using a trained ML model.

- Feature pipeline: windowed technical features + volatility + volume profile + (optional) sentiment/fundamental score buckets → deterministic feature vector with **feature-hash** stored for provenance.
- Model is loaded from the `ModelRegistry` (only active version; falls back to heuristic if absent).
- Inference returns `Predictions`: `prob_up`, `prob_down`, `prob_trend_continuation`, `confidence` (calibrated, e.g., Platt scaling / conformal), `expected_return`, `expected_volatility`.
- `ModelTrainer` (a service, run nightly): trains on labeled history (forward N-bar return), stores version + offline metrics in `model_registry`; never auto-promotes without threshold pass (see Testing doc).
- Persists `predictions`. Publishes `PredictionGenerated`.
- **Ports:** `FeatureStore`, `ModelRepository`, `PredictionRepository`.
- **Config:** horizon, feature set, min-confidence to count as a vote.

---

## 7. Risk Manager Agent

**Responsibility:** enforce risk rules; no trade passes without approval.

- Consumes decisions; computes via `RiskCalculator` service:
  - **position size** from risk-per-trade % of equity and stop distance (and volatility sizing, e.g., ATR multiple),
  - **stop loss / take profit** levels (ATR-based, configurable R-multiples),
  - checks **max daily loss** (running), **max portfolio exposure**, **max positions**, **per-sector concentration**, **correlation clamp**, **max position as % of ADV** (liquidity check).
- Emits `RiskApproved(order_plan)` or `RiskRejected(reasons)`. Rejections are logged to `risk_history` and surfaced on the dashboard.
- Anti-churn: minimum cooldown between trades on same symbol; max trades/day.
- **Ports:** `RiskRepository`, `PortfolioRepository`, `PositionRepository`, `PriceRepository` (ADV), `Cache` (locks).
- **Config:** all limits are config values (RiskPolicy), validated at startup; rejecting a config that violates invariants.

---

## 8. Portfolio Agent

**Responsibility:** capital allocation & portfolio-level control.

- Applies allocation policy (equal-weight, risk-parity, momentum-weighted, target-beta) over approved candidates.
- Enforces max number of concurrent positions; frees capital by closing/sizing-down the weakest position when needed.
- Performs **rebalancing** jobs: drift-based or scheduled; only proposes trades through the normal Risk → Execution flow.
- Computes performance attribution (per stock, per strategy, per agent) and updates `strategy_performance` / `agent_metrics`.
- Publishes `AllocationProposal` / `PortfolioRebalanced`.
- **Ports:** `PortfolioRepository`, `PositionRepository`, `AllocationPolicy` (pluggable strategies), `MetricsCalculator`.
- **Config:** allocation policy, target weights, rebalance schedule, position caps.

---

## 9. Execution Agent

**Responsibility:** talk to the broker and keep state consistent.

- Interface to `BrokerGateway` — one contract, many implementations: `PaperBroker`, `AlpacaBroker`, `IBKRBroker`, `BacktestBroker`.
- Submit orders (market/limit/stop), cancel/replace, adjust SL/TP (`modify_position_brackets`), poll fills.
- Distinguishes broker fill events from local state; idempotent via order idempotency-key + Redis lock (never double-submit on retry).
- Persists `orders`, updates `positions` (open/close), writes fills into `trades` via the Memory System.
- Publishes `OrderSubmitted`, `OrderFilled`, `OrderRejected`, `PositionClosed`.
- **Modes:** gated by `SystemGate`; `BacktestBroker` replays historical fills through the same code path (no special-casing in the agent).
- **Ports:** `BrokerGateway`, `OrderRepository`, `PositionRepository`, `TradeRepository`, `Cache` (locks).
- **Config:** broker choice per mode, order defaults, fill-poll cadence, commission model.

---

## 10. Chief Agent

**Responsibility:** the orchestrator that produces the final **BUY / SELL / HOLD** with an explanation.

- Collects the latest signals: technical, news, fundamental + prediction for each candidate symbol.
- Runs a **weighted ensemble / rule engine** (configurable decision strategy) that fuses the four evidence streams into a final `Decision`.
- Every decision is fully explainable: `Decision.rationale` lists each contributing signal with its score, weight and reason (human-readable, saved to `decision_log`).
- Applies a *threshold* (min combined confidence) — no signal → HOLD; conflicting strong signals → HOLD with explanation of the conflict.
- If decision ≠ HOLD → emits `DecisionMade` → Risk Manager gate → Portfolio → Execution.
- Publishes `DecisionMade`. Persists `decision_log` + combined `signals`.
- **Ports:** `SignalRepository`, `PredictionRepository`, `DecisionRepository`, `DecisionStrategy` (pluggable), `Cache`.
- **Config:** decision strategy, weights per signal source, thresholds, HOLD discipline.

---

## Agent Registry & Reuse

- `AgentRegistry` maps `AgentName → Agent class`; enables running any agent standalone (CLI), wiring them all in the pipeline, and A/B-ing agent versions.
- Agents are **stateless workers**; all state lives in the DB/Redis → horizontally scalable and restartable.
