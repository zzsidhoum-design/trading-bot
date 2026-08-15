# Phase 6 — AI Strategy Selection & Multi-Agent Integration

Date: 2026-08-15. Work request: a research-grade AI layer that *chooses which
validated strategies deserve capital* (a selector) and *integrates the
disparate agent signals* (technical / news / fundamental / pattern /
prediction) into a single decision, with a news sentiment pipeline, regime
awareness, an AI risk gate in front of the Phase 5 portfolio engine, simulated
execution, a decision ledger, a failure monitor and an ablation framework.
Built on `25-phase5-portfolio-risk.md` (Phase 5 portfolio/risk engine remains
the **final authority** on every cleared order).
**Research only — the AI layer never places a real trade: everything downstream
runs through the Phase 5 `PortfolioManager` gate and the Phase 4 simulator.**

## Executive verdict

**COMPLETE AND VERIFIED.** `src/qtrader/application/ai/` implements all 12
required outputs (see §1) and `src/qtrader/application/agents/news.py` gained a
sentiment-model seam. Pipeline:

```
signal repos (technical/news/fundamental/pattern) + prediction repo + news
  -> AgentSignalProvider.collect(symbol)      (latest signal per agent + live
       regime via MarketRegimeAgent + NewsSentimentPipeline assessment)
  -> AgentSignalSet -> WeightedEnsemble.aggregate  (score, weighted, raw)
  -> DecisionEngine.decide  (NO_TRADE / DEGRADED / ProposalVerdict.APPROVE)
       -> ProposalVerdict -> AiRiskGate  (fail-safe on degraded failure state)
            -> PortfolioManager.propose  (Phase 5 authority: sizing, caps, kills)
                 -> (research) SimulatedExecution (Phase 4 simulator)
       -> DecisionLedger records every decision (jsonl)
  -> AiFailureMonitor (dispersion / confidence saturation / news staleness /
       prediction drift) flags degraded -> AiRiskGate refuses to route
  -> StrategySelector.select  (multi-factor ranking of validated strategies)
       -> run_ablation  (8 additive cases, per-agent contribution)
```

Full suite **870 passed** (+99 AI tests), 27 skipped (pre-existing skips for
optional Phase 2/3 data/AI paths); `ruff check src tests` clean; `mypy src`
clean (183 source files). **No new dependencies** (FinBERT stays lazy/fail-safe;
lexicon is the offline deterministic fallback). Nothing trades live.

## 1. The 12 required outputs

| # | Output | Module | Entry point |
|---|--------|--------|-------------|
| 1 | Sentiment feature models + pipeline | `ai/sentiment.py` | `NewsSentimentPipeline.assess` |
| 2 | News agent sentiment-model seam | `agents/news.py` | `NewsAgent._analyze` |
| 3 | Market regime agent | `ai/regime.py` | `MarketRegimeAgent.assess` |
| 4 | Agent signal collection | `ai/signals.py` | `AgentSignalProvider.collect` |
| 5 | Weighted ensemble aggregation | `ai/signals.py` | `WeightedEnsemble.aggregate` |
| 6 | Decision engine | `ai/decision.py` | `DecisionEngine.decide` |
| 7 | AI strategy selector | `ai/selector.py` | `StrategySelector.select` |
| 8 | AI risk gate | `ai/risk_gate.py` | `AiRiskGate.evaluate` |
| 9 | Simulated execution integration | `ai/execution_integration.py` | `SimulatedExecution.run` |
| 10 | Decision ledger | `ai/records.py` | `DecisionLedger` / `build_decision_record` |
| 11 | AI failure monitor | `ai/failure.py` | `AiFailureMonitor.report` |
| 12 | Ablation framework | `ai/ablation.py` | `run_ablation` |

