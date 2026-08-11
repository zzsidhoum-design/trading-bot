# Phase 1 — Market-Data Layer Audit & Hardening

Date: 2026-08-11. Engine at HEAD `a9b7907`. Work request: *"before any
strategy work — audit the market-data layer: data-source inventory, integrity
checks, corporate actions, look-ahead/survivorship bias, real-time freshness;
fix critical data-quality issues first; produce a Data Quality Report with a
readiness verdict; no new trading strategies."*

## Executive verdict

**RELIABLE FOR STRATEGY RESEARCH** — with the following caveats locked into the
report (below): intraday M5 is *delayed snapshot* data with a small tolerated
provider-hole allowance, intraday bars are unadjusted while D1 is
split/dividend-adjusted, and there is no streaming/real-time feed.

Structural integrity of the persisted price universe is clean after this
session's repairs (zero-volume junk deleted, misaligned intraday bars deleted,
provider-side gaps re-backfilled) and is now enforced automatically by a new
`BarCleaner` rule plus a scheduled `data_quality_cycle` job. The persisted Data
Quality Report is **PASS (score 1.00)**.

## 1. Data-source inventory

| source | role | provides | real-time? |
|--------|------|----------|------------|
| Yahoo Finance `/v8/finance/chart` | market data (only price provider) | OHLCV M1/M5/M15/H1/D1 | **No** — delayed snapshot ≈10–15 min, polled; no WebSocket/streaming |
| Alpaca | broker only (`broker_provider="paper"`) | order routing | n/a — not a data feed |
| SEC EDGAR XBRL | fundamentals | point-in-time financials (`asof`=filing date) | daily-ish (filings) |
| Yahoo RSS | news | headlines + LLM scoring | near-live, no history |

There is no second price source; price integrity therefore depends on the
cleaner/validator/audit stack below.

## 2. Integrity

Persisted universe after this session's repair (Postgres `prices`, 634,729
rows):

| check | rule | result |
|-------|------|--------|
| duplicates | (`stock_id`,`interval`,`ts`) unique | **0** |
| invalid OHLC | `high<low` etc. | **0** |
| non-positive price | `open/high/low/close <= 0` | **0** |
| zero volume | `volume = 0` | **0** (was 517) |
| misaligned intraday | intraday `second≠0` / off-grid minute | **0** (was 81) |
| weekend D1 | DOW in {Sat,Sun} (ET) | **0** |
| off-session intraday | bars outside 09:30–16:00 ET | **0** |
| future bars | `ts > now + 2min` | **0** |

Repair performed:
- `DELETE FROM prices WHERE volume = 0` (517 rows: 1d inactive-symbol junk incl.
  SW ×352, plus live 5m zero-volume junk) and all misaligned intraday rows
  (81; a subset of the zero-volume set).
- Re-backfilled M5 for 2026-08-03..08-10 (2,805 bars) after the audit exposed
  mid-session provider gaps (e.g. AAPL 2026-08-05 missing 11:45–14:30 ET).
  3 provider-side holes remain (single bars on 08-04 ×2, 08-07 ×1) — Yahoo
  does not serve them; tolerated by `max_missing_m5_bars=5`.

New enforcement in `BarCleaner.clean`:
- `reject_zero_volume=True` → drops `volume=0` (reason `zero-volume`).
- `align_intraday=True` → drops intraday bars not on the interval grid
  (`second≠0`, or minute not a multiple of 5/15, or `minute≠0` for H1); D1
  exempt (reason `misaligned-timestamp`). This kills the in-progress
  live-refresh bar that previously landed in the DB with an end-of-request
  timestamp and `volume=0`.

## 3. Corporate actions

- D1 is **adjusted** (auto_adjust) — NVDA 2024-06 10:1 split, 2023-01 $14.28
  vs split-adjusted 2024-06 $120.78, verified consistent.
- Intraday M5 is **unadjusted**. Divergence at a split boundary is a known
  edge; the D1-vs-M5 consistency check (max |Δ|/D1 close ≤ 1%) currently reads
  0.59% (NVDA 08-10) and would catch any boundary drift within the audit
  window. No historical backtest mixes D1 with intraday today.

