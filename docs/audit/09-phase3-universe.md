# Phase 3 — Universe & Survivorship-Bias Audit

Date: 2026-08-08. Baseline: `a459406`.

## 1. Findings (recomputed against `prices`/`stocks`/`backtest_runs`)

**Survivorship bias is structural and confirmed.**
- 502 symbols, all with bars through 2026-07+ → **0 delisted names**. The list is a
  *current* S&P 500 membership pull; any name removed from the index during
  2021–2026 is absent entirely. Historical tests therefore only see today's
  survivors.
- Point-in-time coverage grows with time (tradeable = first bar ≤ anchor):

  | anchor | tradeable symbols |
  |---|---|
  | 2021-09-01 | 492 |
  | 2022-01-01 | 492 |
  | 2023-01-01 | 494 |
  | 2024-01-01 | 496 |
  | 2025-01-01 | 498 |
  | 2026-01-01 | 500 |

  So the universe is not static — 10 names listed after the data window opened
  (VLTO 2023-10, SOLV 2024-03, GEV 2024-03, SNDK 2025-02, Q 2025-10, FDXF 2026-05,
  HONA 2026-06, plus GEHC/KVUE/CEG in 2022–23), yet the membership list is the
  2026 one. **No historical point-in-time membership table exists.**
- **Delisted-name absence cannot be fixed from this dataset** — the fix needs an
  external point-in-time constituents source (e.g. CRSP S&P 500 history, or a
  delisted-symbol provider). Flagged as a hard data-sourcing requirement.

## 2. Universe inconsistencies across layers

| layer | size | notes |
|---|---|---|
| `prices` table | 502 | includes newest listings HONA/FDXF/Q |
| persisted `backtest_runs[274].universe` | 499 | excludes HONA/FDXF/Q |
| walk-forward experiment universe | 498 | (prior session log) |

Three different universe definitions in use → results across runs are not
directly comparable. `stocks.exchange` is `YAHOO` for all 502 (auto-created on
ingest), plus 1 `PAPER` test + 4 `XNAS` fixtures. **All 502 are `is_active=false`**
while the 4 test fixtures are active — the flag is not a reliable record of
trading eligibility; the scheduler re-activates the watchlist at startup.

## 3. Listing-date compression of the walk-forward window

Folds are **bar-index aligned** (`walk_forward.py:_make_folds`, min bars over
the universe) not calendar aligned. Short listings in the experiment universe
(GEV 592, SOLV 593 bars; also VLTO/GEHC/KVUE/CEG) set the shared timeline, so:
- for the ~492 full-history names the true OOS test window is ≈ **14 months
  (2022-09..2023-11)** despite the "2021–2026" label;
- new listings (GEV/SOLV) are tested over a *different* calendar window
  (2024–2026) entirely — a backtest that trades a 2024 listing "in 2021" is a
  look-ahead.

This is the single most damaging backtest-design flaw after unadjusted OHLC.

## 4. Deliverable: point-in-time universe filter (BUILT)

`src/qtrader/application/services/universe.py`:
- `point_in_time_universe(listing_dates, as_of) -> list[str]` — returns only
  symbols listed on/before a date (listing date = first stored bar).
- `listing_date_from_first_bar(ts)` helper.
- Tests: `tests/unit/test_universe.py` (4 cases). Suite 327 unit + 25 integration.

This lets any historical test select the tradeable set per window and exclude
future listings. **Integration into the walk-forward fold constructor is a
Phase 10/18 remediation** (calendar-aligned per-symbol windows + PIT eligibility).

## 5. Answer for the brief

- Survivorship bias: **YES, confirmed** — 0 delisted names, current-membership
  only, no point-in-time table.
- Stocks available / analyzed / ignored: 502 in prices, 499 in the persisted
  backtest universe, 498 in the experiment; 3 newest listings excluded from the
  backtest run; 10 names only partially tradeable; the *reason* for exclusion
  is ad-hoc, not a documented eligibility rule.
