# Phase 4 — Per-Agent Audit (Input / Output / Accuracy / Latency / Failure / Historical / Ablation / Unified Record)

Date: 2026-08-08. Baseline: `a459406` (after Phase 2/3 commits).

## 1. Agent inventory & status

| agent | source | unit-tested | persisted output | exercised in live run? | exercised in backtest? |
|---|---|---|---|---|---|
| DataAgent | `agents/data.py` | yes | `prices` (624,820 bars) | yes (backfill) | via in-memory loader |
| Scanner | `agents/scanner.py` | yes | `agent_metrics` (477 runs, candidates avg 5.79) | yes | no (WF uses full universe) |
| TechnicalAgent | `agents/technical.py` | yes | `signals.agent='technical'` (2570) | yes | no (WF recomputes own signals) |
| FundamentalAgent | `agents/fundamental.py` | yes | `signals.agent='fundamental'` (2568), `fundamentals` (7) | yes | no |
| NewsAgent | `agents/news.py` | yes | `signals.agent='news'` (2567), `news` (125) | yes (default sentiment, no LLM key) | no |
| PredictionAgent | `agents/prediction.py` | yes | `predictions` (2569, model `momentum` v0) | yes (heuristic fallback) | no |
| RiskAgent | `agents/risk.py` | yes | `risk_history` (**0 rows**) | no (no orders) | no |
| ExecutionAgent | `agents/execution.py` | yes | `orders` (4), `trades` (0) | no (dry-run only) | no |
| PortfolioAgent | `agents/portfolio.py` | yes | `positions`/`portfolios` | partial | no |
| ChiefAgent | `agents/chief.py` | yes | `decision_log` (2569) | yes | no |

**Verdict: every agent has unit tests; the *live* pipeline exercises data/scanner/technical/news/fundamental/prediction/chief. Risk, execution, and portfolio produce no real records (0 trades).**

## 2. Fatal findings

1. **The agent ensemble is a one-day artifact, not a validated strategy.**
   All 7,705 signals, 2,569 predictions, and 2,569 decisions were created
   **2026-08-06 only** (1 distinct date). The 5 real symbols (AMZN, GOOGL,
   MSFT, NVDA, TSLA) each have **513 decisions with 513 distinct timestamps
   across a 45-minute window** (13:30:59→14:15:49 UTC) — a loop-run artifact,
   not a per-day decision history. Result: **zero forward-return observations
   exist**, so agent-level *accuracy is unmeasurable* (not failed, not tested).
2. **The agent ensemble and the backtested strategy are two disconnected systems.**
   The persisted 38.78%-win-rate walk-forward backtest (`strategy_performance`
   id145, +9.23%) recomputes technical signals **in-process** and never calls
   the agents. The agents' ensemble has **never been backtested**. There is no
   OOS performance for the actual decision path that would trade live.
3. **Prediction agent is a placeholder.** Registry = `dash-momentum` v1–v6,
   all registered within **2 seconds** (2026-08-06 20:24:39–41), identical
   `hyperparams {coef:[0.1]}`, `offline_metrics.accuracy=0.6`, and
   `artifact_path=null` (no artifact exists). v1 is active; inference always
   falls back to heuristic `momentum` v0 (rationale in decision_log:
   `model=momentum v0 conf=0.37 exp_ret=-0.0003`). The trainer promoted one
   model on 08-04 (acc 0.6317) but it is disconnected from live inference.
4. **News agent is inert without an LLM key.** `news` has 125 rows; signals
   use the keyword-default sentiment; no LLM-driven scoring occurs.
5. **Fundamental agent is nearly empty.** 7/502 symbols have fundamentals;
   the 2,568 fundamental signals are computed for 5 mega-caps with
   placeholder/fallback inputs.
6. **Only 1 persisted backtest run** (`backtest_runs` id=274, technical,
   2025-07→2026-07, commission 1 bp + slippage 5 bp, **ret −12.65%**, 442
   trades). The +9.23% ensemble walk-forward run is *not* persisted as a run
   (CLI execution) — only its summary sits in `strategy_performance`.
   **Reproducibility gap**: the headline result cannot be re-derived from the DB.
7. **Risk/execution/portfolio records are empty** (`risk_history` 0, `trades`
   0, `orders` 4) → no trade-level evidence exists anywhere.

## 3. Unified agent record — required vs stored

Required per brief: `agent_id, timestamp, input_data_version, output,
confidence, latency, reason, features_used, model_version`.

| field | where stored today | status |
|---|---|---|
| agent_id | `signals.agent`, `predictions.model_name` | ok |
| timestamp | `signals.created_at` | ok |
| input_data_version | — | **missing** |
| output | `signals.signal_type/score`, `decision_log.decision` | ok |
| confidence | `predictions.confidence`, `decision_log.confidence` | ok (heuristic) |
| latency | — | **missing** (`system_logs` only 25 rows, no per-agent timing) |
| reason | `decision_log.rationale` (ensemble-level only) | partial |
| features_used | `signals.metadata.sub_scores` (technical), `predictions.features_hash` (never populated) | partial |
| model_version | `predictions.model_version` (=0 heuristic) | misleading |

**Gap: `input_data_version` and `latency` are not recorded anywhere;
`features_hash` is never populated; `model_version=0` is the fallback, not a
registered model.** Schema extensions are deferred to the rebuild phase
(Phase 18), per the freeze discipline.

## 4. Test-dimension status (P4 scope)

- **Input/Output tests**: present and green for all agents (unit suite).
- **Accuracy test**: **impossible on the live path** (single day, no forward
  bars). Will become measurable only after the rebuild (Phase 18+) or via the
  strategy-level forward-return evaluation (Phase 5/16 on the WF path).
- **Latency test**: no data; add timing capture in rebuild.
- **Failure test**: present (`test_resilience.py`, `test_yahoo_resilience.py`,
  DataAgent provider-failure paths).
- **Historical test**: no history exists (all outputs = 2026-08-06).
- **Ablation test**: Phase 5 (strategy-level A–H on the rerunnable WF path).

## 5. Answers for the brief

- Do the agents "work"? They *execute* (produce output) but the outputs are
  single-day, heuristic/placeholder, and never feed the backtested strategy.
- Is there a real strategy or overfitting? The WF strategy shows a PF≈1.19
  win≈38.8% on a **flawed** backtest (unadjusted prices, survivorship, fold
  misalignment) — see Phases 2/3; the agent ensemble itself has **no
  performance evidence at all**.
