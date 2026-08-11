# Phase 2 — Dynamic Trading Universe Engine

Date: 2026-08-11. Work request: build the dynamic universe engine — asset
discovery, liquidity/tier filtering, membership lifecycle (add/suspend/delist/
resume/rename), point-in-time historical reconstruction, a strategy-compatible
`filter_symbols` query, and a scheduled `universe_cycle` update task. No
trading-strategy changes in this phase.

## Executive verdict

**READY FOR STRATEGY CONSUMPTION.** The engine discovers candidates from a
Yahoo Finance screener (with a seeded fallback), computes liquidity metrics from
persisted D1 bars, assigns tiers (A/B/C), persists membership state with full
history (added/removed dates, reasons), reconstructs the tradable set as-of any
date for backtests, and exposes a single `UniverseFilterRequest` used by
strategies. The `0004_universe_engine` migration is applied to the dev database
and verified. Full suite **490 passed, 27 skipped**; ruff clean (src+tests);
mypy clean on all changed files.

## 1. Design

**Entities** (`src/qtrader/domain/entities/__init__.py`): `AssetType`
(stock/ETF/ADR/REIT/…), `TradingStatus` (active/suspended/delisted),
`UniverseTier` (A/B/C), `DiscoveredAsset`, `LiquidityMetrics`,
`UniverseMembership`, `SymbolChange`.

**Ports** (`src/qtrader/domain/ports/__init__.py`):
`AssetDiscoveryProvider.discover_candidates(limit)` and `UniverseRepository`
(list/get/upsert memberships; record/list symbol changes).

**Engine** (`src/qtrader/application/services/universe.py`):

- `UniverseThresholds` mirrors `UniverseSettingsMixin` in `settings.py`
  (`from_settings`). Base floor: min trading days (30), min price ($2), min
  dollar volume ($1M), min avg volume (200k), optional max spread % (2) and
  min market cap. Tier A: ≥$20M dv AND ≥$10; Tier B: ≥$5M AND ≥$5; else C.
  `None` disables a check.
- `refresh()`: discover → per-asset metrics+classification → persist
  memberships → staleness pass over untouched members → rename detection →
  `UniverseReport`.
- Membership semantics: new candidates below the floor are **not persisted**;
  existing members that dip below the floor keep membership/history/tier and
  only record a `reason`. `added_at` = first bar date.
- `resolve_status`: DELISTED is terminal; no bars + `removed_at` set → DELISTED,
  no bars otherwise → SUSPENDED; bar age > `stale_delist_days` (60) → DELISTED,
  > `stale_suspend_days` (15) → SUSPENDED, else ACTIVE (resume).
- `universe_as_of(memberships, as_of)` + `point_in_time_universe` reconstruct
  the tradable set at any date using `added_at`/`removed_at` — backtest-safe,
  no look-ahead.
- `detect_symbol_changes`: conservative ticker-rename heuristic — an existing
  non-delisted member missing from today's discovery that has an exact
  normalized-name match in the discovered set records a `SymbolChange`
  (e.g. FB→META).
- `filter_symbols(UniverseFilterRequest)` — strategy entry point:
  `min_tier` (strict A≥B≥C ordering), `as_of`, optional live
  `min_dollar_volume`/`min_price` overrides recomputed from stored bars,
  `max_symbols` cap, and a `symbols` whitelist. `snapshot()` reports
  active/suspended/delisted/tradable counts by tier for the dashboard.

## 2. Discovery provider

`src/qtrader/infrastructure/data_providers/discovery.py`:

- `YahooAssetDiscoveryProvider` hits `/v1/finance/screener/predefined/saved`
  (`most_actives`), count ≤ min(limit, 200). Uses the existing CircuitBreaker
  (`yahoo_screener`), a TokenBucket rate limiter, crumb auth
  (`/v1/test/getcrumb`), and treats HTTP 429 as an error (feeds the breaker).
  `close()` releases the client.
- `parse_screener_response` is a pure function: drops no-symbol rows, dedupes,
  maps quote types to `AssetType`, extracts exchange/market-cap/currency.