## 4. Look-ahead / survivorship

- Backtest, features and ML are backward-only (next-bar-open fills, intra-bar
  stops; indicators/labels never peek). Fundamentals are truly point-in-time
  (`merge_asof` backward on `asof` = EDGAR filing date). No look-ahead found.
- Survivorship: universe = S&P 500 constituents as of the backtest snapshot;
  Yahoo has no delisting coverage — a delisted name simply disappears. Flagged
  as a limitation for long-horizon research; acceptable for the current 5-year
  D1 work (verified: no >4-day D1 gap over 5y for the six watchlist names).

## 5. Real-time / freshness

- No streaming. Cadence: `backfill_cycle` every 15m (M5 last 5 days), `scan`
  5m, `execute` 15m, train 02:00, backtest 03:00, walk-forward 03:05.
- Freshness is enforced at ingestion (`reject_stale=True`, 10 min lateness /
  60 s future) and at risk-decision time (risk staleness ceiling, Phase 3).
- New `data_quality_cycle` (every 30m, not market-gated) audits the persisted
  universe and records a dashboard score metric.

## 6. Data Quality Report (live run, 2026-08-11)

`python scripts/data_quality_report.py` → **PASS, score 1.00** (scope
AAPL/MSFT/TSLA/NVDA/AMZN/GOOGL):

| check | status | detail |
|-------|--------|--------|
| duplicates | ok | 0 |
| invalid_ohlc | ok | 0 |
| non_positive_price | ok | 0 |
| zero_volume | ok | 0 |
| misaligned_intraday | ok | 0 |
| weekend_daily | ok | 0 |
| off_session_intraday | ok | 0 |
| future_bars | ok | 0 |
| m5_coverage | ok | missing 3 / tolerance 5 (last 5 sessions) |
| daily_gaps | ok | max gap 4 days (all watchlist) |
| d1_m5_consistency | ok | max diff 0.59% (NVDA 08-10) |
| freshness | ok | market closed — not enforced (pre-market run) |

## 7. Tooling added

- `src/qtrader/application/services/data_quality.py` — `DataQualityAuditor`,
  `DataQualityReport`, 12 checks; all thresholds explicit parameters.
- `SQLAlchemyDataQualityRepository` — read-only aggregate queries (structural
  counts, per-session M5 coverage, D1 gaps, D1-vs-M5 drift, freshness).
- `DataQualityRepository` port; container wiring; `data_quality_cycle` cron.
- `scripts/data_quality_report.py` — prints the report; exit 0/1 for CI.
- Tests: `tests/unit/test_data_quality.py` (17), cleaner alignment/zero-volume
  cases, refresh-path fixture aligned to the grid. Full suite **442 passed,
  27 skipped**; ruff clean (src+tests); mypy clean on changed files.

## 8. Limitations carried forward

1. Intraday M5 is delayed (~10–15 min) snapshot data; the system is not
   real-time. Strategies must not rely on sub-15-minute latency.
2. M5 has small provider holes (3 current) — tolerated, flagged in the report.
3. Intraday unadjusted vs D1 adjusted — only a concern if a backtest mixes
   intervals across a split boundary.
4. No delisting coverage → survivorship for universe construction.
5. Single price source; no cross-validation against a second feed.

**Verdict: RELIABLE FOR STRATEGY RESEARCH** (D1 value-factor and 5m-scan
paths). Not a claim about strategy skill — that is the separate SystemGate /
OOS verdict from Phase 3 (`docs/audit/17-phase3-final-validation.md`).

## References

- Tests: `tests/unit/test_data_quality.py`, `tests/unit/test_bar_cleaner.py`,
  `tests/unit/test_data_agent.py`, `tests/unit/test_bar_validator.py`.
- Evidence: `scripts/data_quality_report.py` output (above), psql integrity
  queries in §2, M5 gap listing (Aug-05 11:45–14:30 ET, now repaired).
- Prior audit: `docs/audit/17-phase3-final-validation.md` (engine/gate/risk
  verdict, unchanged).
