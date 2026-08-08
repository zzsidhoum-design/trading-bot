# Phase 2 — Data Layer Audit & Validation Layer

Date: 2026-08-08. Baseline: `a459406` (frozen `fcb51dd` fixed engine).

## 1. Integrity checks (persisted `prices`, recomputed this session)

| check | result |
|---|---|
| rows / symbols / interval | 624,820 / 502 / `1d` |
| range | 2021-08-02 .. 2026-08-06 |
| duplicate (symbol, ts) | 0 (DB unique `uq_prices_stock_interval_ts`) |
| high < low / high < max(O,C) | 0 (blocked at `PriceBar` construction) |
| **low > min(O,C)** | **2 — GL, STE on 2026-08-06** (live snapshot bars; `PriceBar` does not check this) |
| non-positive price | 0 |
| zero volume | 352 (all `SW`, 2021–2022) |
| weekend bars | 0 |
| calendar gap > 30d | 0 |
| \|1-day close move\| > 50% | 4 — CVNA +56% (rev-split seq), GL −53% (split), HOOD +50%, ECHO +70% |

Two UTC stamp offsets (13:30 / 14:30 UTC) are the same 09:30 ET bar under
EST/EDT — benign DST labeling, not a data error.

## 2. Data layer structure (code findings)

- **One provider, no alternative:** only `YahooFinanceProvider`
  (`infrastructure/data_providers/yahoo.py`), `/v8/finance/chart`. Settings
  `data_provider`, `yahoo_enabled`, `polygon_api_key` are **dead config** —
  the container always wires Yahoo (`config/container.py:219`). No Polygon,
  no IEX/StaticFeed, no websocket; "real-time" is the same delayed HTTP quote
  endpoint (`fetch_quote` → `range=1d`, M1 bar).
- **No corporate-action handling anywhere.** `adjclose` is fetched
  (`events=history`) but the parser reads only `indicators.quote`
  (`yahoo.py:48-53`) and ignores it → stored OHLC is unadjusted → split dates
  appear as ±50% "returns" and corrupt any feature/label derived from closes.
- **Upsert cannot heal:** `insert ... on_conflict_do_nothing`
  (`infrastructure/database/repositories/sqlalchemy.py:221`) means a re-ingest
  of a duplicate bar silently skips; the GL/STE and SW rows persist forever.
- **Silent ingestion failures:** provider `RuntimeError` and empty windows both
  return `0` from `DataAgent.backfill` with no distinction (alerting could not
  tell "provider down" from "no data"). `vwap`/`source` are never populated.
- **Backfill has no gap detection** despite the docs claim
  (`docs/02-agents.md:26`): it blindly fetches a fixed `backfill_days` window.
- **Universe seed is not in the repo** (no script); 502-name S&P 500 snapshot,
  survivorship-biased (see Phase 3).
- **Missing candles / short history:** 7 symbols < 800 bars (HONA 37, FDXF 50,
  Q 195, SNDK 371, GEV 592, SOLV 593, VLTO 712) — 2024–2026 listings.

## 3. Data Validation Layer (BUILT — Phase 2 requirement)

New `src/qtrader/application/services/bar_validator.py` (`BarValidator`,
`ValidationReport`, `DataGap`):

- **Rejects** (counted per reason, before persist):
  - `ohlc-low-above-open-close` — `low > min(open, close)` (the GL/STE defect);
  - `ohlc-high-below-open-close` — defensive (already blocked by `PriceBar`);
  - `weekend-bar` on daily data;
  - `large-single-bar-move` — |close/prev_close − 1| > `data_max_single_bar_move_pct`
    (default 0.50), with previous-close baseline seeded from
    `prev_close_by_symbol` or inferred chronologically within the batch;
    configurable `data_reject_large_moves=False` downgrades to a flag
    (`large-single-bar-move-flagged`).
- **Reports** `gaps`: calendar gaps > `data_max_calendar_gap_days` between a
  symbol's bars (delisting/halt/missing candles).
- Wired into `DataAgent.backfill` and `DataAgent.refresh`
  (`application/agents/data.py`): cleaned bars → validated → upserted; reasons,
  reject count and gaps are logged; provider failure and empty windows are now
  logged separately (`data.backfill.provider_failed` / `data.backfill.empty_window`).
- Settings: `data_max_single_bar_move_pct`, `data_reject_large_moves`,
  `data_max_calendar_gap_days` (defaults 0.5 / True / 10) wired via container.
- **Verified against real data:** running the validator over the stored GL/STE
  final bars rejects exactly those two bars (`ohlc-low-above-open-close: 2`)
  and keeps the clean 08-05 bars.
- Tests: `tests/unit/test_bar_validator.py` (10 cases). Suite now
  **323 unit + 25 integration pass**, ruff + mypy clean.

## 4. Verdict & what remains

The layer now rejects structurally-suspicious bars before they reach agents,
and surfaces gaps/empty/failed windows explicitly. **Not fixed here** (deferred
to Phase 18 / documented): adjusted-price ingestion (`adjclose`), corporate
actions, heal-capable upsert (cleanup of the persisted GL/STE + SW rows), a
second data source, and real-time feeds. The largest data risk to the *model*
remains the unadjusted OHLC (split artifacts) — the validator's large-move
rejection is a stopgap; proper adjustment is the fix.