- `_discover` returns `("provider" | "seed", assets)`; any provider failure or
  empty result falls back to `StockRepository.list_active()`, then to
  `Settings().watchlist_symbols` when the stock table is empty (gated by
  `seed_from_watchlist`). The report carries the actual source.

## 3. Persistence

Migration `alembic/versions/0004_universe_engine.py` (revision
`0004_universe_engine`, down `0003_backtest_runs`) creates:

- `universe_memberships` — symbol (unique), status, tier, added_at, removed_at,
  last_traded_at, asset_type, name, reason, `extras` JSONB (live metric
  snapshot), created/updated timestamps; indexes on status and removed_at.
  The JSON column is **`extras`**, not `metadata` — `metadata` is reserved by
  SQLAlchemy declarative and breaks model registration.
- `universe_symbol_changes` — old/new symbol (indexed), effective_at, reason.

Applied to the dev Postgres and verified (`\d` output matches the model;
`pk`/`uq`/`ix` constraints present). Repository
`SQLAlchemyUniverseRepository(SessionBoundRepo, UniverseRepository)` maps
`extras` ↔ domain `metadata`.

## 4. Scheduler & wiring

- `universe_cycle` task (`src/qtrader/infrastructure/schedulers/tasks.py`):
  refresh, daily-backfill `report.added` symbols (1d, `backfill_days`), record
  a `universe` agent metric (`tradable` count). Cron at
  `hour={universe_refresh_hour}` (default 01:00), not market-gated.
- Container (`src/qtrader/config/container.py`): registers
  `UniverseRepository`, the Yahoo `AssetDiscoveryProvider`, and a
  `UniverseEngine` whose thresholds come from settings; `aclose()` releases the
  discovery provider.

## 5. Tests & verification

- `tests/unit/test_universe.py` — pure logic: `compute_liquidity_metrics`
  (window truncation, <2 bars), `estimate_spread_pct`, `classify`/`tier_gte`
  (A/B/C boundaries + floors), `universe_as_of`, `point_in_time_universe`,
  `resolve_status` (fresh/stale/very-stale/no-bar/resume/terminal),
  `detect_symbol_changes` (match, no-match, skip-delisted, used-once).
- `tests/unit/test_universe_engine.py` — engine with fakes: adds members and
  assigns tiers, `added_at` = first-bar date, below-floor candidates not
  persisted, seed fallback on provider error (source reported as `seed`),
  suspend stale / resume fresh / delist no-recent, symbol-change recording,
  `filter_symbols` (min-tier, as-of, symbols whitelist, cap, live liquidity
  override), snapshot coverage, spread filter via M5 intraday.
- `tests/unit/test_discovery.py` — screener payload parsing.
- `tests/unit/test_settings.py` — universe settings defaults + env overrides.
- `tests/unit/test_models_metadata.py` — the two new tables added to the schema
  inventory.
- Full suite: **490 passed, 27 skipped**. `ruff check` clean on all touched
  src+tests; `mypy` clean on the 10 changed source files. `alembic upgrade
  head` validated against the running dev database.

## 6. Limitations carried forward

1. Yahoo has no delisting coverage — a delisted name simply stops appearing;
   the engine detects this via data staleness, not a corporate-action feed.
2. Discovery is limited to the `most_actives` screener (top ~200 by volume);
   the long tail of the market is not scanned.
3. Ticker-rename detection is heuristic (exact normalized-name match) and may
   occasionally miss or over-match; changes are logged, not auto-applied.
4. Intraday M5 is delayed snapshot data (Phase 1 audit §8); spread estimates
   are only as fresh as the last backfilled intraday bars.

## References

- Tests: `tests/unit/test_universe.py`, `tests/unit/test_universe_engine.py`,
  `tests/unit/test_discovery.py`, `tests/unit/test_settings.py`,
  `tests/unit/test_models_metadata.py`.
- Evidence: `alembic upgrade head` log + `psql \d universe_memberships` /
  `\d universe_symbol_changes` (this report).
- Prior audits: `docs/audit/18-phase1-market-data.md` (data layer verdict),
  `docs/audit/17-phase3-final-validation.md` (engine/risk verdict).
