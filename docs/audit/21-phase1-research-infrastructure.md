# Phase 1 — Trading Research Infrastructure

Date: 2026-08-12. Work request: inspect the existing multi-agent system and
prepare the infrastructure for quantitative strategy research — dependency
audit, clean internal interfaces with third-party libraries behind adapters,
a unified technical-indicator layer, and verification that the existing
system still works. **No trading strategies were created or modified.**
Baseline: `a3d8119`.

## Executive verdict

**READY FOR STRATEGY RESEARCH.** The system already carried most of the
infrastructure: pandas/numpy indicator engine, pluggable strategy framework,
custom backtest engine (fills, costs, ATR sizing, bracket/time exits), a
numpy-only logistic-model pipeline, and `domain/ports` ABCs. This phase added
the missing piece — a named research integration layer (six Protocols +
concrete adapters, DI-wired) and a by-name indicator factory so consumers
never touch pandas/third-party TA code directly. **Zero new dependencies**
were required. Full suite **558 passed, 27 deselected**; `ruff check src
tests` clean; `mypy src` clean; backtest engine re-verified.

## 1. Existing project — what was already there

- **Architecture**: clean hexagonal layout — `domain` (entities, value
  objects, events, `ports` ABCs), `application` (agents + services +
  strategies), `infrastructure` (DB/`data_providers`/`brokers`/`llm`/`news`/
  `cache`/`eventbus`/`ml`/`resilience`/`schedulers`), `interfaces` (FastAPI,
  CLI, dashboard). DI via punq in `config/container.py`.
- **Agents**: data, scanner, technical, news, fundamental, prediction, risk,
  portfolio, execution, chief — all depend on `domain/ports` and
  `application/services`, none import a third-party trading library.
- **Backtesting**: `application/services/backtest.py` (`BacktestRunner`),
  `walk_forward.py`, `multitimeframe.py` simulators, `strategies/*`
  (trend, momentum, mean_reversion, breakout, ml, baselines, value_factor).
- **Market data**: Yahoo provider + `PriceRepository`/`MarketDataProvider`
  ports; universe engine (Phase 2).
- **ML**: `model_trainer.py` fits a logistic model in pure numpy;
  `prediction_model.py` (`LogisticModel`/`HeuristicModel`).
- **Indicators**: `application/services/indicators.py` already implements
  SMA, EMA, RSI, MACD, ATR, ADX, Bollinger, VWAP, Stochastic, Ichimoku,
  VolumeProfile over pandas.
- **Database/config/tests**: SQLAlchemy+asyncpg+Alembic; pydantic-settings;
  pytest (asyncio auto) with fakes, ruff + mypy in CI.

## 2. Dependency audit

Already installed: fastapi, uvicorn, pydantic(-settings), sqlalchemy, asyncpg,
alembic, redis, arq, numpy, pandas, tenacity, structlog, punq, typer, httpx,
pytest(-asyncio/-cov), fakeredis, ruff, mypy, types-requests.

Evaluated and **not** installed (with reasons):

- **vectorbt** — not required: a mature custom backtest engine already exists
  (`BacktestRunner` + walk-forward + multitimeframe simulators); adding
  vectorbt would duplicate and diverge execution semantics.
- **TA-Lib** — not required: the full required indicator set is already
  implemented in pure pandas (`indicators.py`); TA-Lib adds a fragile C
  build for no functional gain.
- **scikit-learn** — not required: the logistic model is fitted in pure numpy
  (`model_trainer.py`).
- **XGBoost / LightGBM** — not required: no tree-model code exists or is
  planned in this phase.
- **PyTorch** — not required: no deep-learning path; only "if actually
  required".
- **PyPortfolioOpt** — not required: allocation is deterministic
  (`EqualWeightAllocation`); no portfolio-optimization requirement.
- **statsmodels** — not required by current code.

## 3. Dependencies installed / files updated

**None installed.** pyproject.toml already pins every required library
(numpy, pandas, plus web/DB/async/ML). `requirements.txt` does not exist and
no lock file is used. Verified the installed env imports cleanly (pandas
3.0.5 / numpy 2.5.1) and that all 533 baseline tests still pass. No
`pyproject.toml` change was warranted — adding nothing avoids version churn.

## 4. Integration layer (new)

`src/qtrader/application/research/` — a research-facing seam. No agent or
research module imports pandas/numpy or a third-party TA library directly;
everything flows through the six Protocols with adapters over existing
services:

| Interface | Adapter | Wraps |
|---|---|---|
| `MarketDataInterface` | `MarketDataAdapter` | `PriceRepository` |
| `StrategyInterface` | `StrategyAdapter` | `Strategy` ABC |
| `BacktestInterface` | `BacktestAdapter` | `BacktestRunner` |
| `IndicatorInterface` | `IndicatorAdapter` | `IndicatorEngine` |
| `PortfolioInterface` | `PortfolioAdapter` | `PortfolioService` + position/portfolio repos |
| `PredictionInterface` | `PredictionAdapter` | `PredictionModel` (registry-backed) |

`PredictionAdapter.from_registry()` loads the active registered
`LogisticModel` with a deterministic `HeuristicModel` fallback. All six
adapters are registered in the punq container and resolvable (verified in
`test_container.py`). Interfaces use `@runtime_checkable` Protocols, so
consumers type against the contract and DI can swap implementations.

## 5. Technical indicator layer (unified, no duplication)

`indicators.py` was the single indicator implementation; this phase added a
by-name registry + factory so every consumer builds indicators the same way:

- `INDICATOR_REGISTRY` maps names → classes; `indicator_factory(name, **params)`
  is the one construction entry point (case-insensitive, unknown names raise
  `ValueError` listing supported indicators); `indicator_names()` for
  discovery. Covers the required set: **SMA, EMA, RSI, MACD, ATR, ADX,
  Bollinger Bands, VWAP, Stochastic** (plus Ichimoku/VolumeProfile).
- Indicator calculation remains centralized in one place; agents/strategies
  share `IndicatorEngine` / `IndicatorInterface` instead of re-implementing
  moving averages, RSI, etc.

## 6. Files modified / added

- `src/qtrader/application/research/interfaces.py` (new) — six Protocols.
- `src/qtrader/application/research/adapters.py` (new) — six adapters.
- `src/qtrader/application/research/__init__.py` (new) — package exports.
- `src/qtrader/application/services/indicators.py` — registry + factory.
- `src/qtrader/config/container.py` — adapter registrations.
- `tests/unit/test_research_infrastructure.py` (new) — 25 tests.
- `tests/unit/test_container.py` — resolve checks for the five DI adapters.
- `tests/unit/test_universe_engine.py` — made one date-stamped assertion
  relative to `now` (it had hard-coded yesterday's date and broke on the
  date rollover; pre-existing, unrelated to this phase).

## 7. Tests performed

- **New** `tests/unit/test_research_infrastructure.py` (25 tests):
  factory/registry (names, case-insensitivity, params, unknown-name error,
  required-set coverage) and **hand-computed correctness anchors** for SMA,
  EMA, VWAP, Bollinger, Stochastic, MACD (= fast−slow EMA), ATR, RSI
  (rising→100 / falling→0), ADX (bounds + trending series), plus adapter
  delegation for all six adapters (fakes; registry-loaded prediction).
- **Backtest engine re-verified**: full suite `tests/unit/test_backtest_runner.py`
  + walk-forward/multitimeframe backtest tests pass unchanged.
- **Full suite**: **558 passed, 27 deselected** (baseline was 533; +25 new).
- **Imports verified**: research package + all indicator names import cleanly.
- **Lint/type**: `ruff check src tests` clean; `mypy src` clean (133 files).

## 8. Compatibility problems

- pandas 3.0.5 / numpy 2.5.1 in the venv are newer than the floor pins; the
  full suite (including indicator and backtest tests) passes on them, so no
  action was needed.
- One pre-existing date-sensitive universe test failed solely because it
  hard-coded `2026-08-11` as "now"; fixed to assert against
  `datetime.now(UTC).date()`.

## 9. Confirmation the existing system still works

Yes. All 533 baseline tests pass unchanged; 25 new tests added; `ruff` and
`mypy` are clean; the backtest engine, indicator engine, agents, ML pipeline,
and DI container all still resolve and pass their existing test suites. **No
trading strategies were created or modified in this phase.**

## References

- Interfaces/adapters: `src/qtrader/application/research/*`.
- Indicator layer: `src/qtrader/application/services/indicators.py`.
- DI: `src/qtrader/config/container.py`; port ABCs: `src/qtrader/domain/ports/__init__.py`.
- Tests: `tests/unit/test_research_infrastructure.py`,
  `tests/unit/test_container.py`, `tests/unit/test_indicators.py`,
  `tests/unit/test_backtest_runner.py`.
- Prior audits: `docs/audit/20-phase3-multitimeframe.md`,
  `docs/audit/19-phase2-universe.md`.