All shared models live in `ai/models.py` (`RegimeAssessment`, `AgentSignal`,
`AgentSignalSet`, `AgentWeightsConfig`, `SelectorConfig`, `SelectorReport`,
`StrategySelection`, `ExcludedStrategy`, `AssetContext`, `DecisionProposal`,
`RiskGateResult`, `ExecutionAssumptions`, `ExecutionOutcome`, `FailureEvent`,
`FailureReport`, `AiDecisionRecord`, `AblationCase/Metrics/Result/Report`,
`AgentContribution`, `ProposalVerdict`, `FailureSeverity`).

## 2. Sentiment feature models and news pipeline (output 1)

`sentiment.py`:

- `FinancialSentimentModel` (Protocol): `analyze(text, *, symbol) ->
  SentimentResult` (sentiment -1..1, confidence 0..1, relevance, model,
  error flags). 
- `LexiconFinancialSentimentModel` — deterministic keyword lexicon calibrated
  to -1..1; the offline fallback. Never imports transformers.
- `FinBERTModel` (`ProsusAI/finbert`) — **feature model only, lazy import**.
  When `transformers` is absent or the model cannot load, every call returns a
  flagged `SentimentResult(error=True, error_message=...)` — never fabricated
  data, never a crash. Typed with `Any` handles to avoid a hard dependency.
- `NewsSentimentPipeline(fetch, repo, model)` — `async assess(symbol, *, as_of,
  min_relevance=0.0)` fetches news from the provider, filters point-in-time
  (`published_at <= as_of`) and by relevance (default passes everything), runs
  the sentiment model per item, aggregates to a `NewsAssessment` (bias,
  `scored_count`), and **persists analyzed items** to the news repository
  (url-dedup upsert) so analysis is reusable. No items → `None` (honest).

## 3. News agent seam (output 2)

`agents/news.py` `_analyze`: when a sentiment model is configured it drives the
score — a model error **skips** the item (`news.sentiment_failed`, return
`None`) instead of fabricating; the analyzed `NewsItem` carries
`sentiment_score`, `analysis_confidence`, `expected_market_impact`,
`impact_direction`, `summary`, `analyzed_at` and metadata
`analysis_schema=<model name>` + `relevance`. The LLM branch (no sentiment
model) uses `llm.complete_json` into `NewsAnalysis` with the same outcome
fields. `analyze_symbol` publishes `NewsSignalGenerated` (score + impact).

## 4. Regime agent (output 3)

`regime.py` `MarketRegimeAgent.assess(closes)` wraps the existing causal
`MarketRegimeEngine` and adds what the engine lacks: **confidence** (trend
decisiveness 0.45..0.95; sideways 0.30..0.80 from fast/slow spread), the trend
condition, the volatility condition and the timeframe. The engine's strict
comparisons (`close > slow and fast > slow` → BULL, inverted → BEAR, else
SIDEWAYS) make a constant series reliably SIDEWAYS. `RegimeAssessment.volatility`
is `VolatilityRegime | None` — when vol history is too short the regime is
*unknown*, never fabricated; `regime.assess` then falls back to a 0.30
vol-confidence baseline. The agent is a pure classifier (never orders).

## 5. Signal collection (output 4)

`signals.py` `AgentSignalProvider(signals, predictions, regime_agent,
news_pipeline=None)`:

- `AGENT_SIGNAL_SOURCES = ("technical", "news", "fundamental", "pattern")` —
  latest persisted `Signal` per agent via `SignalRepository.latest_for_symbol`
  (missing signals are simply absent; nothing is fabricated).
- Latest `Prediction` maps to a `"prediction"` agent signal:
  `score = prob_up - prob_down`, confidence from the model, features
  `{prob_up, prob_down, expected_return, expected_volatility}`.
- Live regime via `MarketRegimeAgent` (from provided bars or the price series)
  and, optionally, a live `NewsSentimentPipeline` assessment.
- Returns an `AgentSignalSet` (`asset`, `as_of`, `signals`, `regime`, `news`,
  `closes`) with `by_agent()` grouping and `agreeing_agents()` helpers.
- `_signal_features` carries agent `sub_scores` metadata into features; every
  signal carries a human-readable `reason` (type + score + horizon).

## 6. Weighted ensemble (output 5)

`WeightedEnsemble(config)` — `aggregate(signal_set) -> (ensemble, weighted,
raw)`:

- `weighted[agent] = w * clamp(confidence, 0, 1) * score`; `raw[agent] = score`;
- `ensemble = sum(weighted) / sum(w * clamp(confidence))` — confidence-weighted
  blend; **zero enabled signals → 0.0 (neutral, do-nothing)**.
- `AgentWeightsConfig` (versioned, auditable config, never hard-coded): weights
  per agent; `enabled` restricts which agents participate
  (`effective_agents()`), empty → all configured agents.

## 7. Decision engine (output 6)

`decision.py` `DecisionEngine(ensemble, config)` — `decide(asset, signal_set,
capital) -> DecisionOutcome`:

- **Gate 1** `|ensemble| < min_ensemble_abs_score (0.15)` → `NO_TRADE`.
- `conviction = |ensemble| * (0.5 + 0.5 * regime.confidence)` — regime scales
  *how strongly we act*, never the direction (regime is context only); no
  regime → `conviction = |ensemble|`.
- **Gate 2** `conviction < min_confidence (0.0)` → `DEGRADED`.
- **Gate 3** agreeing agents `< min_agreeing_agents (1)` → `DEGRADED`
  (agreement = fraction of non-zero agents matching the ensemble sign).
- Else `APPROVE`: side from ensemble sign,
  `quantity = capital * position_size_pct (0.02) * min(1, conviction) *
  leverage (1.0) / price`, `ROUND_DOWN`, floored at 0. Volatility expected
  (prediction features or OOS expectancy/max-drawdown) is carried as context —
  it never blocks an approved trade here (that is Phase 5's job).

## 8. AI strategy selector (output 7)

`selector.py` `StrategySelector(config)` — `select(records, *, regime,
features, suspended, as_of) -> SelectorReport`. Hard exclusions (never
selectable): status outside `VALIDATED / EXECUTION_SENSITIVE / EXECUTION_ROBUST`;
`suspended` set (Phase 5 control); unverifiable regime operating conditions
(any missing feature / cross-bar operator → fail safe); walk-forward
positive-fold fraction below `min_positive_fold_fraction`; missing OOS result.

Score = weighted average of 12 bounded factors (each clipped to [0,1],
historical return alone never decides): `oos_sharpe`, `oos_return` (cagr or
total-return fallback), `oos_sortino`, `stability` (wf positive folds +
1 − std), `execution` (baseline fill/reject), `recent` (recency decay),
`volatility_match` (timeframe fit), `cross_asset` (symbols_with_profit /
symbols_tested), `risk` (`1 − _factor(max_drawdown, 0.5)`), `correlation`,
`regime` (best-regime match), `complexity` (`1 − complexity/10`).
`_factor(value, cap, scale)` coerces `Decimal -> float`; missing values return
0.5 (neutral). Emits ranked `StrategySelection` with per-factor reasons.

## 9. AI risk gate (output 8)

`risk_gate.py` `AiRiskGate(portfolio_manager)`:

- **Fail-safe**: while `AiFailureMonitor` reports `degraded`, every proposal is
  refused (`RiskGateResult` degraded) — no research decision is routed.
- Otherwise the AI proposal is translated to a Phase 5 `ProposedTrade` and
  routed through `PortfolioManager.propose(...)` — **Phase 5 remains the final
  authority** (kill switch, control statuses, drawdown, sizing, constraints,
  liquidity cap). Approved → `ClearedOrder`; rejected → the Phase 5 reason is
  surfaced unchanged. The AI layer can only request; it can never override.

## 10. Simulated execution (output 9)

`execution_integration.py` `SimulatedExecution(scenario=BASELINE,
commission_bps=10.0, max_participation_rate=0.10, seed=42)` —
`run(order, bars, *, adv_volume, adv_dollar, atr_pct) -> ExecutionOutcome`:

- Submits the cleared order to the real Phase 4 `ExecutionSimulator`
  (`MARKET`, int quantity) and processes bars sequentially under the scenario's
  slippage assumptions, per-bar participation and `TransactionCostModel`.
- Residual unfilled quantity at the final bar is exited at the close under
  friction (via `simulator.pending` + `exit_quote`, commission
  `commission_for(remaining, price)`).
- Returns a clean `ExecutionOutcome` (filled flag, fill_rate, rejected_rate,
  net_return, avg_slippage_bps, commission) for research — never raises, and is
  never used for live orders. Scenario is validated via the `ExecutionScenario`
  enum in the container.

## 11. Decision ledger (output 10)

`records.py`:

- `AiDecisionRecord` (persisted JSON): decision_id, asset, as_of, ensemble
  score, per-agent scores, verdict, requested/cleared quantity, reason,
  regime/volatility, news bias, failure state, created_at.
- `DecisionLedger(path)` — append `record()`, `get(id)`, `all(limit)`,
  `count()`, `write()`/`load()` (jsonl), `to_dict()`; default
  `data/ai/decisions.jsonl` from settings.
- `build_decision_record(...)` assembles a record; `ledger_stats(ledger)`
  summarizes counts by verdict.

## 12. AI failure monitor (output 11)

`failure.py` `AiFailureMonitor(config)` — `report(signal_set, news_age,
latest_prediction)` collects warnings over the last `drift_history` points:

- `agent_dispersion` above `max_agent_dispersion (1.0)` → warning;
- mean confidence above `max_mean_confidence (0.95)` → warning (saturation);
- confidence std above `max_confidence_std (0.25)` → warning;
- `news_staleness` — no relevant news inside `news_staleness_hours (48)` → warning;
- prediction drift — `|latest − median| > drift_max_abs (0.50)` → warning.
- **`degraded = critical OR >= 3 warnings`** — while degraded, `AiRiskGate`
  refuses to route any proposal.

## 13. Ablation framework (output 12)

`ablation.py`:

- `ABLATION_CASES`: 8 additive cases — `strategies_only` (no agents),
  `with_technical`, `with_news`, `with_fundamental`, `with_pattern`,
  `with_prediction`, `with_regime`, `full_system`.
- `run_ablation(...)` evaluates each case over a window and computes the
  **contribution of each added agent** as (case − previous) on sharpe/return/
  max-drawdown/cost-adjusted metrics; the final `full_system` duplicates the
  `with_regime` agent set, so its contribution is skipped (no fabricated edge).
- Emits `AblationReport` with per-case `AblationMetrics` and per-agent
  `AgentContribution`, exposing whether each agent adds or destroys value.

## 14. Settings and DI

- `settings.py::AiSettingsMixin` — `ai_*` knobs: weights version + enabled
  agents + per-agent weights, `ai_min_ensemble_abs_score (0.15)`,
  `ai_min_confidence (0.0)`, `ai_min_agreeing_agents (1)`,
  `ai_position_size_pct (0.02)`, `ai_leverage (1.0)`, `ai_news_model
  ("lexicon")`, `ai_finbert_model_name`, `ai_news_lookback_hours (24)`,
  `ai_news_per_symbol_limit (20)`, `ai_execution_scenario ("baseline")`,
  commission bps / participation / seed, ablation risk-free rate + periods,
  failure thresholds, `ai_ledger_path ("data/ai/decisions.jsonl")`.
  Builders: `ai_weights_config`, `ai_decision_config`, `ai_failure_config`,
  `ai_selector_config` (configs validated on construction).
- `container.py` — wires `WeightedEnsemble`, `StrategySelector`,
  `DecisionEngine`, `AiRiskGate` (over `PortfolioManager`), `SimulatedExecution`
  (scenario coerced to the `ExecutionScenario` enum), `DecisionLedger`,
  `AiFailureMonitor`, `NewsSentimentPipeline` (lexicon default; FinBERT only if
  configured) and the NewsAgent sentiment seam.
- Fixes from strict checks: slots-safe `SelectorConfig.__post_init__`
  (iterates `__dataclass_fields__`), honest nullable volatility, Decimal-safe
  `_factor`, `zip(..., strict=...)` in ablation tests, and the Phase 4
  `ExecutionOrder` residual-exit loop rewritten against the real
  `simulator.pending` API.

## 15. Files added / modified

- `src/qtrader/application/ai/` (new package): `__init__.py`, `models.py`,
  `sentiment.py`, `regime.py`, `signals.py`, `decision.py`, `selector.py`,
  `risk_gate.py`, `execution_integration.py`, `records.py`, `failure.py`,
  `ablation.py`.
- `src/qtrader/application/agents/news.py` — sentiment-model seam.
- `src/qtrader/config/settings.py`, `src/qtrader/config/container.py` —
  `AiSettingsMixin` + AI wiring.
- `tests/unit/fakes_ai.py` (new, not collected) — shared fakes: `make_signal`,
  `FakeSignalRepository` (`.rows`), `FakePredictionRepository`,
  `FakeNewsProvider` (records calls), `FakeNewsRepository` (url-dedup upsert +
  `recent`), `StubSentimentModel`, `FakeEventBus`, `make_price_bars`,
  `rising/falling/sideways_closes` (constant → SIDEWAYS).
- `tests/unit/test_ai_models.py`, `test_ai_regime.py`, `test_ai_sentiment.py`,
  `test_ai_news_agent.py`, `test_ai_signals.py`, `test_ai_selector.py`,
  `test_ai_decision.py`, `test_ai_risk_gate.py`,
  `test_ai_execution_integration.py`, `test_ai_records.py`, `test_ai_failure.py`,
  `test_ai_ablation.py`, `test_ai_settings.py` (new).

## 16. Tests performed

- **New** (99 tests): models (regime assessment serde incl. None volatility,
  agent weights versioning/effective agents, outcome records, selector config
  validation), regime (bull/bear/sideways from rising/falling/constant closes,
  confidence bounds, volatile ↔ extreme mapping), sentiment (lexicon
  calibration, FinBERT fail-safe on missing transformers, pipeline relevance
  filter + point-in-time + persistence url-dedup), news agent (sentiment model
  drives score; model error skips item; still analyzes without LLM; ignored
  when None), signals (prediction → score mapping `prob_up − prob_down`,
  collect gathers signals+regime+news, missing signals absent, agreeing
  agents), selector (exclusions: status/suspended/regime-cross/wf-unstable/
  missing-OOS; factor ordering, risk inversion, Decimal safety, top pick),
  decision (NO_TRADE/DEGRADED/APPROVE paths, conviction = |ensemble| ×
  (0.5 + 0.5 × confidence), quantity math e.g. 0.8-conf two-agent → 12 shares,
  1.0-conf single-agent 50k → 8), risk gate (approve routes to PortfolioManager;
  degraded blocks; Phase 5 rejection surfaced), simulated execution (fill /
  reject / partial-residual-exit outcomes, no-raise, scenario assumptions),
  records (ledger append/read/count/persist, stats), failure (dispersion,
  saturation, staleness, drift, degraded at critical or ≥3 warnings, recovery),
  ablation (8 additive cases, per-agent contribution, duplicate full_system
  no-op, metrics present), settings (mixin builders + snapshot).
- **Full suite**: **870 passed** (baseline 771 → +99), 27 skipped (pre-existing
  skips for optional Phase 2/3 data/AI paths).
- **Lint/type**: `ruff check src tests` clean; `mypy src` clean (183 files).

## References

- AI package: `src/qtrader/application/ai/*`.
- News seam: `src/qtrader/application/agents/news.py`.
- Settings/DI: `src/qtrader/config/{settings,container}.py`.
- Phase 5 gate: `src/qtrader/application/portfolio_mgmt/manager.py`.
- Phase 4 simulator: `src/qtrader/application/execution/simulator.py`.
- Tests: `tests/unit/test_ai_*.py`, `tests/unit/fakes_ai.py`.
- Prior audit: `docs/audit/25-phase5-portfolio-risk.md`.
